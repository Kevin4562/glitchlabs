"""Instrument tools — the oscilloscope (spec §11.4). SAFE capture/measure + DANGER source/probe.

All funnel through the single ScopeAdapter session (§16.2). Operations that need an instrument
return a clear not-bound result; DANGER tools additionally require enforced_limits or fail closed.
"""
from __future__ import annotations

import time
from pathlib import Path
from typing import Annotated, Any

import numpy as np
from pydantic import Field

from . import anns, meta
from .rig_state import target_state_interlock
from .. import config


def _rig_busy(exc: Exception, operation: str) -> dict:
    return {"ok": False, "refused": True, "reason": "rig_operation_in_progress",
            "violated_rule": "rig_operation_in_progress", "operation": operation,
            "detail": str(exc)}


def acquire_companion_device_lease(core) -> tuple[dict, bool]:
    """Acquire the process-wide/cross-process rig lease before LAN scope I/O."""
    if getattr(core, "device_lease", None) is not None:
        return {"ok": True, "already_owned": True}, False
    from ..io.device_lease import (DeviceLease, DeviceOwnershipError,
                                   find_device_owner_conflicts)

    conflicts = find_device_owner_conflicts()
    if conflicts:
        raise DeviceOwnershipError(
            "other processes may own Husky/J-Link/Rigol: "
            + "; ".join(
                f"PID {row['pid']} {row['name']}: {row['command']}"
                for row in conflicts
            )
        )
    lease = DeviceLease(config.DATA_DIR / "live-rig.lock")
    result = lease.acquire()
    core.device_lease = lease
    return result, True


def release_companion_device_lease(core) -> bool:
    """Release a scope-only lease; a connected glitcher continues to own it."""
    try:
        glitcher_bound = bool(core.glitcher_bound())
    except Exception:
        glitcher = getattr(core, "glitcher", None)
        glitcher_bound = bool(glitcher is not None and getattr(glitcher, "connected", False))
    lease = getattr(core, "device_lease", None)
    scope = getattr(core, "scope", None)
    scope_bound = bool(scope is not None and getattr(scope, "bound", False))
    if glitcher_bound or scope_bound or lease is None:
        return False
    lease.release()
    core.device_lease = None
    return True


def companion_scope_policy(core) -> dict:
    """Fail-closed ownership policy for the optional companion SCPI session."""
    target_state = target_state_interlock(core)
    profile = getattr(core.rig, "project_profile", {}) or {}
    evidence = profile.get("evidence") or {}
    required = set(evidence.get("required_for_success") or [])
    project_evidence_owned = bool(evidence.get("rigol")) and (
        not required or "rigol" in required
    )
    glitcher = getattr(core, "glitcher", None)
    try:
        glitcher_bound = bool(core.glitcher_bound())
    except Exception:
        glitcher_bound = bool(glitcher is not None and getattr(glitcher, "connected", False))
    non_simulator_glitcher_bound = bool(
        glitcher_bound and not getattr(glitcher, "is_simulator", False)
    )
    sweep_id = (getattr(core, "active", {}) or {}).get("sweep_id")
    try:
        sweep_running = bool(sweep_id and core.sweep_engine.is_running(sweep_id))
    except Exception:
        sweep_running = False
    evidence_sweep_active = bool(project_evidence_owned and sweep_running)
    bind_allowed = (
        not target_state["blocking"]
        and not project_evidence_owned
        and not non_simulator_glitcher_bound
    )
    access_allowed = not target_state["blocking"] and not project_evidence_owned
    reason = None
    if target_state["blocking"]:
        reason = target_state["reason"] or "target state is preserved or unknown-held"
    elif project_evidence_owned:
        reason = "active project evidence collector owns the Rigol session"
    elif non_simulator_glitcher_bound:
        reason = "a live non-simulator glitcher is bound"
    return {
        "project_evidence_owned": project_evidence_owned,
        "non_simulator_glitcher_bound": non_simulator_glitcher_bound,
        "evidence_sweep_active": evidence_sweep_active,
        "bind_allowed": bind_allowed,
        "companion_access_allowed": access_allowed,
        "companion_capture_allowed": access_allowed and not evidence_sweep_active,
        "reason": reason,
        "target_state": target_state,
    }


def register(srv, core):
    scope = core.scope
    store = core.store

    @srv.tool(name="describe_instrument", description="IDN, model, capabilities, bind status, safety "
              "limits in force + probed AWG source syntax (spec §11.4).",
              annotations=anns(read_only=True), meta=meta("SAFE", "invisible", 800))
    def describe_instrument() -> dict:
        return {"status": scope.status(), "capabilities": scope.caps,
                "source_syntax": scope.source_syntax,
                "rated_max_input_v": scope.rated_max_input_v(),
                "instruments_bound": store.get_instruments(),
                "companion_policy": companion_scope_policy(core)}

    @srv.tool(name="scope_discover", description="Run optional companion-scope discovery on the "
              "instrument subnet (spec §16.3). Refuses when project evidence owns the Rigol or target "
              "state is preserved/unknown-held.", annotations=anns(read_only=True, open_world=True),
              meta=meta("SAFE", "invisible", 500))
    async def scope_discover(
        hint_ip: Annotated[str | None, Field(description=
            "Optional IPv4 address to probe first; omit to use configured subnet discovery.")] = None,
    ) -> dict:
        policy = companion_scope_policy(core)
        if not policy["companion_access_allowed"]:
            return {"ok": False, "refused": True, "reason": "rigol_session_owned",
                    "detail": policy["reason"], "policy": policy}
        from ..app_core import RigBusyError
        try:
            async with core.exclusive_rig_operation("scope_discover"):
                return await scope.discover(hint_ip)
        except RigBusyError as exc:
            return _rig_busy(exc, "scope_discover")

    @srv.tool(name="scope_bind", description="Bind the optional companion SCPI session from a resolved "
              "resource (or auto-discovery). Refuses when a live non-simulator glitcher is bound or "
              "the active project's evidence collector owns the Rigol, and acquires the shared "
              "cross-process device lease before opening the LAN session.",
              annotations=anns(open_world=True), meta=meta("SAFE", "control", 800))
    async def scope_bind(
        resource: Annotated[str | None, Field(description=
            "Resolved VISA/SCPI resource string to bind; omit to discover from hint_ip or rig config.")] = None,
        hint_ip: Annotated[str | None, Field(description=
            "Optional IPv4 discovery hint used only when resource is omitted.")] = None,
    ) -> dict:
        policy = companion_scope_policy(core)
        if not policy["bind_allowed"]:
            core.auditor.record("scope_bind", "CAUTION", {"resource": resource, "hint_ip": hint_ip},
                                "refused", violated_rule="rigol_session_owned")
            return {"ok": False, "refused": True, "reason": "rigol_session_owned",
                    "detail": policy["reason"], "policy": policy}
        from ..app_core import RigBusyError
        lease_created = False
        try:
            async with core.exclusive_rig_operation("scope_bind"):
                lease, lease_created = acquire_companion_device_lease(core)
                res = await scope.bind(resource, hint_ip)
        except RigBusyError as exc:
            return _rig_busy(exc, "scope_bind")
        except Exception as exc:
            if lease_created:
                release_companion_device_lease(core)
            return {"ok": False, "refused": True,
                    "reason": "device_ownership_unavailable", "detail": str(exc)}
        if not res.get("ok") and lease_created:
            release_companion_device_lease(core)
        if res.get("ok"):
            core.bus.publish("scope_bound", {"idn": res.get("idn"),
                                             "resource": res.get("resource"),
                                             "device_lease": lease})
        return res

    @srv.tool(name="scope_unbind", title="Release the companion Rigol session",
              description="Explicitly close GlitchLab's optional companion SCPI session. Use this when "
              "project evidence or a live glitcher must take exclusive ownership. It releases a "
              "scope-only device lease after close, but refuses while target state is held.",
              annotations=anns(idempotent=True), meta=meta("SAFE", "control", 300))
    async def scope_unbind() -> dict:
        policy = companion_scope_policy(core)
        if policy["target_state"]["blocking"]:
            return {"ok": False, "refused": True,
                    "reason": "target_state_preserved" if policy["target_state"]["preserved"]
                    else "target_state_unknown_held",
                    "detail": policy["reason"], "policy": policy}
        was_bound = bool(scope.bound)
        from ..app_core import RigBusyError
        try:
            async with core.exclusive_rig_operation("scope_unbind"):
                await scope.unbind()
                lease_released = release_companion_device_lease(core)
        except RigBusyError as exc:
            return _rig_busy(exc, "scope_unbind")
        core.auditor.record("scope_unbind", "SAFE", {"was_bound": was_bound}, "executed")
        core.bus.publish("scope_unbound", {"was_bound": was_bound})
        return {"ok": True, "unbound": True, "was_bound": was_bound,
                "device_lease_released": lease_released,
                "policy": companion_scope_policy(core)}

    @srv.tool(name="scope_measure", description="Automated measurements (Vpp, Vmax, Vmin, width, "
              "rise, freq) → compact values (spec §11.4). SAFE.", annotations=anns(read_only=True),
              meta=meta("SAFE", "invisible", 400))
    async def scope_measure(
        channel: Annotated[int, Field(description=
            "One-based oscilloscope input channel to measure.")] = 1,
    ) -> dict:
        policy = companion_scope_policy(core)
        if not policy["companion_access_allowed"]:
            return {"ok": False, "refused": True, "reason": "rigol_session_owned",
                    "detail": policy["reason"], "policy": policy}
        if not scope.bound:
            return {"ok": False, "error": "scope not bound; call scope_bind first"}
        from ..app_core import RigBusyError
        try:
            async with core.exclusive_rig_operation("scope_measure"):
                m = await scope.measure(channel)
        except RigBusyError as exc:
            return _rig_busy(exc, "scope_measure")
        core.bus.publish("scope_measure", {"channel": channel, "measurements": m})
        return {"ok": True, "channel": channel, "measurements": m}

    @srv.tool(name="scope_capture", description="Capture a waveform through the optional companion "
              "session → trace summary + waveform_uri. Refuses whenever project evidence owns the "
              "Rigol; synchronized campaign evidence is collected only by that project adapter.",
              annotations=anns(read_only=True), meta=meta("SAFE", "invisible", 600))
    async def scope_capture(
        channel: Annotated[int, Field(description=
            "One-based oscilloscope input channel to capture.")] = 1,
        detail: Annotated[str, Field(description=
            "Response detail: summary, textmap, or image.")] = "summary",
        attempt_id: Annotated[int | None, Field(description=
            "Optional persisted attempt ID to which the waveform sidecar is attached.")] = None,
    ) -> dict:
        policy = companion_scope_policy(core)
        if not policy["companion_capture_allowed"]:
            return {"ok": False, "refused": True, "reason": "rigol_capture_owned",
                    "detail": policy["reason"], "policy": policy}
        if not scope.bound:
            return {"ok": False, "error": "scope not bound; call scope_bind first"}
        from ..app_core import RigBusyError
        try:
            async with core.exclusive_rig_operation("scope_capture"):
                volts, pre = await scope.capture(channel)
        except RigBusyError as exc:
            return _rig_busy(exc, "scope_capture")
        dt = pre.get("dt_s", 1e-9)
        cap_id = int(time.time() * 1000) % 10_000_000
        # Persist samples as an NPY sidecar and attach a trace raw_capture if an attempt is given.
        npy_path = config.BLOB_DIR / f"wave_{cap_id}.npy"
        try:
            np.save(npy_path, volts)
        except Exception:
            pass
        if attempt_id is not None:
            store._wlock.acquire()
            try:
                store._conn.execute(
                    "INSERT INTO raw_capture(attempt_id,channel,payload,encoding,preamble,"
                    "is_sidecar,sidecar_path) VALUES (?,?,?,?,?,?,?)",
                    (attempt_id, "trace", b"", "npy", None, 1, str(npy_path)))
                store._conn.commit()
            finally:
                store._wlock.release()
        vmin, vmax = float(np.min(volts)), float(np.max(volts))
        vpp = vmax - vmin
        # crude glitch feature detection: largest excursion from median
        med = float(np.median(volts))
        clipped = bool(vmax >= 0.98 * (vmax) and (vmax - med) > 0)
        summary = {"samples": int(volts.size), "dt_s": dt, "t0_s": pre.get("t0_s", 0.0),
                   "vmin": round(vmin, 4), "vmax": round(vmax, 4), "vpp": round(vpp, 4),
                   "clipped": clipped,
                   "waveform_uri": f"glitchlab://scope/waveform/{cap_id}"}
        if detail == "textmap":
            from ..render.textart import trace_sparkline
            summary["sparkline"] = trace_sparkline(volts, 64)
        if detail == "image":
            from ..render.image import waveform_png
            out = config.FIGURE_DIR / f"wave_{cap_id}.png"
            waveform_png(volts, dt, pre.get("t0_s", 0.0), out)
            summary["render_uri"] = f"glitchlab://scope/waveform/{cap_id}.png"
        core.bus.publish("scope_capture", {"capture_id": cap_id, "channel": channel, **summary})
        return {"ok": True, "capture_id": cap_id, **summary}

    @srv.tool(name="scope_configure_acquisition", description="Timebase/trigger/coupling within "
              "sane ranges (spec §11.4). SAFE.", meta=meta("SAFE", "control", 400))
    async def scope_configure_acquisition(
        timebase_s: Annotated[float | None, Field(description=
            "Optional horizontal time scale in seconds per division.")] = None,
        trig_level_v: Annotated[float | None, Field(description=
            "Optional trigger threshold in volts.")] = None,
        trig_source: Annotated[str | None, Field(description=
            "Optional trigger source understood by the bound scope, such as CHAN2 or EXT.")] = None,
    ) -> dict:
        policy = companion_scope_policy(core)
        if not policy["companion_access_allowed"]:
            return {"ok": False, "refused": True, "reason": "rigol_session_owned",
                    "detail": policy["reason"], "policy": policy}
        if not scope.bound:
            return {"ok": False, "error": "scope not bound"}
        from ..app_core import RigBusyError
        try:
            async with core.exclusive_rig_operation("scope_configure_acquisition"):
                applied = await scope.configure_acquisition(timebase_s=timebase_s,
                                                            trig_level_v=trig_level_v,
                                                            trig_source=trig_source)
        except RigBusyError as exc:
            return _rig_busy(exc, "scope_configure_acquisition")
        return {"ok": True, "applied": applied}

    @srv.tool(name="scope_screenshot", description="PNG resource from the optional companion SCPI "
              "session. Refuses when the project evidence collector owns the Rigol.", annotations=anns(read_only=True),
              meta=meta("SAFE", "invisible", 200))
    async def scope_screenshot() -> dict:
        policy = companion_scope_policy(core)
        if not policy["companion_capture_allowed"]:
            return {"ok": False, "refused": True, "reason": "rigol_capture_owned",
                    "detail": policy["reason"], "policy": policy}
        if not scope.bound:
            return {"ok": False, "error": "scope not bound"}
        from ..app_core import RigBusyError
        try:
            async with core.exclusive_rig_operation("scope_screenshot"):
                png = await scope.screenshot()
        except RigBusyError as exc:
            return _rig_busy(exc, "scope_screenshot")
        sid = int(time.time() * 1000) % 10_000_000
        out = config.FIGURE_DIR / f"scope_{sid}.png"
        out.write_bytes(png)
        return {"ok": True, "bytes": len(png), "render_uri": f"glitchlab://scope/screenshot/{sid}.png",
                "path": str(out)}

    # -- DANGER tools ----------------------------------------------------------------
    @srv.tool(name="scope_channel_configure", description="Vertical scale + REQUIRED probe_ratio; "
              "over-range refusal (integrity). DANGER: refuses without probe_ratio or if scale "
              "implies over-rated input.", annotations=anns(destructive=False),
              meta=meta("DANGER", "control", 400,
                        safety={"enforced": "probe_ratio required, over-range refusal",
                                "fails_closed": "rated_max_input_unknown|probe_ratio_unset"}))
    async def scope_channel_configure(
        channel: Annotated[int, Field(description=
            "One-based input channel to configure.")] = 1,
        scale_v_per_div: Annotated[float | None, Field(description=
            "Optional vertical scale at the probe tip, in volts per division.")] = None,
        offset_v: Annotated[float | None, Field(description=
            "Optional channel vertical offset in volts.")] = None,
        coupling: Annotated[str | None, Field(description=
            "Optional input coupling mode supported by the scope, such as DC, AC, or GND.")] = None,
        probe_ratio: Annotated[float | None, Field(description=
            "Declared probe attenuation ratio; required by safety policy for a live change.")] = None,
        dry_run: Annotated[bool, Field(description=
            "Validate safety limits and report effective settings without changing the scope.")] = False,
    ) -> dict:
        policy = companion_scope_policy(core)
        if not policy["companion_access_allowed"] and not dry_run:
            return {"ok": False, "refused": True, "reason": "rigol_session_owned",
                    "detail": policy["reason"], "policy": policy}
        args = {"channel": channel, "scale_v_per_div": scale_v_per_div, "probe_ratio": probe_ratio}
        dec = core.safety.check("scope_channel_configure", args, dry_run=dry_run,
                                context={"rated_max_input_v": scope.rated_max_input_v()})
        core.auditor.record_decision("scope_channel_configure", args, dec)
        if not dec.allowed and dec.decision == "refused":
            return dec.refusal_dict()
        if dec.decision == "dry_run":
            return {"ok": True, "dry_run": True, "would_apply": dec.effective}
        if not scope.bound:
            return {"ok": False, "error": "scope not bound"}
        from ..app_core import RigBusyError
        try:
            async with core.exclusive_rig_operation("scope_channel_configure"):
                applied = await scope.configure_channel(channel, scale_v_per_div=scale_v_per_div,
                                                        offset_v=offset_v, coupling=coupling,
                                                        probe_ratio=probe_ratio)
        except RigBusyError as exc:
            return _rig_busy(exc, "scope_channel_configure")
        return {"ok": True, "applied": applied,
                "max_safe_measurement_v": dec.effective.get("max_safe_measurement_v")}

    @srv.tool(name="scope_source_configure", description="Validate a bounded AWG configuration "
              "with output-off and declared-load checks. This tool currently returns the accepted "
              "configuration but does not issue SCPI; output remains off.",
              annotations=anns(destructive=True, open_world=True),
              meta=meta("DANGER", "control", 400,
                        safety={"enforced": "amplitude/offset/frequency ceilings + declared load",
                                "note": "AWG unused in this campaign (safe_guards S1)"}))
    async def scope_source_configure(
        amplitude_vpp: Annotated[float, Field(description=
            "Requested generator amplitude in peak-to-peak volts.")] = 0.0,
        offset_v: Annotated[float, Field(description=
            "Requested generator DC offset in volts.")] = 0.0,
        frequency_hz: Annotated[float, Field(description=
            "Requested generator frequency in hertz.")] = 0.0,
        load_impedance: Annotated[str | None, Field(description=
            "Declared passive load, for example Hi-Z or 50OHM; required by safety policy.")] = None,
        dry_run: Annotated[bool, Field(description=
            "Validate and report the request without applying an effective configuration.")] = False,
    ) -> dict:
        policy = companion_scope_policy(core)
        if policy["target_state"]["blocking"] and not dry_run:
            return {"ok": False, "refused": True,
                    "reason": "target_state_preserved" if policy["target_state"]["preserved"]
                    else "target_state_unknown_held",
                    "detail": policy["reason"], "policy": policy}
        args = {"amplitude_vpp": amplitude_vpp, "offset_v": offset_v, "frequency_hz": frequency_hz,
                "load_impedance": load_impedance}
        dec = core.safety.check("scope_source_configure", args, dry_run=dry_run)
        core.auditor.record_decision("scope_source_configure", args, dec)
        if not dec.allowed and dec.decision == "refused":
            return dec.refusal_dict()
        if dec.decision == "dry_run":
            return {"ok": True, "dry_run": True, "would_apply": dec.effective}
        # NOTE: source syntax must be confirmed against :SYSTem:ERRor? before any real output (§16.5)
        return {"ok": True, "configured": dec.effective,
                "warning": "AWG output remains OFF; syntax must be confirmed before enabling"}

    @srv.tool(name="scope_source_output", description="Validate and record a requested AWG output "
              "state with passive-load and no-back-drive checks. This tool currently does not issue "
              "the hardware SCPI output command.",
              annotations=anns(destructive=True, open_world=True),
              meta=meta("DANGER", "control", 300,
                        safety={"enforced": "declared passive load, no back-drive, auto-off"}))
    async def scope_source_output(
        enable: Annotated[bool, Field(description=
            "Requested output state: true for on, false for off.")] = False,
        load_impedance: Annotated[str | None, Field(description=
            "Declared passive load presented to the generator; required when enabling.")] = None,
        load_driven: Annotated[bool, Field(description=
            "True if another source may drive the load; enabling then fails closed to prevent back-drive.")] = False,
        dry_run: Annotated[bool, Field(description=
            "Validate and report the requested state without changing recorded danger state.")] = False,
    ) -> dict:
        policy = companion_scope_policy(core)
        if policy["target_state"]["blocking"] and not dry_run:
            return {"ok": False, "refused": True,
                    "reason": "target_state_preserved" if policy["target_state"]["preserved"]
                    else "target_state_unknown_held",
                    "detail": policy["reason"], "policy": policy}
        args = {"enable": enable, "load_impedance": load_impedance, "load_driven": load_driven}
        dec = core.safety.check("scope_source_output", args, dry_run=dry_run)
        core.auditor.record_decision("scope_source_output", args, dec)
        if not dec.allowed and dec.decision == "refused":
            return dec.refusal_dict()
        if dec.decision == "dry_run":
            return {"ok": True, "dry_run": True, "would_set_output": enable}
        core.set_danger_state(awg_output="ON" if enable else "OFF")
        return {"ok": True, "awg_output": "ON" if enable else "OFF",
                "note": "AWG intentionally unused in this campaign (safe_guards S1)"}
