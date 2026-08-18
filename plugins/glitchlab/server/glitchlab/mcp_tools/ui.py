"""Visible MCP tools (spec goal: "controls the active pages and hits the buttons on the pages").

Each tool pushes a command over the UI command bus to every connected viewer browser, which applies
it (navigate / fill a field / click a button / highlight) so a human watching sees the screen MOVE,
then acknowledges. The ack is returned to the agent. When a visible tool clicks a real button (e.g.
"Start Sweep"), the button's own handler fires the backend action — closing the loop
MCP → UI button → real glitch.

Contrast with the read tools (read.py): those are *invisible* — they gather data without moving the
UI. `ui_get_state` is a UI-state read (reports the screen without changing it).
"""
from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import Field

from . import anns, meta

PAGES = ["home", "campaigns", "live", "paramdb", "instruments", "settings"]


def register(srv, core):
    ui = core.uibus

    @srv.tool(name="ui_navigate", description="VISIBLE: switch the viewer to a screen so the human "
              "sees it change. page ∈ {home, campaigns, live, paramdb, instruments, "
              "settings}.", annotations=anns(open_world=True),
              meta=meta("SAFE", "visible", 300))
    async def ui_navigate(page: Annotated[Literal["home", "campaigns", "live", "paramdb",
                                                   "instruments", "settings"],
                                          Field(description="Viewer page to show the operator.")]) -> dict:
        page = page.lower()
        if page not in PAGES:
            return {"ok": False, "error": f"unknown page; choose from {PAGES}"}
        return await ui.send("navigate", {"page": page})

    @srv.tool(name="ui_click", description="VISIBLE: click an existing data-mcp control. Valid IDs "
              "include run_preflight, discover_timing, acknowledge_target, start_sweep, pause_sweep, "
              "read_connector, refresh_map, refresh_workflow, bind_scope, unbind_scope, dry_run_toggle, and theme_toggle. "
              "bind_scope may be hidden/refused when project evidence owns the companion scope; unbind_scope "
              "explicitly releases a prior companion session. Preserved/unknown-held target state "
              "disables start, preflight, connector reads, timing, and all scope controls. The first "
              "start_sweep click must leave DRY-RUN enabled; that creates and validates an immutable "
              "sweep plan from the currently visible fields. The fields remain editable while a run "
              "is active; edits apply to the next dry-run plan and never mutate an in-flight plan. "
              "A later live click executes only the validated plan. The button "
              "becomes Stop while running—there is no separate stop_sweep element.",
              annotations=anns(destructive=False, open_world=True),
              meta=meta("CAUTION", "visible", 300,
                        safety={"backend_enforced": True, "target_dependent": True}))
    async def ui_click(target: Annotated[str, Field(description=
        "Exact data-mcp ID on the current page; call ui_get_state after clicking.")]) -> dict:
        return await ui.send("click", {"target": target})

    @srv.tool(name="ui_set_field", description="VISIBLE: edit a sweep field in the viewer. Changes "
              "define the next dry-run plan and never mutate a sweep already in progress.",
              annotations=anns(open_world=True), meta=meta("SAFE", "visible", 300))
    async def ui_set_field(
        field: Annotated[Literal["campaign_name", "connector_id", "width_min", "width_max",
                                 "width_step", "offset_min", "offset_max", "offset_step", "repeats"],
                         Field(description="Editable field shown in the sweep form.")],
        value: Annotated[Any, Field(description="Value to show in the viewer field.")],
    ) -> dict:
        return await ui.send("set_field", {"field": field, "value": value})

    @srv.tool(name="ui_fill_form", description="VISIBLE: fill one or more editable sweep fields. "
              "Changes define the next dry-run plan and never mutate an active sweep.", annotations=anns(open_world=True),
              meta=meta("SAFE", "visible", 400))
    async def ui_fill_form(fields: Annotated[dict, Field(description=
        "Mapping of campaign_name, connector_id, width/offset min/max/step, and repeats.")]) -> dict:
        allowed = {"campaign_name", "connector_id", "width_min", "width_max", "width_step",
                   "offset_min", "offset_max", "offset_step", "repeats"}
        refused = sorted(set(fields) - allowed)
        if refused:
            return {"ok": False, "refused": True,
                    "reason": "unknown_viewer_fields",
                    "unknown_fields": refused,
                    "allowed_fields": sorted(allowed)}
        return await ui.send("fill_form", {"fields": fields})

    @srv.tool(name="ui_highlight", description="VISIBLE: flash-highlight an element (by data-mcp id) "
              "to draw the human's attention.", annotations=anns(open_world=True),
              meta=meta("SAFE", "visible", 200))
    async def ui_highlight(
        target: Annotated[str, Field(description="Existing data-mcp or data-mcp-field ID.")],
        note: Annotated[str, Field(description="Optional operator-facing explanation.")] = "",
    ) -> dict:
        return await ui.send("highlight", {"target": target, "note": note})

    @srv.tool(name="ui_toast", description="VISIBLE: show a transient banner/toast message in the "
              "viewer.", annotations=anns(open_world=True), meta=meta("SAFE", "visible", 200))
    async def ui_toast(
        message: Annotated[str, Field(description="Short operator-facing status message.")],
        level: Annotated[Literal["info", "success", "caution", "danger"], Field(description=
            "Visual severity only; does not alter campaign state.")] = "info",
    ) -> dict:
        return await ui.send("toast", {"message": message, "level": level})

    @srv.tool(name="ui_get_state", description="UI-READ: report the viewer's current screen + field "
              "values + connected clients WITHOUT moving the UI.", annotations=anns(read_only=True),
              meta=meta("SAFE", "visible", 400))
    async def ui_get_state() -> dict:
        # ask a live browser for its state; fall back to the last server-tracked state
        ack = await ui.send("get_state", {})
        if ack.get("applied"):
            return {"ok": True, "live": True, "state": ack.get("state", {}),
                    "clients": ui.client_count}
        return {"ok": True, "live": False, "state": ui.last_state, "clients": ui.client_count}
