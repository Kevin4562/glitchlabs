"""Public SDK for workspace-owned GlitchLab connection modules.

Connection modules own target communication, classification, and the structured
evidence emitted for an attempt.  They do not own the fault-delivery hardware.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import math
from typing import Any, Mapping, Optional


VALID_VERDICTS = ("success", "reset", "exception", "no-effect", "false-positive")
CONNECTION_READING_SCHEMA_VERSION = "glitchlab.connection-reading/v1"


@dataclass(frozen=True)
class DynamicParameter:
    """One connector-owned sweep parameter exposed through MCP and the UI."""

    name: str
    parameter_type: str
    default: Any
    description: str
    title: str | None = None
    choices: tuple[Any, ...] = ()
    minimum: float | None = None
    maximum: float | None = None
    group: str = "Connection"
    advanced: bool = False
    visible_if: Mapping[str, Any] | None = None

    def __post_init__(self) -> None:
        if not self.name or not self.name.replace("_", "").isalnum():
            raise ValueError(f"invalid dynamic parameter name {self.name!r}")
        if self.parameter_type not in {"boolean", "integer", "number", "string", "select"}:
            raise ValueError(f"unsupported dynamic parameter type {self.parameter_type!r}")
        if self.parameter_type == "select" and not self.choices:
            raise ValueError(f"select parameter {self.name!r} requires choices")
        self.validate(self.default)

    def validate(self, value: Any) -> Any:
        if self.parameter_type == "boolean":
            if type(value) is not bool:
                raise ValueError(f"{self.name} must be a boolean")
            result = value
        elif self.parameter_type == "integer":
            if type(value) is not int:
                raise ValueError(f"{self.name} must be an integer")
            result = value
        elif self.parameter_type == "number":
            if type(value) not in (int, float) or not math.isfinite(float(value)):
                raise ValueError(f"{self.name} must be a finite number")
            result = float(value)
        elif self.parameter_type in {"string", "select"}:
            if not isinstance(value, str):
                raise ValueError(f"{self.name} must be a string")
            result = value
        else:  # pragma: no cover - guarded in __post_init__
            raise ValueError(self.parameter_type)
        if self.choices and result not in self.choices:
            raise ValueError(f"{self.name} must be one of {list(self.choices)!r}")
        if self.minimum is not None and float(result) < self.minimum:
            raise ValueError(f"{self.name} must be >= {self.minimum}")
        if self.maximum is not None and float(result) > self.maximum:
            raise ValueError(f"{self.name} must be <= {self.maximum}")
        return result

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "type": self.parameter_type,
            "title": self.title or self.name.replace("_", " ").title(),
            "description": self.description,
            "default": self.default,
            "choices": list(self.choices),
            "minimum": self.minimum,
            "maximum": self.maximum,
            "group": self.group,
            "advanced": self.advanced,
            "visible_if": dict(self.visible_if or {}),
        }


@dataclass(frozen=True)
class ConnectionCapabilities:
    transport: str
    process_isolated: bool = False
    read_only: bool = True
    target_memory_reads: bool = False
    target_memory_writes: bool = False
    persistent_target_writes: bool = False
    target_reset: bool = False
    target_halt: bool = False
    target_resume: bool = False
    provides_runtime_liveness: bool = False
    provides_connection_health: bool = False

    def as_dict(self) -> dict[str, Any]:
        return dict(self.__dict__)


@dataclass
class ConnectionReading:
    """One connector observation; storage retains legacy oracle column names."""

    connection_name: str
    verdict: str
    latency_ms: float = 0.0
    detail: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.verdict not in VALID_VERDICTS:
            raise ValueError(f"invalid connection verdict {self.verdict!r}")
        if not isinstance(self.detail, dict):
            raise TypeError("connection detail must be a dict")

    @property
    def oracle_name(self) -> str:  # database/backward compatibility
        return self.connection_name

    def as_reading(self) -> dict[str, Any]:
        result = {
            "connection_name": self.connection_name,
            "oracle_name": self.connection_name,
            "verdict": self.verdict,
            "latency_ms": round(self.latency_ms, 3),
        }
        if self.detail:
            result["detail"] = self.detail
        return result


class ConnectionModule:
    """Base class imported by external workspace connector modules."""

    name = "connection"
    connector_id = "connection"
    plugin_id = "connection"  # compatibility with pre-connector adapters
    project_id: str | None = None
    read_only = True
    capabilities = ConnectionCapabilities(transport="unknown")

    @classmethod
    def static_config_schema(cls) -> Mapping[str, Any]:
        return {"type": "object", "additionalProperties": True}

    @classmethod
    def config_schema(cls) -> Mapping[str, Any]:  # compatibility alias
        return cls.static_config_schema()

    @classmethod
    def dynamic_parameters(cls) -> tuple[DynamicParameter, ...]:
        return ()

    @classmethod
    def validate_dynamic_parameters(cls, values: Mapping[str, Any] | None) -> dict[str, Any]:
        supplied = dict(values or {})
        definitions = {item.name: item for item in cls.dynamic_parameters()}
        unknown = sorted(set(supplied) - set(definitions))
        if unknown:
            raise ValueError("unknown connector parameter(s): " + ", ".join(unknown))
        resolved: dict[str, Any] = {}
        for name, definition in definitions.items():
            resolved[name] = definition.validate(supplied.get(name, definition.default))
        return resolved

    @classmethod
    def describe_connector(cls) -> dict[str, Any]:
        return {
            "id": cls.connector_id,
            "connection_name": cls.name,
            "static_config_schema": dict(cls.static_config_schema()),
            "dynamic_parameters": [item.as_dict() for item in cls.dynamic_parameters()],
            "capabilities": cls.capabilities.as_dict(),
        }

    @classmethod
    def describe_plugin(cls) -> dict[str, Any]:  # compatibility alias
        data = cls.describe_connector()
        data.update({"plugin": cls.connector_id, "oracle_name": cls.name,
                     "config_schema": data["static_config_schema"]})
        return data

    def configure_attempt(self, parameters: Mapping[str, Any] | None) -> dict[str, Any]:
        self.attempt_parameters = self.validate_dynamic_parameters(parameters)
        return dict(self.attempt_parameters)

    def describe(self) -> dict[str, Any]:
        data = self.describe_connector()
        data["project_id"] = self.project_id
        data["effective_dynamic_parameters"] = dict(
            getattr(self, "attempt_parameters", self.validate_dynamic_parameters({}))
        )
        return data

    def connect(self) -> dict[str, Any]:
        return {}

    def disconnect(self) -> None:
        return None

    def probe_status(self, **kwargs: Any) -> dict[str, Any]:
        return {}

    def bind_glitcher(self, glitcher: Any) -> None:
        """Receive the live delivery adapter without taking ownership of it."""
        self.glitcher = glitcher

    def prepare_attempt(self, context: Optional[dict] = None) -> dict[str, Any]:
        """Establish a fresh baseline/trigger state before the adapter arms."""
        return {"ok": True}

    def trigger(self, context: Optional[dict] = None) -> dict[str, Any]:
        """Cause the target event that the glitcher is armed against."""
        raise NotImplementedError(
            f"connector {self.connector_id!r} has no target trigger operation"
        )

    def read(self, context: Optional[dict] = None) -> ConnectionReading:
        raise NotImplementedError

    def read_runtime_checkpoint(self, context: Optional[dict] = None) -> ConnectionReading:
        raise NotImplementedError(
            f"connector {self.connector_id!r} has no runtime-checkpoint operation"
        )

    def recover(self, context: Optional[dict] = None) -> dict[str, Any]:
        """Return the target to the connector's reviewed baseline state."""
        return {"ok": False, "unsupported": True}

    def classify_attempt(
        self,
        *,
        primary: ConnectionReading,
        context: Mapping[str, Any] | None = None,
        evidence: Mapping[str, Any] | None = None,
        parameters: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Return connector-owned classification and evidence summary."""
        resolved = self.validate_dynamic_parameters(parameters or getattr(self, "attempt_parameters", {}))
        return {
            "classification": "success" if primary.verdict == "success" else primary.verdict,
            "verified": primary.verdict == "success",
            "preserve": primary.verdict in {"success", "exception"},
            "connection": primary.as_reading(),
            "parameters": resolved,
            "evidence": dict(evidence or {}),
        }

    def classify_primary(
        self, reading: ConnectionReading, parameters: Mapping[str, Any] | None = None
    ) -> dict[str, Any]:
        """Classify the immediate transport observation before slow evidence work."""
        resolved = self.validate_dynamic_parameters(parameters or getattr(self, "attempt_parameters", {}))
        infrastructure = bool(
            reading.verdict == "exception"
            or reading.detail.get("infrastructure_failure") is True
        )
        return {
            "raw_success": reading.verdict == "success",
            "partial": bool(reading.detail.get("partial_candidate_observed")),
            "infrastructure_failure": infrastructure,
            "preserve": bool(reading.verdict == "success" or infrastructure),
            "parameters": resolved,
        }


# Temporary source-compatibility aliases. External modules should use Connection* names.
Oracle = ConnectionModule
OracleReading = ConnectionReading
OracleCapabilities = ConnectionCapabilities
ORACLE_READING_SCHEMA_VERSION = CONNECTION_READING_SCHEMA_VERSION
