"""Workspace connector discovery and SDK guidance tools."""
from __future__ import annotations

from typing import Annotated, Any

from pydantic import Field

from . import anns, meta


def register(srv, core):
    @srv.tool(
        name="list_connectors", title="List hot-loaded connection modules",
        description=(
            "Rescan workspace connector manifests and return every selectable connector, "
            "its source fingerprint, dynamic sweep parameters, capabilities, and SDK hint. "
            "This never opens hardware."
        ), annotations=anns(read_only=True), meta=meta("SAFE", "invisible", 3000),
    )
    def list_connectors() -> dict[str, Any]:
        from ..connections import connector_sdk_instructions, describe_connectors

        return {"ok": True, "connectors": describe_connectors(),
                "sdk": connector_sdk_instructions()}

    @srv.tool(
        name="get_connector_schema", title="Get one connector's dynamic parameter schema",
        description=(
            "Rescan and return one connector's typed dynamic parameters, static configuration "
            "schema, capability declaration, source fingerprint, and workspace location."
        ), annotations=anns(read_only=True), meta=meta("SAFE", "invisible", 3000),
    )
    def get_connector_schema(
        connector_id: Annotated[str, Field(min_length=1, description=
            "Connector id shown by list_connectors, for example uart-target.")]
    ) -> dict[str, Any]:
        from ..connections import describe_connectors

        matches = [item for item in describe_connectors() if item.get("id") == connector_id]
        if not matches:
            return {"ok": False, "error": f"connector {connector_id!r} is not registered"}
        return {"ok": True, "connector": matches[0]}

    @srv.tool(
        name="validate_connector_parameters", title="Validate connector sweep parameters",
        description=(
            "Resolve defaults, validate types/ranges/dependencies, and return the exact "
            "fingerprinted connector block to embed in define_sweep. No hardware is opened."
        ), annotations=anns(read_only=True), meta=meta("SAFE", "invisible", 2000),
    )
    def validate_connector_parameters(
        connector_id: Annotated[str, Field(min_length=1, description=
            "Registered connector id selected for the sweep.")],
        parameters: Annotated[dict | None, Field(description=
            "Connector-specific dynamic values; omitted values use module defaults.")] = None,
    ) -> dict[str, Any]:
        from ..connections import resolve_connector_selection

        try:
            selected = resolve_connector_selection(
                core.rig.project_profile,
                {"id": connector_id, "parameters": parameters or {}},
            )
        except Exception as exc:
            return {"ok": False, "error": str(exc)}
        return {"ok": True, "connector": selected,
                "define_sweep_fragment": {"connector": selected}}

    @srv.tool(
        name="get_connector_sdk_instructions", title="Build a new workspace connector",
        description=(
            "Return the versioned public Python base-class contract, manifest layout, hot-load "
            "location, safety rules, and required methods for an AI creating a new UART/JTAG/SWD "
            "connector beside a running GlitchLab checkout."
        ), annotations=anns(read_only=True), meta=meta("SAFE", "invisible", 2500),
    )
    def get_connector_sdk_instructions() -> dict[str, Any]:
        from ..connections import connector_sdk_instructions

        return {"ok": True, **connector_sdk_instructions()}
