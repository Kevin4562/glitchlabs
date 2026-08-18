"""Portable configuration, private settings, and target-safety loading."""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
import hashlib
import json
import os
from pathlib import Path
import re
import sys
from typing import Any
from urllib.parse import urlparse

import yaml


PACKAGE_ROOT = Path(__file__).resolve().parent.parent
# Compatibility name for modules that resolve bundled, read-only assets from
# the server package. User data is always written under DATA_DIR instead.
PROJECT_ROOT = PACKAGE_ROOT
BUNDLED_CONFIG_DIR = PACKAGE_ROOT / "config"


def _default_data_dir() -> Path:
    override = os.environ.get("GLITCHLAB_DATA")
    if override:
        return Path(override).expanduser().resolve()
    if sys.platform == "win32" and os.environ.get("LOCALAPPDATA"):
        return (Path(os.environ["LOCALAPPDATA"]) / "GlitchLab").resolve()
    if sys.platform == "darwin":
        return (Path.home() / "Library" / "Application Support" / "GlitchLab").resolve()
    base = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share"))
    return (base / "glitchlab").resolve()


DATA_DIR = _default_data_dir()
LAKE_DIR = DATA_DIR / "data-lake"
SESSION_DIR = DATA_DIR / "sessions"
CSV_DIR = DATA_DIR / "csv"
BLOB_DIR = DATA_DIR / "blobs"
FIGURE_DIR = DATA_DIR / "figures"
CONNECTOR_DIR = DATA_DIR / "connectors"
SETTINGS_PATH = DATA_DIR / "settings.json"
USER_RIG_CONFIG = DATA_DIR / "rig_config.yaml"
USER_TARGET_PROFILE = DATA_DIR / "generic-target.yaml"

VIEWER_HOST = os.environ.get("GLITCHLAB_HOST", "127.0.0.1")
# Standalone CLI default. The Codex plugin binds an OS-selected free port instead.
VIEWER_PORT = int(os.environ.get("GLITCHLAB_PORT", "43127"))
SCOPE_HINT_IP = os.environ.get("GLITCHLAB_SCOPE_IP", "")
SCOPE_WEBCONTROL_URL = os.environ.get(
    "GLITCHLAB_SCOPE_WEBCONTROL",
    f"http://{SCOPE_HINT_IP}/control.html" if SCOPE_HINT_IP else "",
)


def _write_private_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    try:
        temporary.chmod(0o600)
    except OSError:
        pass
    temporary.replace(path)


def ensure_user_files() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    pairs = (
        (BUNDLED_CONFIG_DIR / "rig_config.example.yaml", USER_RIG_CONFIG),
        (BUNDLED_CONFIG_DIR / "generic-target.example.yaml", USER_TARGET_PROFILE),
    )
    for source, destination in pairs:
        if not destination.exists():
            destination.write_text(source.read_text(encoding="utf-8"), encoding="utf-8")
    if not SETTINGS_PATH.exists():
        _write_private_json(SETTINGS_PATH, {"notifications": {
            "enabled": False, "topic": "", "base_url": "https://ntfy.sh"
        }})


def load_user_settings() -> dict[str, Any]:
    ensure_user_files()
    try:
        value = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def mask_secret(value: str | None) -> str:
    secret = str(value or "")
    if not secret:
        return ""
    if len(secret) <= 10:
        return "configured"
    return f"{secret[:4]}…{secret[-4:]}"


def notification_settings(rig: "RigConfig | None" = None) -> dict[str, Any]:
    raw = dict(((rig.raw if rig else {}).get("notifications") or {}))
    stored = dict(load_user_settings().get("notifications") or {})
    return {
        "enabled": bool(stored.get("enabled", raw.get("ntfy_enabled", False))),
        "topic": str(stored.get("topic", raw.get("ntfy_topic", "")) or "").strip(),
        "base_url": str(
            stored.get("base_url", raw.get("ntfy_base_url", "https://ntfy.sh"))
            or "https://ntfy.sh"
        ).rstrip("/"),
    }


def save_notification_settings(*, enabled: bool, topic: str, base_url: str) -> dict[str, Any]:
    settings = load_user_settings()
    existing = dict(settings.get("notifications") or {})
    clean_topic = str(topic or "").strip()
    if enabled and not clean_topic:
        clean_topic = str(existing.get("topic") or "").strip()
    clean_base = str(base_url or "https://ntfy.sh").strip().rstrip("/")
    if enabled and not clean_topic:
        raise ValueError("a topic is required when notifications are enabled")
    if clean_topic and not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", clean_topic):
        raise ValueError("topic must use 1-128 letters, digits, underscores, or hyphens")
    parsed = urlparse(clean_base)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("notification base URL must be an absolute HTTP(S) URL")
    settings["notifications"] = {
        "enabled": bool(enabled), "topic": clean_topic, "base_url": clean_base
    }
    _write_private_json(SETTINGS_PATH, settings)
    return {
        "enabled": bool(enabled and clean_topic),
        "configured": bool(clean_topic),
        "topic": mask_secret(clean_topic),
        "base_url": clean_base,
    }


def _load_yaml(text: str) -> dict[str, Any]:
    value = yaml.safe_load(text) or {}
    if not isinstance(value, dict):
        raise ValueError("configuration must be a YAML mapping")
    return value


def _restrictive_limit_merge(operator: dict, target: dict) -> dict:
    """Combine rig and target limits without letting either widen the other."""
    result = deepcopy(operator)
    max_keys = {
        "pulse_cycles_max", "width_cycles_max", "ext_offset_max", "repeat_max",
        "num_glitches_max", "vcc_max_v", "max_cycles_per_minute",
        "max_attempts_per_second", "amplitude_vpp_max", "offset_v_abs_max",
        "frequency_hz_max", "rated_max_input_v",
    }
    min_keys = {"ext_offset_min", "min_seconds_between_cycles"}
    for key, target_value in target.items():
        operator_value = result.get(key)
        if isinstance(operator_value, dict) and isinstance(target_value, dict):
            result[key] = _restrictive_limit_merge(operator_value, target_value)
        elif isinstance(operator_value, bool) and isinstance(target_value, bool):
            result[key] = operator_value or target_value
        elif key in max_keys and isinstance(operator_value, (int, float)) and isinstance(target_value, (int, float)):
            result[key] = min(operator_value, target_value)
        elif key in min_keys and isinstance(operator_value, (int, float)) and isinstance(target_value, (int, float)):
            result[key] = max(operator_value, target_value)
        else:
            result[key] = deepcopy(target_value)
    return result


@dataclass
class RigConfig:
    raw: dict = field(default_factory=dict)
    path: Path | None = None
    source_sha256: str | None = None
    _project_profile_cache: dict | None = field(default=None, init=False, repr=False)
    _project_profile_path_cache: Path | None = field(default=None, init=False, repr=False)
    _project_profile_source_sha256: str | None = field(default=None, init=False, repr=False)

    @property
    def operator_limits(self) -> dict:
        return self.raw.get("limits", {})

    @property
    def target_safety_limits(self) -> dict:
        profile = self.project_profile
        limits = profile.get("safety_limits", profile.get("limits", {}))
        return dict(limits) if isinstance(limits, dict) else {}

    @property
    def limits(self) -> dict:
        return _restrictive_limit_merge(self.operator_limits, self.target_safety_limits)

    @property
    def rig(self) -> dict:
        return self.raw.get("rig", {})

    @property
    def instruments(self) -> dict:
        return self.raw.get("instruments", {})

    @property
    def glitcher_id(self) -> str:
        return self.rig.get("glitcher", "simulator")

    @property
    def target_model(self) -> str:
        return self.rig.get("target_model", "UNCONFIGURED")

    @property
    def project_profile_path(self) -> Path | None:
        if self._project_profile_cache is not None:
            return self._project_profile_path_cache
        value = os.environ.get("GLITCHLAB_PROJECT_PROFILE") or self.rig.get("project_profile")
        if not value:
            return None
        candidate = Path(os.path.expandvars(str(value))).expanduser()
        if not candidate.is_absolute():
            candidate = (self.path.parent if self.path else DATA_DIR) / candidate
        return candidate.resolve()

    @property
    def project_profile(self) -> dict:
        if self._project_profile_cache is not None:
            return deepcopy(self._project_profile_cache)
        profile_path = self.project_profile_path
        if profile_path is None:
            profile: dict[str, Any] = {}
            self._project_profile_path_cache = None
            self._project_profile_source_sha256 = None
        else:
            if not profile_path.is_file():
                raise FileNotFoundError(f"project profile not found: {profile_path}")
            raw_bytes = profile_path.read_bytes()
            profile = _load_yaml(raw_bytes.decode("utf-8"))
            self._project_profile_path_cache = profile_path
            self._project_profile_source_sha256 = hashlib.sha256(raw_bytes).hexdigest()
        self._project_profile_cache = dict(profile)
        return deepcopy(self._project_profile_cache)

    @property
    def project_profile_source_sha256(self) -> str | None:
        _ = self.project_profile
        return self._project_profile_source_sha256

    def resolve_project_path(self, value: str | os.PathLike) -> Path:
        candidate = Path(os.path.expandvars(str(value))).expanduser()
        if candidate.is_absolute():
            return candidate.resolve()
        base = self.project_profile_path.parent if self.project_profile_path else (self.path.parent if self.path else DATA_DIR)
        return (base / candidate).resolve()

    def limit(self, *path: str, default: Any = None) -> Any:
        node: Any = self.limits
        for key in path:
            if not isinstance(node, dict) or key not in node:
                return default
            node = node[key]
        return node

    def has_limits_for(self, *path: str) -> bool:
        return self.limit(*path, default=None) is not None


def load_rig_config(path: str | os.PathLike | None = None) -> RigConfig:
    ensure_user_files()
    selected = path or os.environ.get("GLITCHLAB_CONFIG") or USER_RIG_CONFIG
    config_path = Path(selected).expanduser().resolve()
    source = config_path.read_bytes()
    raw = _load_yaml(source.decode("utf-8"))
    glitcher_override = os.environ.get("GLITCHLAB_GLITCHER_OVERRIDE")
    if glitcher_override:
        raw.setdefault("rig", {})["glitcher_override"] = glitcher_override
    return RigConfig(raw=raw, path=config_path, source_sha256=hashlib.sha256(source).hexdigest())


def _ensure_dirs() -> None:
    for directory in (DATA_DIR, LAKE_DIR, SESSION_DIR, CSV_DIR, BLOB_DIR, FIGURE_DIR, CONNECTOR_DIR):
        directory.mkdir(parents=True, exist_ok=True)
    ensure_user_files()


_ensure_dirs()
