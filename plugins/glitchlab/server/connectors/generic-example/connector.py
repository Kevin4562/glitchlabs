"""Template-only connector. Copy it to the private connector directory before editing."""
from __future__ import annotations

from typing import Any, Mapping

from glitchlab.connections import (
    ConnectionCapabilities,
    ConnectionModule,
    ConnectionReading,
    DynamicParameter,
)


class GenericExampleConnection(ConnectionModule):
    name = "generic-example"
    connector_id = "generic-example"
    capabilities = ConnectionCapabilities(
        transport="template-only",
        process_isolated=True,
        read_only=True,
        provides_runtime_liveness=False,
        provides_connection_health=False,
    )

    def __init__(self, project_id: str | None = None, **config: Any) -> None:
        self.project_id = project_id
        self.config = dict(config)
        self.attempt_parameters: dict[str, Any] = {}

    @classmethod
    def static_config_schema(cls) -> Mapping[str, Any]:
        return {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "endpoint": {
                    "type": "string",
                    "description": "Replace with the private target endpoint or device identity."
                }
            },
        }

    @classmethod
    def dynamic_parameters(cls) -> tuple[DynamicParameter, ...]:
        return (
            DynamicParameter(
                name="confirmation_reads", parameter_type="integer", default=2,
                minimum=1, maximum=8,
                description="Independent observations required before confirmation."
            ),
            DynamicParameter(
                name="preserve_on_partial", parameter_type="boolean", default=True,
                description="Preserve target state when evidence is incomplete but interesting."
            ),
        )

    def connect(self) -> dict[str, Any]:
        return {
            "ok": False,
            "template_only": True,
            "error": "generic-example is not a real hardware connector; copy and implement it first",
        }

    def read(self, context: dict | None = None) -> ConnectionReading:
        raise RuntimeError("generic-example cannot read hardware")
