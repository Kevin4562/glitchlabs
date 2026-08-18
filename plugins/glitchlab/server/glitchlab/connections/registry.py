"""Hot-reloadable registry for workspace-owned GlitchLab connectors."""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import importlib
import importlib.util
import json
import os
from pathlib import Path
import re
import sys
import tomllib
from typing import Any, Mapping

from .. import config as app_config
from .base import ConnectionModule


MANIFEST_NAME = "glitchlab_connector.toml"
CONNECTOR_API_VERSION = 1


def _normalise(value: str) -> str:
    return re.sub(r"[^a-z0-9_-]+", "-", str(value).strip().lower()).strip("-")


def _file_digest(path: Path) -> dict[str, Any]:
    raw = path.read_bytes()
    return {"path": str(path.resolve()), "sha256": hashlib.sha256(raw).hexdigest(),
            "size": len(raw)}


def _source_files(root: Path) -> list[Path]:
    return sorted(
        path for path in root.rglob("*")
        if path.is_file() and path.name != "__pycache__" and "__pycache__" not in path.parts
        and path.suffix.lower() in {".py", ".toml", ".jlinkscript", ".json", ".yaml", ".yml"}
    )


def _fingerprint(root: Path) -> tuple[str, list[dict[str, Any]]]:
    files = [_file_digest(path) for path in _source_files(root)]
    payload = json.dumps(
        [{"relative": str(Path(item["path"]).relative_to(root.resolve())),
          "sha256": item["sha256"], "size": item["size"]} for item in files],
        sort_keys=True, separators=(",", ":"),
    ).encode()
    return hashlib.sha256(payload).hexdigest(), files


def default_connector_roots() -> list[Path]:
    project_root = Path(__file__).resolve().parents[2]
    roots = [app_config.CONNECTOR_DIR, project_root / "connectors"]
    for raw in os.environ.get("GLITCHLAB_CONNECTOR_PATHS", "").split(os.pathsep):
        if raw.strip():
            roots.append(Path(raw).expanduser())
    result: list[Path] = []
    for root in roots:
        resolved = root.resolve()
        if resolved not in result:
            result.append(resolved)
    return result


@dataclass(frozen=True)
class ConnectorDescriptor:
    connector_id: str
    display_name: str
    description: str
    root: Path
    entrypoint: str
    api_version: int
    glitcher_id: str | None
    glitcher_entrypoint: str | None
    fingerprint: str
    files: tuple[dict[str, Any], ...]

    def public(self) -> dict[str, Any]:
        try:
            self.root.relative_to(app_config.CONNECTOR_DIR.resolve())
            source = "private user connector"
        except ValueError:
            source = "bundled example"
        return {
            "id": self.connector_id, "display_name": self.display_name,
            "description": self.description, "root": str(self.root), "source": source,
            "entrypoint": self.entrypoint, "api_version": self.api_version,
            "private_glitcher": ({
                "id": self.glitcher_id,
                "entrypoint": self.glitcher_entrypoint,
            } if self.glitcher_id else None),
            "fingerprint": self.fingerprint, "files": list(self.files),
        }


class ConnectorRegistry:
    def __init__(self, roots: list[Path] | None = None):
        self.roots = roots or default_connector_roots()
        self._descriptors: dict[str, ConnectorDescriptor] = {}
        self._classes: dict[tuple[str, str], type[ConnectionModule]] = {}
        self._components: dict[tuple[str, str, str], type[Any]] = {}

    def refresh(self) -> list[ConnectorDescriptor]:
        found: dict[str, ConnectorDescriptor] = {}
        manifests: list[Path] = []
        for root in self.roots:
            if not root.is_dir():
                continue
            direct = root / MANIFEST_NAME
            if direct.is_file():
                manifests.append(direct)
            manifests.extend(path for path in root.glob(f"*/{MANIFEST_NAME}") if path.is_file())
        for manifest_path in sorted(set(manifests)):
            data = tomllib.loads(manifest_path.read_text(encoding="utf-8"))
            section = data.get("connector") or {}
            connector_id = _normalise(section.get("id", ""))
            if not connector_id:
                raise ValueError(f"{manifest_path} requires [connector].id")
            api_version = int(section.get("api_version", 0))
            if api_version != CONNECTOR_API_VERSION:
                raise ValueError(
                    f"connector {connector_id!r} uses API {api_version}; expected {CONNECTOR_API_VERSION}"
                )
            if connector_id in found:
                raise ValueError(f"duplicate connector id {connector_id!r}")
            glitcher = data.get("glitcher") or {}
            if not isinstance(glitcher, Mapping):
                raise ValueError(f"{manifest_path} [glitcher] must be a mapping")
            glitcher_id = _normalise(glitcher.get("id", "")) or None
            glitcher_entrypoint = str(glitcher.get("entrypoint") or "") or None
            if bool(glitcher_id) != bool(glitcher_entrypoint):
                raise ValueError(
                    f"{manifest_path} [glitcher] requires both id and entrypoint"
                )
            if glitcher and int(glitcher.get("api_version", 0)) != CONNECTOR_API_VERSION:
                raise ValueError(
                    f"private glitcher {glitcher_id!r} uses API "
                    f"{glitcher.get('api_version')}; expected {CONNECTOR_API_VERSION}"
                )
            root_dir = manifest_path.parent.resolve()
            fingerprint, files = _fingerprint(root_dir)
            found[connector_id] = ConnectorDescriptor(
                connector_id=connector_id,
                display_name=str(section.get("display_name") or connector_id),
                description=str(section.get("description") or ""),
                root=root_dir,
                entrypoint=str(section.get("entrypoint") or "connector:Connector"),
                api_version=api_version,
                glitcher_id=glitcher_id,
                glitcher_entrypoint=glitcher_entrypoint,
                fingerprint=fingerprint,
                files=tuple(files),
            )
        self._descriptors = found
        return list(found.values())

    def descriptor(self, connector_id: str) -> ConnectorDescriptor:
        self.refresh()
        canonical = _normalise(connector_id)
        try:
            return self._descriptors[canonical]
        except KeyError as exc:
            raise ValueError(
                f"unknown connector {connector_id!r}; discovered: {', '.join(sorted(self._descriptors))}"
            ) from exc

    def _load_component(self, descriptor: ConnectorDescriptor, entrypoint: str) -> type[Any]:
        key = (descriptor.connector_id, descriptor.fingerprint, entrypoint)
        cached = self._components.get(key)
        if cached is not None:
            return cached
        module_part, separator, attribute = entrypoint.partition(":")
        if not separator or not module_part or not attribute:
            raise ValueError(f"invalid private entrypoint {entrypoint!r}")
        package_name = (
            "_glitchlab_workspace_connector_"
            + re.sub(r"[^a-z0-9_]", "_", descriptor.connector_id)
            + "_" + descriptor.fingerprint[:16]
        )
        init_path = descriptor.root / "__init__.py"
        if not init_path.is_file():
            raise ValueError(f"connector {descriptor.connector_id!r} requires __init__.py")
        if package_name not in sys.modules:
            spec = importlib.util.spec_from_file_location(
                package_name, init_path,
                submodule_search_locations=[str(descriptor.root)],
            )
            if spec is None or spec.loader is None:
                raise ImportError(f"cannot load connector package {descriptor.root}")
            package = importlib.util.module_from_spec(spec)
            sys.modules[package_name] = package
            spec.loader.exec_module(package)
        module_name = package_name if module_part in {".", "__init__"} else f"{package_name}.{module_part}"
        module = importlib.import_module(module_name)
        cls = getattr(module, attribute)
        if not isinstance(cls, type):
            raise TypeError(f"private entrypoint {entrypoint!r} must resolve to a class")
        self._components[key] = cls
        return cls

    def load_class(self, descriptor: ConnectorDescriptor) -> type[ConnectionModule]:
        key = (descriptor.connector_id, descriptor.fingerprint)
        cached = self._classes.get(key)
        if cached is not None:
            return cached
        cls = self._load_component(descriptor, descriptor.entrypoint)
        if not isinstance(cls, type) or not issubclass(cls, ConnectionModule):
            raise TypeError(
                f"connector {descriptor.connector_id!r} entrypoint must subclass ConnectionModule"
            )
        self._classes[key] = cls
        return cls

    def load_glitcher_class(self, glitcher_id: str) -> type[Any]:
        """Load a fingerprinted private delivery adapter without opening hardware."""
        canonical = _normalise(glitcher_id)
        matches = [
            descriptor for descriptor in self.refresh()
            if descriptor.glitcher_id == canonical
        ]
        if not matches:
            raise ValueError(f"unknown private glitcher adapter {glitcher_id!r}")
        if len(matches) != 1:
            owners = ", ".join(sorted(item.connector_id for item in matches))
            raise ValueError(
                f"duplicate private glitcher adapter {glitcher_id!r}: {owners}"
            )
        descriptor = matches[0]
        assert descriptor.glitcher_entrypoint is not None
        cls = self._load_component(descriptor, descriptor.glitcher_entrypoint)
        from ..io.glitcher.base import GlitcherAdapter
        if not issubclass(cls, GlitcherAdapter):
            raise TypeError(
                f"private glitcher {glitcher_id!r} must subclass GlitcherAdapter"
            )
        return cls

    def describe(self) -> list[dict[str, Any]]:
        result = []
        for descriptor in self.refresh():
            cls = self.load_class(descriptor)
            result.append({**descriptor.public(), **cls.describe_connector()})
        return sorted(result, key=lambda item: item["id"])

    def instantiate(
        self,
        connector_config: Mapping[str, Any],
        *,
        project_id: str | None = None,
        expected_fingerprint: str | None = None,
    ) -> ConnectionModule:
        if not isinstance(connector_config, Mapping):
            raise TypeError("connector configuration must be a mapping")
        connector_id = connector_config.get("id") or connector_config.get("plugin")
        if not connector_id:
            raise ValueError("connector configuration requires 'id'")
        descriptor = self.descriptor(str(connector_id))
        if expected_fingerprint and descriptor.fingerprint != expected_fingerprint:
            raise RuntimeError(
                f"connector source changed: expected {expected_fingerprint}, got {descriptor.fingerprint}"
            )
        static_config = connector_config.get("config") or {}
        dynamic = connector_config.get("parameters") or connector_config.get("defaults") or {}
        if not isinstance(static_config, Mapping) or not isinstance(dynamic, Mapping):
            raise TypeError("connector config and parameters must be mappings")
        cls = self.load_class(descriptor)
        instance = cls(project_id=project_id, **dict(static_config))
        instance.connector_descriptor = descriptor.public()
        instance.configure_attempt(dynamic)
        return instance


_REGISTRY = ConnectorRegistry()


def describe_connectors() -> list[dict[str, Any]]:
    return _REGISTRY.describe()


def load_connection_class(connector_id: str) -> type[ConnectionModule]:
    """Load one connector class without instantiating it or opening hardware."""
    descriptor = _REGISTRY.descriptor(connector_id)
    return _REGISTRY.load_class(descriptor)


def load_private_glitcher_class(glitcher_id: str) -> type[Any]:
    """Load a private adapter declared beside a private connector."""
    return _REGISTRY.load_glitcher_class(glitcher_id)


def refresh_connectors() -> list[dict[str, Any]]:
    return _REGISTRY.describe()


def make_connection_from_config(
    config: Mapping[str, Any], *, project_id: str | None = None,
    expected_fingerprint: str | None = None,
) -> ConnectionModule:
    return _REGISTRY.instantiate(
        config, project_id=project_id, expected_fingerprint=expected_fingerprint
    )


def make_connection_from_project(
    project: Mapping[str, Any], *, project_id: str | None = None,
    expected_fingerprint: str | None = None,
) -> ConnectionModule:
    block = project.get("connector")
    if not isinstance(block, Mapping):
        raise ValueError("project requires a 'connector' mapping")
    resolved = project_id or project.get("id") or project.get("project_id")
    return make_connection_from_config(
        block, project_id=str(resolved) if resolved is not None else None,
        expected_fingerprint=expected_fingerprint,
    )


def resolve_connector_selection(
    project: Mapping[str, Any], selection: Mapping[str, Any] | None = None
) -> dict[str, Any]:
    """Validate and fingerprint a connector selection without opening hardware."""
    declared = project.get("connector")
    if not isinstance(declared, Mapping):
        raise ValueError("project requires a 'connector' mapping")
    supplied = dict(selection or {})
    connector_id = supplied.get("id") or declared.get("id") or declared.get("plugin")
    if not connector_id:
        raise ValueError("connector selection requires an id")
    declared_id = declared.get("id") or declared.get("plugin")
    if declared_id and _normalise(str(connector_id)) != _normalise(str(declared_id)):
        raise ValueError(
            f"project connector is {declared_id!r}; selection {connector_id!r} is not authorized"
        )
    descriptor = _REGISTRY.descriptor(str(connector_id))
    expected = supplied.get("fingerprint")
    if expected and str(expected) != descriptor.fingerprint:
        raise RuntimeError(
            f"connector source changed: expected {expected}, got {descriptor.fingerprint}"
        )
    cls = _REGISTRY.load_class(descriptor)
    defaults = declared.get("parameters") or declared.get("defaults") or {}
    if not isinstance(defaults, Mapping):
        raise TypeError("project connector parameters must be a mapping")
    requested = supplied.get("parameters", defaults)
    if not isinstance(requested, Mapping):
        raise TypeError("selected connector parameters must be a mapping")
    parameters = cls.validate_dynamic_parameters(requested)
    return {
        "id": descriptor.connector_id,
        "fingerprint": descriptor.fingerprint,
        "parameters": parameters,
        "display_name": descriptor.display_name,
        "root": str(descriptor.root),
        "entrypoint": descriptor.entrypoint,
        "files": list(descriptor.files),
    }


def connector_sdk_instructions() -> dict[str, Any]:
    return {
        "api_version": CONNECTOR_API_VERSION,
        "manifest": MANIFEST_NAME,
        "location": (
            f"Create one folder per private connector under {app_config.CONNECTOR_DIR} "
            "(or add a directory with GLITCHLAB_CONNECTOR_PATHS). The server rescans manifests "
            "whenever connector tools or UI APIs are called; no plugin reinstall is required."
        ),
        "connector_root": str(app_config.CONNECTOR_DIR),
        "bundled_example": str(Path(__file__).resolve().parents[2] / "connectors" / "generic-example"),
        "manifest_example": {
            "connector": {
                "id": "uart-example", "display_name": "UART Example",
                "api_version": 1, "entrypoint": "connector:UartConnection",
            },
            "optional_private_glitcher": {
                "id": "my-target-delivery", "api_version": 1,
                "entrypoint": "adapter:MyTargetGlitcher",
            },
        },
        "python_contract": (
            "from glitchlab.connections import ConnectionModule, ConnectionReading, "
            "ConnectionCapabilities, DynamicParameter"
        ),
        "required_methods": ["read"],
        "optional_methods": [
            "connect", "disconnect", "probe_status", "bind_glitcher", "prepare_attempt",
            "trigger", "recover", "read_runtime_checkpoint", "classify_attempt"
        ],
        "safety": (
            "Connectors must declare reset/halt/write capabilities truthfully and return "
            "structured evidence. Hardware writes remain subject to GlitchLab's rig interlock."
        ),
    }
