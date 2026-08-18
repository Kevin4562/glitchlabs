"""Project namespaces bound to one server-selected hardware/connector profile.

A server process loads exactly one projects/*.yaml profile before constructing any
adapter. Database projects remain useful analysis namespaces, but switching a row must
never masquerade as switching the live target/connector configuration.
"""
from __future__ import annotations

from typing import Annotated

from pydantic import Field

from . import anns, meta


def register(srv, core):
    store = core.store

    @srv.tool(name="create_project", title="Create an inactive analysis namespace",
              description="Create a database namespace for importing or organizing historical data. "
              "It does not load a target, glitcher, or connector profile and cannot become the live "
              "destination in this server process. Start a new server with --project-profile to "
              "select another live configuration.",
              meta=meta("SAFE", "invisible", 300))
    def create_project(
        name: Annotated[str, Field(min_length=1, description="Unique human-readable project name.")],
        notes: Annotated[str, Field(description=
            "Target, board revision, injection point, and connector-profile provenance.")] = "",
        activate: Annotated[bool, Field(description=
            "Deprecated. Live activation is refused; project profiles are selected at startup.")] = False,
    ) -> dict:
        if activate:
            return {"ok": False, "refused": True, "name": name,
                    "reason": "live_project_profiles_are_selected_at_server_startup",
                    "active_project": core.config_project_id}
        pid = store.create_project(name, notes)
        return {"ok": True, "project_id": pid, "name": name, "active": False,
                "note": "analysis namespace only; no hardware/connector configuration loaded"}

    @srv.tool(name="list_projects", description="List all projects with per-project totals "
              "(campaigns, attempts, legacy success-class rows) and which server-selected profile is active.",
              annotations=anns(read_only=True), meta=meta("SAFE", "invisible", 800))
    def list_projects() -> dict:
        return {"active_project": core.active.get("project_id"),
                "projects": store.projects_with_totals()}

    @srv.tool(name="set_active_project", description="Confirm the server-configured live project. "
              "A different database namespace is refused because changing an ID cannot reload or "
              "validate a different hardware/connector profile; restart with --project-profile instead.",
              meta=meta("SAFE", "invisible", 300))
    def set_active_project(project_id: Annotated[str, Field(description=
        "Exact project ID returned by list_projects.")]) -> dict:
        if not any(p["id"] == project_id for p in store.list_projects()):
            return {"ok": False, "error": f"project {project_id} not found"}
        if project_id != core.config_project_id:
            return {"ok": False, "refused": True,
                    "reason": "project_profile_restart_required",
                    "configured_project": core.config_project_id,
                    "project_profile": str(core.rig.project_profile_path or "")}
        core.active.update({"project_id": project_id, "campaign_id": None, "session_id": None})
        core.active.pop("ui_validated_sweep_id", None)
        return {"ok": True, "active_project": project_id, "profile_unchanged": True}

    @srv.tool(name="move_campaign", title="Campaign evidence ownership is immutable",
              description="Compatibility stub that always refuses. Moving a campaign would change "
              "project joins used for evidence interpretation even if session snapshots stayed intact. "
              "Create/import a new analysis record instead of rewriting campaign ownership.",
              annotations=anns(read_only=True), meta=meta("SAFE", "invisible", 300))
    def move_campaign(
        campaign_id: Annotated[str, Field(description="Campaign to re-scope.")],
        project_id: Annotated[str, Field(description=
            "Destination project whose target/connector semantics apply.")],
    ) -> dict:
        return {"ok": False, "refused": True,
                "reason": "campaign_evidence_ownership_is_immutable",
                "campaign_id": campaign_id, "requested_project_id": project_id}
