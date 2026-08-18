"""Legacy oracle registry retained for simulator and third-party compatibility.

New targets should use the public connection-module API instead.
"""
from __future__ import annotations

from dataclasses import dataclass
from importlib import import_module, metadata
from typing import Any, Callable, Mapping

from .base import Oracle


OracleFactory = Callable[..., Oracle]


@dataclass(frozen=True)
class OraclePlugin:
    plugin_id: str
    factory: OracleFactory | str
    aliases: tuple[str, ...] = ()
    description: str = ""

    def load_factory(self) -> OracleFactory:
        factory = self.factory
        if callable(factory):
            return factory
        module_name, separator, attribute = factory.partition(":")
        if not separator or not module_name or not attribute:
            raise ValueError(
                f"oracle factory {factory!r} must use the 'module:attribute' format"
            )
        value = getattr(import_module(module_name), attribute)
        if not callable(value):
            raise TypeError(f"oracle factory {factory!r} is not callable")
        return value


_PLUGINS: dict[str, OraclePlugin] = {}
_ALIASES: dict[str, str] = {}
_BUILTINS_REGISTERED = False
_ENTRY_POINTS_LOADED = False


def _normalise_id(value: str) -> str:
    return str(value or "").strip().lower().replace("-", "_")


def register_oracle_plugin(
    plugin_id: str,
    factory: OracleFactory | str,
    *,
    aliases: tuple[str, ...] | list[str] = (),
    description: str = "",
    replace: bool = False,
) -> None:
    """Register one plugin factory.

    Registration does not instantiate the plugin and therefore cannot touch hardware.
    ``replace`` is intended for tests and controlled application composition only.
    """

    canonical = _normalise_id(plugin_id)
    if not canonical:
        raise ValueError("oracle plugin id cannot be empty")
    if canonical in _PLUGINS and not replace:
        raise ValueError(f"oracle plugin {plugin_id!r} is already registered")
    alias_values = tuple(_normalise_id(alias) for alias in aliases)
    plugin = OraclePlugin(canonical, factory, alias_values, description)
    _PLUGINS[canonical] = plugin
    for alias in (canonical, *alias_values):
        owner = _ALIASES.get(alias)
        if owner not in (None, canonical) and not replace:
            raise ValueError(f"oracle alias {alias!r} is already owned by {owner!r}")
        _ALIASES[alias] = canonical


def _register_builtins() -> None:
    global _BUILTINS_REGISTERED
    if _BUILTINS_REGISTERED:
        return
    register_oracle_plugin(
        "simulated_connection",
        "glitchlab.io.oracle.sim_oracle:SimOracle",
        aliases=("sim", "simulator"),
        description="Deterministic simulated target observations for offline campaigns.",
    )
    _BUILTINS_REGISTERED = True


def load_entry_point_plugins() -> None:
    """Load optional ``glitchlab.oracles`` Python entry points once."""

    global _ENTRY_POINTS_LOADED
    if _ENTRY_POINTS_LOADED:
        return
    _ENTRY_POINTS_LOADED = True
    try:
        entry_points = metadata.entry_points()
        selected = entry_points.select(group="glitchlab.oracles")
    except Exception:
        return
    for entry_point in selected:
        canonical = _normalise_id(entry_point.name)
        if canonical in _PLUGINS:
            continue
        register_oracle_plugin(
            canonical,
            entry_point.load(),
            description=f"External oracle plugin from {entry_point.value}",
        )


def _plugin(plugin_id: str) -> OraclePlugin:
    _register_builtins()
    load_entry_point_plugins()
    requested = _normalise_id(plugin_id)
    canonical = _ALIASES.get(requested, requested)
    try:
        return _PLUGINS[canonical]
    except KeyError as exc:
        known = ", ".join(sorted(_PLUGINS))
        raise ValueError(
            f"unknown oracle plugin {plugin_id!r}; registered plugins: {known}"
        ) from exc


def _merge_mapping(base: Mapping[str, Any], updates: Mapping[str, Any]) -> dict[str, Any]:
    """Recursively merge config dictionaries without mutating project data."""

    result: dict[str, Any] = dict(base)
    for key, value in updates.items():
        current = result.get(key)
        if isinstance(current, Mapping) and isinstance(value, Mapping):
            result[key] = _merge_mapping(current, value)
        else:
            result[key] = value
    return result


def make_oracle_from_config(
    oracle_config: Mapping[str, Any],
    *,
    project_id: str | None = None,
    **overrides: Any,
) -> Oracle:
    """Instantiate the oracle selected by one project's ``oracle`` block."""

    if not isinstance(oracle_config, Mapping):
        raise TypeError("project oracle configuration must be a mapping")
    plugin_id = (
        oracle_config.get("plugin")
        or oracle_config.get("type")
        or oracle_config.get("id")
    )
    if not plugin_id:
        raise ValueError("project oracle configuration requires a 'plugin' field")

    nested = oracle_config.get("config", {})
    if nested is None:
        nested = {}
    if not isinstance(nested, Mapping):
        raise TypeError("project oracle 'config' field must be a mapping")

    # Adapt the historical flat ``oracle: {type, timeout_s, ...}`` format.  New
    # projects should put plugin arguments under ``config``.
    flat = {
        key: value
        for key, value in oracle_config.items()
        if key not in {"plugin", "type", "id", "config", "description"}
    }
    arguments = _merge_mapping(flat, nested)
    arguments = _merge_mapping(arguments, overrides)
    if project_id is not None:
        arguments.setdefault("project_id", str(project_id))

    instance = _plugin(str(plugin_id)).load_factory()(**arguments)
    from ...connections import ConnectionModule
    if not isinstance(instance, (Oracle, ConnectionModule)):
        raise TypeError(
            f"oracle plugin {plugin_id!r} returned {type(instance).__name__}, not Oracle"
        )
    return instance


def make_oracle_from_project(
    project: Mapping[str, Any],
    *,
    project_id: str | None = None,
    **overrides: Any,
) -> Oracle:
    """Instantiate the oracle declared by a complete project mapping."""

    if not isinstance(project, Mapping):
        raise TypeError("project must be a mapping")
    oracle_config = project.get("oracle")
    if not isinstance(oracle_config, Mapping) and isinstance(project.get("connector"), Mapping):
        # Deprecated compatibility bridge. The implementation is external and
        # the returned object follows the public ConnectionModule contract.
        from ...connections import make_connection_from_project

        return make_connection_from_project(
            project, project_id=project_id, **overrides
        )  # type: ignore[return-value]
    if not isinstance(oracle_config, Mapping):
        raise ValueError("project requires an 'oracle' mapping")
    resolved_id = project_id or project.get("id") or project.get("project_id")
    return make_oracle_from_config(
        oracle_config,
        project_id=str(resolved_id) if resolved_id is not None else None,
        **overrides,
    )


def make_oracle(oracle_id: str | Mapping[str, Any], **config: Any) -> Oracle:
    """Backward-compatible factory.

    Existing adapters may continue calling ``make_oracle('simulator')``.
    New code should call :func:`make_oracle_from_project`.
    """

    if isinstance(oracle_id, Mapping):
        return make_oracle_from_config(oracle_id, **config)
    return make_oracle_from_config(
        {"plugin": str(oracle_id), "config": config},
    )


def describe_oracle_plugins() -> list[dict[str, Any]]:
    """Return schemas and capabilities for UI/MCP discovery without opening hardware."""

    _register_builtins()
    load_entry_point_plugins()
    descriptions: list[dict[str, Any]] = []
    for plugin_id in sorted(_PLUGINS):
        plugin = _PLUGINS[plugin_id]
        factory = plugin.load_factory()
        describe = getattr(factory, "describe_plugin", None)
        data = describe() if callable(describe) else {}
        descriptions.append(
            {
                **data,
                "plugin": plugin_id,
                "aliases": list(plugin.aliases),
                "description": plugin.description,
            }
        )
    return descriptions
