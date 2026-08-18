"""FastAPI companion viewer (spec §19). Read-only peer reader of the shared store.

Live data flows over a WebSocket from the same event bus the MCP server uses. The SAME socket also
carries UI-control commands from *visible* MCP tools (navigate/click/fill), and browser buttons post
"action" messages back — so a visible MCP click and a human click drive the same backend path.

The viewer never owns a write path of its own for campaign data; sweep control funnels through the
same sweep engine + Safety Engine the MCP control tools use.
"""
from __future__ import annotations

import asyncio
import json
import re
import urllib.request
import uuid
from functools import lru_cache
from pathlib import Path

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from .. import config
from ..app_core import get_core
from ..render import descriptor, textart, image as image_render
from ..render.grid import build_grid
from ..domain import stats as stats_mod
from ..mcp_tools.workflow import (_physical_summary, _profile_summary, _target_profile,
                                  get_attempt_evidence_data, get_workflow_state_data)
from ..mcp_tools.scope import (acquire_companion_device_lease, companion_scope_policy,
                               release_companion_device_lease)
from ..mcp_tools.rig_state import target_state_interlock, target_state_refusal

TEMPLATES = Path(__file__).parent / "templates"
STATIC = Path(__file__).parent / "static"


def _downsample_scope_artifacts(rigol_dir: Path, max_points: int = 1600) -> dict:
    """Load persisted BYTE traces and retain min/max extrema in each display bucket."""
    import numpy as np

    max_points = max(200, min(int(max_points), 5000))

    def load_channel(stem: str) -> tuple[list[float] | None, dict]:
        meta_path = rigol_dir / f"{stem}.json"
        data_path = rigol_dir / f"{stem}.byte.bin"
        if not meta_path.is_file() or not data_path.is_file():
            return None, {}
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        preamble = dict(meta.get("preamble") or {})
        raw = np.fromfile(data_path, dtype=np.uint8)
        if raw.size == 0:
            return None, meta
        y_increment = float(preamble.get("y_increment", 1.0))
        y_origin = float(preamble.get("y_origin", 0.0))
        y_reference = float(preamble.get("y_reference", 0.0))
        volts = (raw.astype(np.float64) - y_origin - y_reference) * y_increment
        if volts.size > max_points:
            bucket_count = max(1, max_points // 2)
            edges = np.linspace(0, volts.size, bucket_count + 1, dtype=np.int64)
            selected: list[int] = []
            for start, stop in zip(edges[:-1], edges[1:]):
                if stop <= start:
                    continue
                chunk = volts[start:stop]
                lo = start + int(np.argmin(chunk))
                hi = start + int(np.argmax(chunk))
                selected.extend(sorted({lo, hi}))
            volts = volts[np.asarray(selected, dtype=np.int64)]
        return [round(float(value), 6) for value in volts], meta

    stems = [
        path.name[:-len(".byte.bin")]
        for path in sorted(rigol_dir.glob("*.byte.bin"))
        if (rigol_dir / f"{path.name[:-len('.byte.bin')]}.json").is_file()
    ]
    primary, primary_meta = load_channel(stems[0]) if stems else (None, {})
    observed, observed_meta = load_channel(stems[1]) if len(stems) > 1 else (None, {})
    if not primary and not observed:
        raise FileNotFoundError("persisted waveform channels are unavailable")
    sample_count = max(int(primary_meta.get("sample_count") or 0),
                       int(observed_meta.get("sample_count") or 0))
    preamble = dict((observed_meta or primary_meta).get("preamble") or {})
    return {
        "ok": True,
        "primary": primary,
        "observed": observed,
        "primary_label": primary_meta.get("label") or (stems[0] if stems else "primary signal"),
        "observed_label": observed_meta.get("label") or (
            stems[1] if len(stems) > 1 else "observed signal"
        ),
        "sample_count": sample_count,
        "sample_interval_s": preamble.get("x_increment"),
        "downsampled": sample_count > max(len(primary or []), len(observed or [])),
    }


def _attempt_waveform_dir(core, attempt_id: int) -> Path | None:
    rows = core.store.fetch_all(
        "SELECT detail FROM oracle_reading WHERE attempt_id=? ORDER BY id", (attempt_id,))
    evidence_root = (config.DATA_DIR / "evidence").resolve()
    for row in rows:
        detail = _json_object(row.get("detail"))
        candidate_dir = detail.get("candidate_dir")
        if not candidate_dir:
            continue
        try:
            resolved = Path(str(candidate_dir)).resolve()
            resolved.relative_to(evidence_root)
        except (OSError, ValueError):
            continue
        rigol_dir = resolved / "rigol"
        if rigol_dir.is_dir():
            return rigol_dir
    return None


@lru_cache(maxsize=512)
def _cached_downsample_scope_artifacts(rigol_dir: str, max_points: int) -> dict:
    """Waveform artifacts are immutable once attached to an attempt."""
    return _downsample_scope_artifacts(Path(rigol_dir), max_points=max_points)

# ---------------------------------------------------------------------------
# Same-origin proxy for the scope's control.html live viewer.
#
# The DHO924S serves its own /control.html (an H.264-over-WebSocket live screen). We embed it, but the
# device page has two behaviours that are hostile inside our app:
#   1. On losing its (single-session) video socket it fires a BLOCKING window.alert() the operator can't
#      dismiss from our UI.
#   2. res/config.js builds every ws:// endpoint from location.hostname, so it can't run from any origin
#      but the device's own.
# Serving control.html rewritten through GlitchLab's origin lets us neutralise the alert() and pin the
# sockets to the real device IP, while the heavy player JS still streams straight from the scope.
# ---------------------------------------------------------------------------
_SCOPE_LIVE_CACHE: dict = {"html": None}

_SCOPE_SHIM = """<base href="http://__IP__/">
<script>
/* GlitchLab embed shim: the device pops a BLOCKING alert() when another viewer steals its single video
   socket. Turn that into a non-blocking postMessage the parent renders as a quiet, reclaimable overlay. */
window.alert=function(m){try{parent.postMessage({t:"glitchlab-scope-alert",m:String(m||"")},"*");}catch(e){}try{console.warn("[scope]",m);}catch(e){}};
window.confirm=function(){return true;};window.print=function(){};
</script>
<style>
html,body{width:100%;height:100%;margin:0;background:#05070A;overflow:hidden}
body{display:flex;align-items:center;justify-content:center}
#pic{width:100%!important;height:100%!important;object-fit:contain!important;display:block}
</style>
"""


def _fetch_text(url: str, timeout: float = 6.0) -> str:
    with urllib.request.urlopen(url, timeout=timeout) as r:  # noqa: S310 (fixed LAN device URL)
        return r.read().decode("utf-8", "replace")


def _build_scope_live_html(ip: str) -> str:
    """Fetch the device's control.html + config.js and rewrite them to run from GlitchLab's origin.

    Every rewrite is verified: if the device firmware ever changes its markup so a substitution no longer
    matches, we raise instead of returning a silently-broken page (un-neutralised alert or sockets pinned
    to the wrong host). The /scope/live route then falls back to the last-good cache or the offline page.
    """
    base = "http://%s" % ip
    html = _fetch_text("%s/control.html" % base)
    cfg = _fetch_text("%s/res/config.js" % base)
    # config.js builds every ws:// from location.hostname (== our proxy origin here); pin to the real scope.
    cfg, n = re.subn(r"(?:window\.|document\.)?location\.hostname", '"%s"' % ip, cfg)
    if n == 0:
        raise RuntimeError("scope config.js: location.hostname not found (firmware markup changed)")
    # Inject <base> + alert shim + fit-to-pane CSS right after <head> (before the watchdog can fire). Use a
    # replacement function so shim/cfg text is inserted literally (no re backslash-escape interpretation).
    shim = _SCOPE_SHIM.replace("__IP__", ip)
    html, n = re.subn(r"<head[^>]*>", lambda m: m.group(0) + "\n" + shim, html, count=1, flags=re.IGNORECASE)
    if n != 1:
        raise RuntimeError("scope control.html: <head> not found (cannot inject shim)")
    # Replace the external config include with our inlined, IP-pinned copy (leave the heavy player JS to
    # stream from the device via <base>). Tolerate optional './' and a '?cache-bust' query.
    html, n = re.subn(r"""<script[^>]*src=["']\.?/?res/config\.js(?:\?[^"']*)?["'][^>]*>\s*</script>""",
                      lambda m: "<script>\n" + cfg + "\n</script>", html, count=1)
    if n != 1:
        raise RuntimeError("scope control.html: config.js include not found (cannot pin sockets)")
    return html


def _scope_offline_html(ip: str) -> str:
    return ("<!doctype html><meta charset=utf-8><style>html,body{height:100%;margin:0;background:#05070A;"
            "color:#7d8590;font:500 12px/1.6 ui-monospace,monospace;display:flex;align-items:center;"
            "justify-content:center;text-align:center}</style>"
            "<div>scope live view unreachable<br><span style='color:#4C5766'>%s</span></div>" % ip)


def _scope_blocked_html(reason: str) -> str:
    safe_reason = (reason or "target-state interlock is active").replace("&", "&amp;")
    safe_reason = safe_reason.replace("<", "&lt;").replace(">", "&gt;")
    return ("<!doctype html><meta charset=utf-8><style>html,body{height:100%;margin:0;background:#05070A;"
            "color:#F04B52;font:600 12px/1.6 ui-monospace,monospace;display:flex;align-items:center;"
            "justify-content:center;text-align:center}</style><div>TARGET STATE INTERLOCKED<br>"
            "<span style='color:#98A3B2;font-weight:400'>%s</span></div>" % safe_reason)


def _rig_busy_payload(exc: Exception, operation: str) -> dict:
    return {"ok": False, "refused": True, "reason": "rig_operation_in_progress",
            "violated_rule": "rig_operation_in_progress", "operation": operation,
            "detail": str(exc)}


def _json_object(value) -> dict:
    if isinstance(value, dict):
        return dict(value)
    if isinstance(value, (bytes, bytearray)):
        value = bytes(value).decode("utf-8", "replace")
    if isinstance(value, str):
        try:
            decoded = json.loads(value)
            return dict(decoded) if isinstance(decoded, dict) else {}
        except (TypeError, ValueError):
            return {}
    return {}


def _enrich_attempt_row(core, row: dict) -> dict:
    """Expose per-shot delivery/readback/connection facts without changing history."""
    params = _json_object(row.pop("params_json", None))
    aux = _json_object(row.pop("aux_telemetry", None))
    effective = dict(aux.get("effective_settings") or {})
    frozen = dict(effective.get("frozen_readback") or {})
    composite = _json_object(row.pop("oracle_detail", None))
    underlying = composite.get("underlying_connection") or composite.get("underlying_oracle") or {}
    if isinstance(underlying, dict) and isinstance(underlying.get("detail"), dict):
        raw_oracle = dict(underlying["detail"])
    elif isinstance(underlying, dict):
        raw_oracle = dict(underlying)
    else:
        raw_oracle = {}
    required = dict(composite.get("required_checks") or aux.get("required_checks") or {})
    explicit_valid = aux.get("attempt_valid")
    if explicit_valid is None:
        explicit_valid = composite.get("attempt_valid")
    attempt_valid = explicit_valid if isinstance(explicit_valid, bool) else None

    row["params"] = params
    row["aux_telemetry"] = aux
    row["attempt_valid"] = attempt_valid
    row["coverage_valid"] = attempt_valid is not False
    row["validity"] = ("valid" if attempt_valid is True else
                       "invalid_infrastructure" if attempt_valid is False else
                       "legacy_validity_unknown")
    row["requested"] = {
        "pulse_cycles": params.get("pulse_cycles", params.get("width", row.get("width"))),
        "ext_offset": params.get("ext_offset", params.get("offset", row.get("offset"))),
        "mosfet": params.get("mosfet"),
        "cell_shot": params.get("cell_shot"),
    }
    row["effective_settings"] = effective
    row["readback"] = {
        "pulse_cycles": effective.get("pulse_cycles_readback"),
        "ext_offset": effective.get("ext_offset_readback"),
        "mosfet": effective.get("mosfet_readback"),
    }
    row["phase"] = {
        "phase_shift_steps": frozen.get("phase_shift_steps"),
        "width_steps": frozen.get("phase_width_steps"),
        "offset_steps": frozen.get("phase_offset_steps"),
    }
    row["trigger"] = {
        "source": frozen.get("trigger_source"),
        "module": frozen.get("trigger_module"),
        "edge": frozen.get("trigger_edge"),
        "level_v": frozen.get("trigger_level_v"),
    }
    row["oracle_state"] = {
        "plugin": composite.get("plugin") or raw_oracle.get("plugin"),
        "outcome": composite.get("outcome") or raw_oracle.get("outcome"),
        "failure_stage": raw_oracle.get("failure_stage") or composite.get("failure_stage"),
        "highest_passed_stage": raw_oracle.get("highest_passed_stage"),
        "runtime_confirmed": raw_oracle.get("runtime_confirmed"),
        "protection_state_confirmed": raw_oracle.get("protection_state_confirmed"),
        "evidence_complete": composite.get("evidence_complete"),
    }
    row["required_evidence_checks"] = required
    row["required_evidence_failed"] = [key for key, value in required.items()
                                        if value is not True]
    row["required_evidence_passed"] = sum(value is True for value in required.values())
    row["required_evidence_total"] = len(required)
    row["waveform_available"] = bool(composite.get("candidate_dir"))
    if row.get("outcome_class") == "false-positive":
        partial = bool(raw_oracle.get("partial_candidate_observed"))
        shadow = bool(raw_oracle.get("protection_state_confirmed"))
        runtime = raw_oracle.get("runtime_confirmed")
        if partial and not shadow:
            row["outcome_detail"] = "partial_connection"
        elif shadow and runtime is False:
            row["outcome_detail"] = "target_response_failed_runtime"
        elif runtime is True:
            row["outcome_detail"] = "confirmation_evidence_failed"
        else:
            row["outcome_detail"] = "unconfirmed_response"
    elif row.get("outcome_class") == "no-effect":
        row["outcome_detail"] = "no_target_response"
    if row.get("outcome_class") == "success":
        row["classification"] = get_attempt_evidence_data(
            core, int(row["id"]), include_raw=False).get("classification")
    else:
        row["classification"] = "non_success"
    return row


def _compact_workflow_for_viewer(workflow: dict) -> dict:
    """Keep the browser status payload small without weakening evidence APIs.

    The MCP evidence endpoint intentionally returns complete nested connector records.  The
    readiness card only needs status and a handful of display fields; returning multi-megabyte
    preflight/connection captures on every refresh made historical campaign pages appear hung.
    """
    stages = []
    for stage in workflow.get("stages") or []:
        name = stage.get("name")
        detail = stage.get("detail") if isinstance(stage.get("detail"), dict) else {}
        if name == "project_profile":
            detail = {key: detail.get(key) for key in (
                "id", "display_name", "default_recipe", "fixed_phase", "connector"
            ) if key in detail}
        elif name == "target_acknowledgment":
            detail = {key: detail.get(key) for key in ("target_model", "required_limits")}
        elif name == "husky_connection":
            health = detail.get("health")
            connect_result = detail.get("connect_result")
            if isinstance(connect_result, dict):
                health = health or connect_result.get("health")
            detail = {key: detail.get(key) for key in (
                "id", "bound", "simulator", "serial_number", "firmware_version"
            ) if key in detail}
            if health is not None:
                detail["health"] = health
        elif name == "preflight":
            detail = {key: detail.get(key) for key in (
                "ok", "failure_stage", "error", "reason", "target_returned_off"
            ) if key in detail}
        elif name == "physical_timing":
            detail = {key: detail.get(key) for key in (
                "ok", "source", "captured_this_session", "project_evidence_owned",
                "preflight_validated", "suggested_offset"
            ) if key in detail}
        stages.append({key: value for key, value in stage.items() if key != "detail"}
                      | {"detail": detail})

    def evidence_ref(value):
        if not isinstance(value, dict):
            return None
        attempt = value.get("attempt") if isinstance(value.get("attempt"), dict) else {}
        return {key: item for key, item in {
            "attempt_id": value.get("attempt_id", value.get("id")),
            "classification": value.get("classification"),
            "fully_confirmed": value.get("fully_confirmed"),
            "candidate": value.get("candidate"),
            "outcome": attempt.get("outcome"),
            "seq": attempt.get("seq"),
        }.items() if item is not None}

    target_state = workflow.get("target_state") or {}
    if isinstance(target_state, dict):
        target_state = {key: target_state.get(key) for key in (
            "state", "blocking", "unknown_held", "reason", "source", "sweep_id", "attempt_id"
        ) if key in target_state}
    next_action = workflow.get("next_action") or {}
    if isinstance(next_action, dict):
        next_action = {key: next_action.get(key) for key in ("tool", "reason", "arguments")
                       if key in next_action}
    return {
        "ok": workflow.get("ok", True),
        "campaign_id": workflow.get("campaign_id"),
        "sweep_id": workflow.get("sweep_id"),
        "counts": workflow.get("counts") or {},
        "stages": stages,
        "latest_candidate": evidence_ref(workflow.get("latest_candidate")),
        "latest_confirmed": evidence_ref(workflow.get("latest_confirmed")),
        "target_state": target_state,
        "controls_blocked": bool(workflow.get("controls_blocked")),
        "next_action": next_action,
        "result_semantics": workflow.get("result_semantics") or {},
    }


def build_viewer(core=None) -> FastAPI:
    core = core or get_core()
    app = FastAPI(title="GlitchLab Viewer")

    if STATIC.exists():
        app.mount("/static", StaticFiles(directory=str(STATIC)), name="static")

    # ---- page ----
    @app.get("/", response_class=HTMLResponse)
    def index():
        return (TEMPLATES / "index.html").read_text(encoding="utf-8")

    # ---- bootstrap / read endpoints (invisible reads for the browser) ----
    @app.get("/api/bootstrap")
    def bootstrap():
        # Enrich `active` with the running sweep's plan + progress so the UI can mirror the campaign
        # exactly (param fields) and draw the full planned parameter space as a progress bar (D).
        active = dict(core.active)
        sid = active.get("sweep_id")
        if sid:
            sw = core.store.get_sweep(sid)
            if sw and sw.get("param_spec"):
                try:
                    active["param_spec"] = json.loads(sw["param_spec"])
                except Exception:
                    pass
            if sw:
                active["sweep_status"] = sw.get("status")
            t = core.sweep_engine.timing(sid)
            if t:
                active["timing"] = {"running": t.get("running"), "done": t.get("done"),
                                    "total": t.get("total")}
        profile_name, profile = _target_profile(core)
        return JSONResponse({
            "active": active,
            "danger_state": core.danger_state,
            "capability_manifest": core.capability_manifest(),
            "target_state": target_state_interlock(core),
            "campaigns": core.store.list_campaigns(core.active.get("project_id")),
            "scope": core.scope.status(),
            "scope_policy": companion_scope_policy(core),
            "taxonomy": core.store.outcome_classes(),
            "scope_webcontrol": config.SCOPE_WEBCONTROL_URL,
            "active_project": core.active.get("project_id"),
            "projects": core.store.projects_with_totals(),
            "project_profile": _profile_summary(core, profile_name, profile),
            "notifications": core.notifier.status(),
            "result_semantics": {
                "success": "candidate_unconfirmed unless verified connector evidence is complete",
                "confirmed": "verified success with the active project connector contract",
            },
        })

    @app.get("/api/parameter-profiles")
    def parameter_profiles(target_model: str | None = None):
        """Return real project recipes and stored profiles with verified provenance state."""
        model = target_model or core.rig.target_model
        profile_name, profile = _target_profile(core)
        profile_summary = _profile_summary(core, profile_name, profile)
        configured = []
        for name, recipe in (profile_summary.get("recipes") or {}).items():
            configured.append({
                "name": name,
                "target_model": model,
                "injection_type": "voltage",
                "parameters": recipe,
                "source": "active_project_profile",
                "status": ("published_replicated_prior" if name == "reproduce" and
                           (profile_summary.get("proven_result") or {}).get("verified_successes")
                           else "configured"),
                "proven_result": profile_summary.get("proven_result") if name == "reproduce" else {},
            })
        stored = []
        for row in core.store.get_known_good(model):
            provenance = row.get("provenance") or {}
            attempt_id = provenance.get("confirmed_attempt_id")
            evidence = (get_attempt_evidence_data(core, int(attempt_id), include_raw=False)
                        if attempt_id else {})
            stored.append({**row, "status": ("fully_confirmed" if evidence.get("fully_confirmed")
                                               else "unverified_provenance")})
        return JSONResponse({"target_model": model, "project_profile": profile_summary,
                             "configured": configured, "stored": stored})

    @app.get("/api/workflow")
    def workflow_state(campaign_id: str | None = None, sweep_id: str | None = None,
                       recent_attempts: int = 5):
        """Read-only normalized state used by the AI and the human status-gate card."""
        workflow = get_workflow_state_data(
            core, campaign_id=campaign_id, sweep_id=sweep_id,
            recent_attempts=max(1, min(recent_attempts, 25)))
        return JSONResponse(_compact_workflow_for_viewer(workflow))

    @app.get("/api/attempt/{attempt_id}/evidence")
    def attempt_evidence(attempt_id: int, include_raw: bool = False, max_raw_chars: int = 1200):
        return JSONResponse(get_attempt_evidence_data(
            core, attempt_id, include_raw=include_raw,
            max_raw_chars=max(100, min(max_raw_chars, 8000))))

    @app.get("/api/overview")
    def overview(project_id: str | None = None):
        pid = project_id or core.active.get("project_id")
        camps = core.store.list_campaigns(pid)
        per = []
        tot_a = tot_s = tot_candidate = tot_confirmed = 0
        for c in camps:
            a = core.store.fetch_one(
                "SELECT COUNT(*) n, SUM(CASE WHEN a.outcome_class='success' THEN 1 ELSE 0 END) s, "
                "SUM(CASE WHEN a.outcome_class='success' AND COALESCE(a.verified,0)=0 THEN 1 ELSE 0 END) candidates, "
                "SUM(CASE WHEN a.outcome_class='success' AND COALESCE(a.verified,0)=1 THEN 1 ELSE 0 END) confirmed "
                "FROM attempt a JOIN sweep sw ON a.sweep_id=sw.id JOIN session se "
                "ON sw.session_id=se.id WHERE se.campaign_id=?", (c["id"],))
            att = (a or {}).get("n", 0) or 0
            succ = (a or {}).get("s", 0) or 0
            verified_rows = core.store.fetch_all(
                "SELECT a.id FROM attempt a JOIN sweep sw ON a.sweep_id=sw.id "
                "JOIN session se ON sw.session_id=se.id WHERE se.campaign_id=? "
                "AND a.outcome_class='success' AND COALESCE(a.verified,0)=1 ORDER BY a.id DESC",
                (c["id"],))
            confirmed = sum(
                1 for vr in verified_rows
                if get_attempt_evidence_data(core, int(vr["id"]), include_raw=False).get("fully_confirmed")
            )
            candidates = max(0, int(succ) - confirmed)
            tot_a += att
            tot_s += succ
            tot_candidate += candidates
            tot_confirmed += confirmed
            t = core.store.fetch_one("SELECT model FROM target WHERE id=?", (c.get("target_id"),))
            # Is a sweep in this campaign LIVE right now? Previously the list gave no way to tell a
            # running campaign from a finished one — with 40+ campaigns accumulated, the active one
            # was indistinguishable at a glance. `running` drives the LIVE badge/pulse in the UI.
            # status='running' ALONE IS NOT ENOUGH: a killed/crashed process never updates the row,
            # so the DB currently holds 8 stale 'running' sweeps and 6 campaigns would falsely show
            # LIVE. Require a REAL attempt within the last 120 s — that tracks actual firing, and
            # stale rows age out on their own without rewriting anyone's history.
            _run = core.store.fetch_one(
                "SELECT sw.id id, sw.name name, "
                "  (julianday('now') - julianday(MAX(a.ts))) * 86400 AS age_s "
                "FROM sweep sw JOIN session se ON sw.session_id=se.id "
                "JOIN attempt a ON a.sweep_id=sw.id "
                "WHERE se.campaign_id=? AND sw.status='running' "
                "GROUP BY sw.id HAVING age_s IS NOT NULL AND age_s < 120 "
                "ORDER BY MAX(a.ts) DESC LIMIT 1",
                (c["id"],))
            per.append({**c, "attempts": att, "successes": succ,
                        "candidate_successes": candidates, "confirmed_successes": confirmed,
                        "target": (t or {}).get("model") or "unknown",
                        "running": bool(_run),
                        "running_sweep": (_run or {}).get("name")})
        projs = core.store.projects_with_totals()
        cur = next((p for p in projs if p["id"] == pid), None)
        return JSONResponse({"attempts": tot_a, "successes": tot_s,
                             "candidate_successes": tot_candidate,
                             "confirmed_successes": tot_confirmed, "campaigns": per,
                             "project_id": pid, "project_name": (cur or {}).get("name", "Default"),
                             "projects": projs})

    def _summary_json(sweep_id=None, campaign_id=None, x="width", y="offset"):
        try:
            summ = descriptor.build_summary(core.store, sweep_id, "success_rate", x, y,
                                            campaign_id=campaign_id)
            g = build_grid(core.store, sweep_id, x, y, campaign_id=campaign_id)
            summ["textmap"] = textart.success_rate_map(g)
            summ["categorical"] = textart.categorical_map(g)
            summ["totals_by_class"] = summ.get("totals", {})
            summ["sweep_meta"] = (core.store.get_sweep(sweep_id) if sweep_id
                                  else {"status": "campaign", "name": campaign_id})
            return summ
        except Exception as e:
            return {"error": str(e)}

    def _grid_json(sweep_id=None, campaign_id=None, x="width", y="offset"):
        columns = {"width": "a.width", "offset": "a.offset", "ext_offset": "a.offset",
                   "voltage": "a.voltage", "repeat": "a.repeat"}
        xcol, ycol = columns.get(x), columns.get(y)
        if xcol is None or ycol is None:
            return {"error": "unsupported grid axis", "allowed_axes": sorted(columns)}
        joins = ("FROM attempt a LEFT JOIN env_sample es ON es.id=(SELECT MAX(e2.id) "
                 "FROM env_sample e2 WHERE e2.attempt_id=a.id) ")
        if campaign_id:
            joins += ("JOIN sweep sw ON a.sweep_id=sw.id JOIN session se ON sw.session_id=se.id ")
            selection, args = "se.campaign_id=?", [campaign_id]
        else:
            selection, args = "a.sweep_id=?", [sweep_id]
        validity = ("COALESCE(CASE WHEN json_valid(es.aux_telemetry) "
                    "THEN json_extract(es.aux_telemetry,'$.attempt_valid') END,1)=1")
        # A raw `success` is only a candidate until its persisted connector
        # contract is verified.  The map must use the same vocabulary as the
        # attempt list, otherwise a green confirmed result can disappear into
        # an indistinguishable yellow aggregate cell.
        outcome = ("CASE WHEN a.outcome_class='success' AND COALESCE(a.verified,0)=1 THEN 'confirmed' "
                   "WHEN a.outcome_class='success' THEN 'candidate' ELSE a.outcome_class END")
        rows = core.store.fetch_all(
            f"SELECT {xcol} xv,{ycol} yv,{outcome} c,COUNT(*) n {joins}"
            f"WHERE {selection} AND {xcol} IS NOT NULL AND {ycol} IS NOT NULL AND {validity} "
            f"GROUP BY {xcol},{ycol},{outcome}", tuple(args))
        excluded = core.store.fetch_one(
            "SELECT COUNT(*) n " + joins + f"WHERE {selection} AND NOT ({validity})", tuple(args))
        xs = sorted({row["xv"] for row in rows})
        ys = sorted({row["yv"] for row in rows})
        cells = [{"x": row["xv"], "y": row["yv"], "c": row["c"], "n": int(row["n"])}
                 for row in rows]
        totals: dict[str, int] = {}
        for row in rows:
            totals[row["c"]] = totals.get(row["c"], 0) + int(row["n"])
        # Small runs are not heatmaps: each shot is returned separately so a
        # 13-shot fixed-point run becomes 13 visible marks, including its
        # individual confirmed/candidate states.  Larger runs stay compact and
        # are rendered from adaptive bins client-side.
        total = sum(totals.values())
        samples = []
        sample_limit = 96
        if total <= sample_limit:
            sample_rows = core.store.fetch_all(
                f"SELECT a.id,a.seq,{xcol} xv,{ycol} yv,{outcome} c {joins}"
                f"WHERE {selection} AND {xcol} IS NOT NULL AND {ycol} IS NOT NULL AND {validity} "
                "ORDER BY a.id", tuple(args))
            samples = [{"id": int(row["id"]), "seq": row["seq"], "x": row["xv"],
                        "y": row["yv"], "c": row["c"]} for row in sample_rows]
        return {"xs": xs, "ys": ys, "x_name": x, "y_name": y,
                "x_unit": "cyc" if x in {"width", "offset", "ext_offset"} else "",
                "y_unit": "cyc" if y in {"width", "offset", "ext_offset"} else "",
                "cells": cells, "totals": totals,
                "samples": samples, "sample_count": total, "samples_are_complete": total <= sample_limit,
                "coverage_semantics": "explicit attempt_valid=false rows excluded",
                "invalid_infrastructure_excluded": int((excluded or {}).get("n") or 0)}

    @app.get("/api/sweep/{sweep_id}/summary")
    def sweep_summary(sweep_id: str, x: str = "width", y: str = "offset"):
        return JSONResponse(_summary_json(sweep_id=sweep_id, x=x, y=y))

    @app.get("/api/campaign/{campaign_id}/summary")
    def campaign_summary(campaign_id: str, x: str = "width", y: str = "offset"):
        return JSONResponse(_summary_json(campaign_id=campaign_id, x=x, y=y))

    @app.get("/api/sweep/{sweep_id}/timing")
    def sweep_timing(sweep_id: str):
        return JSONResponse(core.sweep_engine.timing(sweep_id) or {"sweep_id": sweep_id,
                                                                    "running": False})

    @app.get("/api/sweep/{sweep_id}/grid")
    def sweep_grid(sweep_id: str, x: str = "width", y: str = "offset"):
        return JSONResponse(_grid_json(sweep_id=sweep_id, x=x, y=y))

    @app.get("/api/campaign/{campaign_id}/grid")
    def campaign_grid(campaign_id: str, x: str = "width", y: str = "offset"):
        return JSONResponse(_grid_json(campaign_id=campaign_id, x=x, y=y))

    @app.get("/api/paramspace/project")
    def project_paramspace(project_id: str | None = None):
        # (C) The KNOWN parameter space: union of every attempt across ALL sweeps in the project, so
        # the heatmap can show accumulated coverage/outcomes, not just the live campaign.
        pid = project_id or core.active.get("project_id")
        # NB: the attempt table has literal x/y/z columns, so aliasing to x/y and grouping by them
        # collapses to attempt.x. Group by the full json_extract EXPRESSIONS and alias to gx/gy.
        wexpr = "json_extract(a.params,'$.width')"
        yexpr = "COALESCE(json_extract(a.params,'$.ext_offset'), json_extract(a.params,'$.offset'))"
        validity_join = ("LEFT JOIN env_sample es ON es.id=(SELECT MAX(e2.id) FROM env_sample e2 "
                         "WHERE e2.attempt_id=a.id) ")
        validity = ("COALESCE(CASE WHEN json_valid(es.aux_telemetry) "
                    "THEN json_extract(es.aux_telemetry,'$.attempt_valid') END,1)=1")
        outcome = ("CASE WHEN a.outcome_class='success' AND COALESCE(a.verified,0)=1 THEN 'confirmed' "
                   "WHEN a.outcome_class='success' THEN 'candidate' ELSE a.outcome_class END")
        rows = core.store.fetch_all(
            f"SELECT {wexpr} AS gx, {yexpr} AS gy, {outcome} AS c, COUNT(*) AS n "
            "FROM attempt a JOIN sweep sw ON a.sweep_id=sw.id JOIN session se ON sw.session_id=se.id "
            "JOIN campaign ca ON se.campaign_id=ca.id " + validity_join +
            f"WHERE ca.project_id=? AND {validity} GROUP BY {wexpr}, {yexpr}, {outcome}", (pid,))
        xs = sorted({r["gx"] for r in rows if r["gx"] is not None})
        ys = sorted({r["gy"] for r in rows if r["gy"] is not None})
        cells = [{"x": r["gx"], "y": r["gy"], "c": r["c"], "n": int(r["n"])}
                 for r in rows if r["gx"] is not None and r["gy"] is not None]
        totals = {}
        for r in rows:
            totals[r["c"]] = totals.get(r["c"], 0) + int(r["n"])
        total = sum(totals.values())
        samples = []
        if total <= 96:
            sample_rows = core.store.fetch_all(
                f"SELECT a.id,a.seq,{wexpr} AS gx,{yexpr} AS gy,{outcome} AS c "
                "FROM attempt a JOIN sweep sw ON a.sweep_id=sw.id JOIN session se ON sw.session_id=se.id "
                "JOIN campaign ca ON se.campaign_id=ca.id " + validity_join +
                f"WHERE ca.project_id=? AND {validity} AND {wexpr} IS NOT NULL AND {yexpr} IS NOT NULL ORDER BY a.id", (pid,))
            samples = [{"id": int(r["id"]), "seq": r["seq"], "x": r["gx"], "y": r["gy"], "c": r["c"]}
                       for r in sample_rows]
        return JSONResponse({"xs": xs, "ys": ys, "x_name": "width", "y_name": "ext-offset",
                             "cells": cells, "totals": totals, "scope": "project", "project_id": pid,
                             "samples": samples, "sample_count": total, "samples_are_complete": total <= 96,
                             "coverage_semantics": "explicit attempt_valid=false rows excluded"})

    @app.get("/api/connectors")
    def connectors():
        """Hot-rescan workspace connector manifests for the sweep form."""
        from ..connections import connector_sdk_instructions, describe_connectors

        try:
            return JSONResponse({"ok": True, "connectors": describe_connectors(),
                                 "sdk": connector_sdk_instructions()})
        except Exception as exc:
            return JSONResponse({"ok": False, "connectors": [], "error": str(exc)}, status_code=400)

    @app.get("/api/campaign/{campaign_id}/attempts")
    def campaign_attempts(campaign_id: str, limit: int = 250, outcome: str | None = None):
        # LEFT JOIN env_sample so RELOADED rows keep their measured evidence. Without this the
        # dip/waveform only ever existed on the live websocket event and vanished on refresh —
        # i.e. the scope evidence looked absent for every historical attempt.
        base = ("FROM attempt a JOIN sweep sw ON a.sweep_id=sw.id JOIN session se "
                "ON sw.session_id=se.id LEFT JOIN env_sample es ON es.id=(SELECT MAX(e2.id) "
                "FROM env_sample e2 WHERE e2.attempt_id=a.id) "
                "WHERE se.campaign_id=?")
        args = [campaign_id]
        if outcome:
            base += " AND a.outcome_class=?"; args.append(outcome)
        rows = core.store.fetch_all(
            "SELECT a.id,a.seq,a.width,a.offset,a.voltage,a.repeat,a.outcome_class,"
            "a.outcome_confidence,a.duration_ms,a.verified,a.verdict_source,a.notes,a.params params_json,"
            "(SELECT GROUP_CONCAT(o.oracle_name || ':' || o.verdict, ' | ') "
            " FROM oracle_reading o WHERE o.attempt_id=a.id) oracle_summary,"
            "(SELECT o.detail FROM oracle_reading o WHERE o.attempt_id=a.id ORDER BY o.id LIMIT 1) oracle_detail,"
            f"es.aux_telemetry,es.scope_measurements {base} "
            "ORDER BY a.id DESC LIMIT ?",
            tuple(args + [limit]))
        # Flatten the stored scope JSON into the fields the table renderer expects.
        import json as _json
        for _r in rows:
            _sm = _r.pop("scope_measurements", None)
            try:
                _sm = _json.loads(_sm) if isinstance(_sm, str) else (_sm or {})
            except Exception:
                _sm = {}
            _dip = _sm.get("dip_min_V", _sm.get("observed_signal_min_v"))
            _baseline = _sm.get("observed_signal_baseline_v")
            _depth = _sm.get("dip_depth_V")
            if _depth is None and _dip is not None and _baseline is not None:
                _depth = float(_baseline) - float(_dip)
            _r["dip_min_V"] = _dip; _r["dip_depth_V"] = _depth
            _r["wave"] = _sm.get("wave"); _r["pin_wave"] = _sm.get("pin_wave")
            _r["pin_dip_min_V"] = _sm.get("pin_dip_min_V", _dip)
            _r.update(_physical_summary(_sm).get("summary") or {})
            _enrich_attempt_row(core, _r)
        total = core.store.fetch_one(
            "SELECT COUNT(*) n,SUM(CASE WHEN json_valid(es.aux_telemetry) AND "
            "json_extract(es.aux_telemetry,'$.attempt_valid')=0 THEN 1 ELSE 0 END) invalid "
            + base, tuple(args))
        invalid_page = sum(row.get("attempt_valid") is False for row in rows)
        total_n, invalid_n = int((total or {}).get("n") or 0), int((total or {}).get("invalid") or 0)
        return JSONResponse({"attempts": rows, "total": total_n,
                             "invalid": invalid_n, "coverage_valid": total_n - invalid_n,
                             "invalid_in_page": invalid_page,
                             "coverage_valid_in_page": len(rows) - invalid_page})

    @app.get("/api/sweep/{sweep_id}/map.png")
    def sweep_map_png(sweep_id: str, view: str = "success_rate"):
        out = config.FIGURE_DIR / f"map_{sweep_id}_{view}.png"
        image_render.parameter_map_png(core.store, sweep_id, view, out=out)
        return Response(out.read_bytes(), media_type="image/png")

    @app.get("/api/attempt/{attempt_id}/waveform")
    def attempt_waveform(attempt_id: int, max_points: int = 1600):
        """Lazy display trace from the immutable evidence artifact; never load it on page open."""
        rigol_dir = _attempt_waveform_dir(core, attempt_id)
        if rigol_dir is None:
            return JSONResponse({"ok": False, "error": "no persisted waveform for this attempt"},
                                status_code=404)
        try:
            payload = _cached_downsample_scope_artifacts(str(rigol_dir), max_points)
            return JSONResponse(payload, headers={"Cache-Control": "private, max-age=3600"})
        except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=422)

    @app.get("/api/attempts")
    def attempts(sweep_id: str, limit: int = 250, outcome: str | None = None):
        # LEFT JOIN env_sample so a page REFRESH keeps the measured evidence. This is the endpoint
        # the live-sweep view actually uses (the campaign endpoint is the other one) — without the
        # join the dip + waveform existed only on the live websocket event and vanished on reload.
        cols = ("a.id,a.seq,a.width,a.offset,a.voltage,a.repeat,a.outcome_class,"
                "a.outcome_confidence,a.duration_ms,a.verified,a.verdict_source,a.notes,a.params params_json,"
                "(SELECT GROUP_CONCAT(o.oracle_name || ':' || o.verdict, ' | ') "
                " FROM oracle_reading o WHERE o.attempt_id=a.id) oracle_summary,"
                "(SELECT o.detail FROM oracle_reading o WHERE o.attempt_id=a.id ORDER BY o.id LIMIT 1) oracle_detail,"
                "es.aux_telemetry,es.scope_measurements")
        where, args = "a.sweep_id=?", [sweep_id]
        if outcome:
            where += " AND a.outcome_class=?"; args.append(outcome)
        rows = core.store.fetch_all(
            f"SELECT {cols} FROM attempt a LEFT JOIN env_sample es ON es.id=(SELECT MAX(e2.id) "
            "FROM env_sample e2 WHERE e2.attempt_id=a.id) "
            f"WHERE {where} ORDER BY a.id DESC LIMIT ?", tuple(args + [limit]))
        import json as _json
        for _r in rows:
            _sm = _r.pop("scope_measurements", None)
            try:
                _sm = _json.loads(_sm) if isinstance(_sm, str) else (_sm or {})
            except Exception:
                _sm = {}
            _dip = _sm.get("dip_min_V", _sm.get("observed_signal_min_v"))
            _baseline = _sm.get("observed_signal_baseline_v")
            _depth = _sm.get("dip_depth_V")
            if _depth is None and _dip is not None and _baseline is not None:
                _depth = float(_baseline) - float(_dip)
            _r["dip_min_V"] = _dip; _r["dip_depth_V"] = _depth
            _r["wave"] = _sm.get("wave"); _r["pin_wave"] = _sm.get("pin_wave")
            _r["pin_dip_min_V"] = _sm.get("pin_dip_min_V", _dip)
            _r.update(_physical_summary(_sm).get("summary") or {})
            _enrich_attempt_row(core, _r)
        total = core.store.fetch_one(
            "SELECT COUNT(*) n,SUM(CASE WHEN json_valid(es.aux_telemetry) AND "
            "json_extract(es.aux_telemetry,'$.attempt_valid')=0 THEN 1 ELSE 0 END) invalid "
            "FROM attempt a LEFT JOIN env_sample es ON es.id=(SELECT MAX(e2.id) FROM env_sample e2 "
            f"WHERE e2.attempt_id=a.id) WHERE {where}", tuple(args))
        invalid_page = sum(row.get("attempt_valid") is False for row in rows)
        total_n, invalid_n = int((total or {}).get("n") or 0), int((total or {}).get("invalid") or 0)
        return JSONResponse({"attempts": rows, "total": total_n,
                             "invalid": invalid_n, "coverage_valid": total_n - invalid_n,
                             "invalid_in_page": invalid_page,
                             "coverage_valid_in_page": len(rows) - invalid_page})

    @app.get("/api/stats")
    def stats(metric: str, sweep_id: str | None = None, campaign_id: str | None = None,
              target_model: str | None = None):
        if metric == "bootstrap_confidence":
            if not sweep_id:
                return JSONResponse({"error": "sweep_id required"})
            return JSONResponse(stats_mod.bootstrap_confidence(core.store, sweep_id))
        fn = stats_mod.METRICS.get(metric)
        if not fn:
            return JSONResponse({"error": "unknown metric"})
        if metric in ("per_unit_variance", "drift"):
            return JSONResponse(fn(core.store, target_model or core.rig.target_model))
        return JSONResponse(fn(core.store, sweep_id, None, campaign_id))

    @app.get("/api/audit")
    def audit(limit: int = 30):
        return JSONResponse({"audit": core.store.recent_audit(limit)})

    @app.get("/scope/live", response_class=HTMLResponse)
    async def scope_live():
        """control.html re-served from our origin, with the blocking alert() neutralised (see above)."""
        policy = companion_scope_policy(core)
        if not policy["companion_access_allowed"]:
            return HTMLResponse(_scope_blocked_html(policy["reason"]), status_code=409)
        ip = config.SCOPE_HINT_IP
        try:
            html = await asyncio.to_thread(_build_scope_live_html, ip)
            _SCOPE_LIVE_CACHE["html"] = html
        except Exception:
            html = _SCOPE_LIVE_CACHE.get("html")  # fall back to the last good copy if the device blips
            if not html:
                return HTMLResponse(_scope_offline_html(ip))
        # never cache: always reflect the device's current control.html
        return HTMLResponse(html, headers={"Cache-Control": "no-store"})

    @app.get("/api/scope/reachable")
    async def scope_reachable():
        """Is the scope's web server answering right now? Lets the live-view overlay tell a real outage
        ('unreachable — retrying') apart from another window stealing the single video socket ('reclaim')."""
        ip = config.SCOPE_HINT_IP
        policy = companion_scope_policy(core)
        if not policy["companion_access_allowed"]:
            return JSONResponse({"reachable": False, "refused": True,
                                 "reason": "companion_scope_access_refused",
                                 "policy": policy}, status_code=409)
        try:
            await asyncio.to_thread(_fetch_text, "http://%s/res/config.js" % ip, 3.0)
            return JSONResponse({"reachable": True})
        except Exception:
            return JSONResponse({"reachable": False})

    @app.get("/api/scope/screenshot.png")
    async def scope_shot():
        if not companion_scope_policy(core)["companion_capture_allowed"]:
            return Response(b"", media_type="image/png", status_code=409,
                            headers={"X-GlitchLab-Refusal": "rigol-capture-owned"})
        if not core.scope.bound:
            return Response(b"", media_type="image/png", status_code=204)
        try:
            async with core.exclusive_rig_operation("viewer_scope_screenshot"):
                png = await core.scope.screenshot()
        except Exception as exc:
            from ..app_core import RigBusyError
            if isinstance(exc, RigBusyError):
                return JSONResponse(_rig_busy_payload(exc, "viewer_scope_screenshot"), status_code=409)
            return Response(b"", media_type="image/png", status_code=204)
        return Response(png, media_type="image/png")

    # ---- WebSocket: live events + UI command channel ----
    @app.websocket("/ws")
    async def ws(sock: WebSocket):
        await sock.accept()
        client_id = uuid.uuid4().hex[:8]
        cmd_q = core.uibus.register(client_id)
        ev_q = core.bus.subscribe()
        await sock.send_json({"type": "hello", "client_id": client_id,
                              "active": core.active, "danger_state": core.danger_state})

        async def pump_events():
            try:
                while True:
                    ev = await ev_q.get()
                    await sock.send_json({"type": "event", "kind": ev.kind, "data": ev.data,
                                          "seq": ev.seq})
            except Exception:
                pass

        async def pump_commands():
            try:
                while True:
                    cmd = await cmd_q.get()
                    await sock.send_json(cmd)
            except Exception:
                pass

        ev_task = asyncio.create_task(pump_events())
        cmd_task = asyncio.create_task(pump_commands())
        try:
            while True:
                msg = await sock.receive_json()
                await _handle_ws_message(core, msg)
        except WebSocketDisconnect:
            pass
        except Exception:
            pass
        finally:
            ev_task.cancel(); cmd_task.cancel()
            core.bus.unsubscribe(ev_q)
            core.uibus.unregister(client_id)

    return app


async def _handle_ws_message(core, msg: dict) -> None:
    mtype = msg.get("type")
    if mtype == "ack":
        core.uibus.resolve_ack(msg.get("id"), msg.get("ack", {}))
    elif mtype == "state":
        core.uibus.note_state(msg.get("state", {}))
    elif mtype == "action":
        await _handle_action(core, msg.get("name"), msg.get("payload") or {})


async def _handle_action(core, name: str, payload: dict) -> None:
    """A UI button was activated (by a human OR by a visible-MCP synthetic click)."""
    store = core.store
    if name in {"start_sweep", "preflight_check", "discover_timing", "read_connector",
                "bind_scope", "unbind_scope"}:
        refusal = target_state_refusal(core, f"viewer:{name}")
        if refusal:
            core.auditor.record(name, "CAUTION", payload, "refused",
                                violated_rule="preserved_target_state_interlock")
            core.bus.publish("action_refused", {"action": name, **refusal})
            return
    if name == "start_sweep":
        await _start_sweep_from_ui(core, payload)
    elif name == "pause_sweep":
        sid = payload.get("sweep_id") or core.active.get("sweep_id")
        core.sweep_engine.pause(sid)
    elif name == "resume_sweep":
        sid = payload.get("sweep_id") or core.active.get("sweep_id")
        core.sweep_engine.resume(sid)
    elif name == "stop_sweep":
        sid = payload.get("sweep_id") or core.active.get("sweep_id")
        core.sweep_engine.stop(sid)
    elif name == "acknowledge_target":
        core.acknowledge_target(payload.get("target_model"), payload.get("stated") or {})
    elif name == "test_notification":
        accepted = core.notifier.post(
            "GlitchLab notification delivery test.",
            title="GlitchLab test",
            priority=3,
            tags=["test_tube"],
        )
        core.auditor.record("test_notification", "SAFE", {},
                            "queued" if accepted else "refused",
                            violated_rule=None if accepted else "notifications_disabled")
        core.bus.publish("notification_test", {
            "accepted": accepted,
            "status": core.notifier.status(),
        })
    elif name == "save_notification_settings":
        try:
            core.configure_notifications(
                enabled=bool(payload.get("enabled")),
                topic=str(payload.get("topic") or ""),
                base_url=str(payload.get("base_url") or "https://ntfy.sh"),
            )
        except Exception as exc:
            core.bus.publish("action_refused", {
                "action": name, "reason": str(exc), "violated_rule": "invalid_notification_settings"
            })
    elif name == "preflight_check":
        from ..app_core import RigBusyError
        from ..domain.preflight import Preflight
        try:
            async with core.exclusive_rig_operation("viewer_preflight_check"):
                res = await Preflight(core).check()
        except RigBusyError as exc:
            res = _rig_busy_payload(exc, "viewer_preflight_check")
        core.bus.publish("preflight_result", res)
    elif name == "discover_timing":
        policy = companion_scope_policy(core)
        if policy["project_evidence_owned"]:
            core.auditor.record("discover_timing", "CAUTION", payload, "refused",
                                violated_rule="project_evidence_owns_rigol")
            core.bus.publish("action_refused", {
                "action": name,
                "reason": "project evidence owns the Rigol; use project preflight timing",
                "policy": policy,
            })
        else:
            from ..app_core import RigBusyError
            from ..domain.timing_discovery import TimingDiscovery
            try:
                async with core.exclusive_rig_operation("viewer_discover_timing"):
                    res = await TimingDiscovery(core).characterize(
                        window_s=payload.get("window_s"), trigger_channel=payload.get("trigger_channel"),
                        signal_channel=payload.get("signal_channel"))
            except RigBusyError as exc:
                res = _rig_busy_payload(exc, "viewer_discover_timing")
            core.bus.publish("timing_result", res)
    elif name in {"sensitivity_scan", "capture_adc", "capture_glitched",
                  "disruption_scan", "run_handoff"}:
        # These legacy WebSocket-only paths bypassed the MCP safety/confirmation contracts.
        # Keep the wire name recognizable for old clients, but fail closed.
        core.auditor.record(name, "CAUTION", payload, "refused",
                            violated_rule="use_guarded_mcp_tool")
        core.bus.publish("action_refused", {
            "action": name,
            "reason": "legacy direct hardware action disabled; use the guarded MCP workflow",
        })
    elif name == "read_connector":
        # Make one preservation-safe, read-only observation through the active
        # private connector. Target meaning remains entirely connector-owned.
        import asyncio as _asyncio
        def _do(g):
            try:
                connection = getattr(g, "connection", None)
                capabilities = getattr(connection, "capabilities", None)
                unsafe = any(bool(getattr(capabilities, attr, False)) for attr in (
                    "target_memory_writes", "persistent_target_writes", "target_reset",
                    "target_halt", "target_resume",
                )) if capabilities is not None else True
                if (
                    connection is None
                    or capabilities is None
                    or not bool(getattr(capabilities, "read_only", False))
                    or unsafe
                ):
                    return {"verdict": "exception", "confirmed": False,
                            "error": "active connector is not preservation-safe and read-only"}
                reading = connection.read({"phase": "current_state_inspection"})
                d = dict(reading.detail or {})
                d["verdict"] = reading.verdict
                d["latency_ms"] = round(float(reading.latency_ms or 0.0), 1)
                return d
            except Exception as e:  # noqa
                return {"verdict": "exception", "confirmed": False, "error": str(e)[:200]}

        from ..app_core import RigBusyError
        try:
            async with core.exclusive_rig_operation("viewer_connector_read_current_state"):
                g = await _asyncio.to_thread(core.ensure_glitcher, True)
                res = await _asyncio.to_thread(_do, g)
        except RigBusyError as exc:
            res = _rig_busy_payload(exc, "viewer_connector_read_current_state")
        except Exception as exc:  # connection failures are evidence, not websocket failures
            res = {"ok": False, "verdict": "exception", "confirmed": False,
                   "failure_stage": "connection", "error": str(exc)[:200]}
        busy = res.get("reason") == "rig_operation_in_progress"
        core.auditor.record("connector_read_current_state", "CAUTION", {},
                            "refused" if busy else "executed",
                            violated_rule="rig_operation_in_progress" if busy else None,
                            result={"verdict": res.get("verdict"),
                                    "confirmed": bool(res.get("confirmed"))})
        core.bus.publish("connector_read", res)
    elif name in {"legacy_power_glitch", "legacy_set_reset", "glitch_introspect", "set_target_timing",
                  "bind_dip_scope", "unbind_dip_scope"}:
        core.auditor.record(name, "CAUTION", payload, "refused",
                            violated_rule="legacy_direct_mutation_disabled")
        core.bus.publish("action_refused", {
            "action": name,
            "reason": "legacy direct rig mutation disabled; use the project profile and guarded MCP tools",
        })
    elif name == "bind_scope":
        policy = companion_scope_policy(core)
        if not policy["bind_allowed"]:
            core.auditor.record("bind_scope", "CAUTION", payload, "refused",
                                violated_rule="rigol_session_owned")
            core.bus.publish("action_refused", {
                "action": name, "reason": policy["reason"], "policy": policy,
            })
        else:
            from ..app_core import RigBusyError
            lease_created = False
            try:
                async with core.exclusive_rig_operation("viewer_scope_bind"):
                    lease, lease_created = acquire_companion_device_lease(core)
                    res = await core.scope.bind(hint_ip=payload.get("hint_ip"))
            except RigBusyError as exc:
                res = _rig_busy_payload(exc, "viewer_scope_bind")
            except Exception as exc:
                if lease_created:
                    release_companion_device_lease(core)
                res = {"ok": False, "refused": True,
                       "reason": "device_ownership_unavailable", "detail": str(exc)}
            if not res.get("ok") and lease_created:
                release_companion_device_lease(core)
            if res.get("ok"):
                res["device_lease"] = lease
            core.bus.publish("scope_bound" if res.get("ok") else "action_refused",
                             res if res.get("ok") else {"action": name, **res})
    elif name == "unbind_scope":
        was_bound = bool(core.scope.bound)
        from ..app_core import RigBusyError
        try:
            async with core.exclusive_rig_operation("viewer_scope_unbind"):
                await core.scope.unbind()
                lease_released = release_companion_device_lease(core)
        except RigBusyError as exc:
            core.bus.publish("action_refused", {
                "action": name, **_rig_busy_payload(exc, "viewer_scope_unbind")})
            return
        core.auditor.record("unbind_scope", "SAFE", {"was_bound": was_bound}, "executed")
        core.bus.publish("scope_unbound", {"was_bound": was_bound,
                                           "device_lease_released": lease_released})
    elif name == "new_campaign":
        project = core.rig.project_profile
        target = project.get("target") or {}
        requested_target = payload.get("target_model") or core.rig.target_model
        if requested_target != core.rig.target_model:
            core.bus.publish("action_refused", {
                "action": name,
                "reason": "live campaign target must match the server-selected project profile",
                "active_target_model": core.rig.target_model,
            })
            return
        tid = store.get_or_create_target(
            requested_target,
            payload.get("vendor") or target.get("vendor", ""),
            payload.get("package") or core.rig.rig.get("target_package", ""), ["voltage"])
        cid = store.create_campaign(payload.get("name", "campaign"),
                                    payload.get("objective", ""), tid,
                                    project_id=core.active.get("project_id"))
        core.active.update({"campaign_id": cid, "target_id": tid})
        core.active.pop("ui_validated_sweep_id", None)
        core.bus.publish("campaign_opened", {"campaign_id": cid})
    elif name == "set_project":
        pid = payload.get("project_id")
        if pid == core.config_project_id:
            core.active.update({"project_id": pid, "campaign_id": None, "session_id": None})
            core.active.pop("ui_validated_sweep_id", None)
            core.bus.publish("project_changed", {"project_id": pid, "profile_unchanged": True})
        else:
            core.bus.publish("action_refused", {
                "action": name,
                "reason": "restart with --project-profile; a database ID cannot switch live hardware/connector configuration",
            })
    elif name == "new_project":
        pid = store.create_project(payload.get("name", "New project"), payload.get("notes", ""))
        core.bus.publish("project_namespace_created", {
            "project_id": pid, "created": True, "active": False,
            "note": "analysis namespace only; restart with --project-profile for live use",
        })
    elif name == "record_note":
        sid = core.active.get("sweep_id")
        if sid:
            store.set_sweep_measurement_state(sid, "functional_unquantified")
            store.record_attempt(sid, {}, payload.get("outcome") or "no-data", 1.0,
                                 verdict_source="manual", verified=False,
                                 notes=payload.get("notes", ""))


async def _start_sweep_from_ui(core, payload: dict) -> None:
    """Validate the editable form, then build/reuse its immutable persisted sweep.

    A running sweep remains immutable, but the operator is never trapped in a preset: edits create
    the next plan.  Live execution still requires the exact edited plan to pass dry-run first.
    """
    import math

    store = core.store
    project = core.rig.project_profile
    recipes = project.get("recipes") or {}
    recipe_name = str(payload.get("recipe") or "default")
    recipe = dict(
        recipes.get(recipe_name)
        or recipes.get("reproduce")
        or recipes.get("local_refine")
        or recipes.get("discovery")
        or {}
    )
    recipe_axes = json.loads(json.dumps(recipe.get("axes") or {}))

    def _bounds(axis, fallback=None):
        if isinstance(axis, list) and axis:
            values = [float(value) for value in axis]
            return min(values), max(values), abs(values[1] - values[0]) if len(values) > 1 else 1.0
        if isinstance(axis, dict):
            lo = axis.get("min", axis.get("lo", axis.get("start", fallback)))
            hi = axis.get("max", axis.get("hi", axis.get("stop", lo)))
            return float(lo), float(hi), float(axis.get("step", 1))
        if axis is not None:
            value = float(axis)
            return value, value, 1.0
        if fallback is not None:
            value = float(fallback)
            return value, value, 1.0
        return None, None, 1.0

    default_width = _bounds(recipe_axes.get("pulse_cycles"), 1)
    default_offset = _bounds(recipe_axes.get("ext_offset"), 0)
    try:
        width_min = float(payload.get("width_min", default_width[0]))
        width_max = float(payload.get("width_max", default_width[1]))
        width_step = float(payload.get("width_step", default_width[2]))
        offset_min = float(payload.get("offset_min", default_offset[0]))
        offset_max = float(payload.get("offset_max", default_offset[1]))
        offset_step = float(payload.get("offset_step", default_offset[2]))
        repeats = int(payload.get(
            "repeats",
            recipe.get("repeats_per_cell", recipe.get("samples_per_cell", 1)),
        ))
        requested_seed = int(payload.get("random_seed", recipe.get("random_seed") or 0))
    except (TypeError, ValueError):
        core.bus.publish("action_refused", {
            "action": "start_sweep", "reason": "sweep parameters must be numeric",
            "violated_rule": "viewer_sweep_parse_error",
        })
        return
    numeric = (width_min, width_max, width_step, offset_min, offset_max, offset_step)
    if (not all(math.isfinite(value) for value in numeric)
            or width_min > width_max or offset_min > offset_max
            or width_step <= 0 or offset_step <= 0 or repeats < 1):
        core.bus.publish("action_refused", {
            "action": "start_sweep",
            "reason": "use finite ascending ranges, positive steps, and at least one repeat",
            "violated_rule": "viewer_sweep_range_invalid",
        })
        return

    requested_mosfet = payload.get("mosfet")
    if isinstance(requested_mosfet, list):
        requested_mosfet = requested_mosfet[0] if len(requested_mosfet) == 1 else None
    if requested_mosfet in (None, ""):
        configured = recipe_axes.get("mosfet")
        requested_mosfet = configured[0] if isinstance(configured, list) and len(configured) == 1 else configured

    axes = recipe_axes
    axes["pulse_cycles"] = {"min": width_min, "max": width_max, "step": width_step}
    axes["ext_offset"] = {"min": offset_min, "max": offset_max, "step": offset_step}
    if requested_mosfet not in (None, ""):
        axes["mosfet"] = [requested_mosfet]
    spec = {
        "axes": axes,
        "repeats_per_cell": repeats,
        "random_seed": requested_seed,
        "shuffle": bool(payload.get("shuffle", recipe.get("shuffle", False))),
        "scope_capture_every": 0,
        "stop_on_success": bool(payload.get("stop_on_success", recipe.get("stop_on_success", True))),
        "stop_on_infrastructure_error": True,
        "recipe_name": recipe_name,
        "recipe_profile_id": project.get("id"),
    }
    try:
        core.sweep_engine.build_points(spec)
    except (TypeError, ValueError) as exc:
        core.bus.publish("action_refused", {
            "action": "start_sweep", "reason": str(exc),
            "violated_rule": "viewer_sweep_plan_invalid",
        })
        return
    from ..connections import resolve_connector_selection

    try:
        connector_payload = payload.get("connector")
        if not isinstance(connector_payload, dict):
            connector_payload = None
        spec["connector"] = resolve_connector_selection(project, connector_payload)
    except Exception as exc:
        core.bus.publish("action_refused", {
            "action": "start_sweep", "reason": str(exc),
            "violated_rule": "connector_selection_invalid",
        })
        return
    active_project_id = core.config_project_id
    if core.active.get("project_id") != active_project_id:
        core.active.update({"project_id": active_project_id, "campaign_id": None,
                            "session_id": None, "sweep_id": None})
    requested_model = payload.get("target_model") or core.rig.target_model
    if requested_model != core.rig.target_model:
        core.bus.publish("action_refused", {
            "action": "start_sweep",
            "reason": "viewer target does not match the server-selected project profile",
            "violated_rule": "live_target_profile_mismatch",
            "requested_target_model": requested_model,
            "active_target_model": core.rig.target_model,
        })
        return
    requested_campaign_id = payload.get("campaign_id")
    cid = core.active.get("campaign_id")
    if requested_campaign_id:
        selected = store.get_campaign(str(requested_campaign_id))
        if selected and selected.get("project_id") == active_project_id:
            if cid != selected["id"]:
                core.active.update({"campaign_id": selected["id"],
                                    "target_id": selected.get("target_id"),
                                    "session_id": None, "sweep_id": None})
            cid = selected["id"]
        elif selected:
            core.bus.publish("action_refused", {
                "action": "start_sweep",
                "reason": "the viewed campaign belongs to a different rig project; browsing is read-only",
                "violated_rule": "viewed_project_not_active_rig",
            })
            return
    if not cid:
        model = core.rig.target_model
        tid = store.get_or_create_target(model, "", core.rig.rig.get("target_package", ""), ["voltage"])
        pid = active_project_id
        cid = store.create_campaign(payload.get("campaign_name", "Live campaign"),
                                    "voltage glitch", tid, project_id=pid)
        core.active.update({"campaign_id": cid, "target_id": tid})
    sid = core.active.get("session_id")
    if not sid:
        uid = store.create_unit(core.active["target_id"],
                                payload.get("unit_serial") or f"{core.rig.target_model}-viewer")
        sid = store.create_session(
            cid, uid, payload.get("operator", "viewer"),
            rig_config=core.run_configuration_snapshot(),
        )
        core.active.update({"session_id": sid, "unit_id": uid})
    if bool(payload.get("dry_run", False)):
        kind = "fixed-point" if width_min == width_max and offset_min == offset_max else "grid"
        swid = store.create_sweep(sid, kind, spec,
                                  name=payload.get("sweep_name", "viewer sweep plan"))
        core.active.update({"sweep_id": swid})
        core.bus.publish("sweep_defined", {"sweep_id": swid, "spec": spec})
        result = await core.sweep_engine.run_sweep(
            swid, None, dry_run=True, max_attempts=payload.get("max_attempts"))
        if result.get("ok"):
            core.active["ui_validated_sweep_id"] = swid
        core.bus.publish("sweep_dry_run", {"sweep_id": swid, "result": result})
        return

    # A browser live start must execute the exact immutable sweep which previously passed dry-run.
    swid = core.active.get("ui_validated_sweep_id")
    sweep = store.get_sweep(swid) if swid else None
    try:
        stored_spec = json.loads(sweep.get("param_spec") or "{}") if sweep else None
    except (TypeError, ValueError):
        stored_spec = None
    if stored_spec != spec:
        core.bus.publish("action_refused", {
            "action": "start_sweep",
            "reason": "this exact immutable form plan has not passed dry-run; enable DRY-RUN and start first",
        })
        return
    core.active.update({"sweep_id": swid})
    result = core.sweep_engine.start(swid, None, dry_run=False,
                                     max_attempts=payload.get("max_attempts"))
    if not result.get("ok"):
        core.bus.publish("action_refused", {"action": "start_sweep", **result})
