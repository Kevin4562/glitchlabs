"""Bundled rigol-mcp toolset exposed as first-class GlitchLab tools.

Wraps the MIT-licensed rigol-mcp library bundled under vendor/rigol-mcp — a rich Rigol SCPI toolset (measure,
waveform analysis, channel/timebase/trigger config, cursors, screenshot, autoscale, run/stop/single,
raw SCPI) that already auto-detects DHO-series scopes (ours). Every tool runs against GlitchLab's
SINGLE optional companion session via ScopeAdapter.raw(), so there is no second companion
connection. Every call fails closed when the active project evidence collector owns the Rigol.
Names are prefixed `rigol_` so they sit alongside the native scope_* tools without collision.
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path
from typing import Annotated

from pydantic import Field

from . import anns, meta
from .scope import companion_scope_policy
from .. import config

# Make the vendored rigol_mcp importable (LAN-only; no USB/libusb needed).
_VENDOR = config.PROJECT_ROOT / "vendor" / "rigol-mcp" / "src"
if _VENDOR.exists() and str(_VENDOR) not in sys.path:
    sys.path.insert(0, str(_VENDOR))

try:
    import rigol_mcp.scope as R
    from rigol_mcp.waveform_analysis import describe_waveform as _describe
    _AVAILABLE = True
except Exception as _e:  # pragma: no cover
    R = None
    _describe = None
    _AVAILABLE = False


def _f(v):
    return float(v) if v is not None else None


def register(srv, core):
    if not _AVAILABLE:
        return
    # rigol_mcp reads RIGOL_IP for its own connection path; we never use that (we share
    # GlitchLab's session), but set it so any internal diagnostic prints the right host.
    os.environ.setdefault("RIGOL_IP", config.SCOPE_HINT_IP)
    scope = core.scope

    async def _raw(fn, *a, **k):
        policy = companion_scope_policy(core)
        if not policy["companion_access_allowed"]:
            return None, {"ok": False, "refused": True, "reason": "rigol_session_owned",
                          "detail": policy["reason"], "policy": policy}
        from ..app_core import RigBusyError
        try:
            operation = f"rigol_{getattr(fn, '__name__', 'access')}"
            async with core.exclusive_rig_operation(operation):
                return await scope.raw(fn, *a, **k), None
        except RigBusyError as e:
            return None, {"ok": False, "refused": True,
                          "reason": "rig_operation_in_progress",
                          "violated_rule": "rig_operation_in_progress",
                          "detail": str(e)}
        except Exception as e:
            return None, f"{type(e).__name__}: {e}"

    def _failure(err, **context):
        if isinstance(err, dict):
            return {**err, **context}
        return {"ok": False, "error": err, **context}

    @srv.tool(name="rigol_idn", description="[rigol] Identify the instrument (*IDN?) and confirm the "
              "auto-detected dialect driver (DHO/DS1000Z).", annotations=anns(read_only=True),
              meta=meta("SAFE", "invisible", 300, bundle="rigol-mcp"))
    async def rigol_idn() -> dict:
        v, err = await _raw(R.idn)
        if err is not None:
            return _failure(err)
        return {"ok": err is None, "idn": v, "error": err}

    @srv.tool(name="rigol_get_scope_state", description="[rigol] Snapshot of the scope config: active "
              "channels (scale/offset/coupling/probe), timebase, and trigger.",
              annotations=anns(read_only=True), meta=meta("SAFE", "invisible", 1200, bundle="rigol-mcp"))
    async def rigol_get_scope_state() -> dict:
        v, err = await _raw(R.get_scope_state)
        return v if err is None else _failure(err)

    @srv.tool(name="rigol_measure", description="[rigol] Query a single-source measurement on a "
              "channel. item ∈ {VMAX,VMIN,VPP,VTOP,VBASE,VAMP,VAVG,VRMS,FREQUENCY,PERIOD,PWIDTH,"
              "NWIDTH,PDUTY,NDUTY,RTIME,FTIME,OVERSHOOT,PRESHOOT,…}. 9.9E37 = invalid/overflow.",
              annotations=anns(read_only=True), meta=meta("SAFE", "invisible", 300, bundle="rigol-mcp"))
    async def rigol_measure(
        channel: Annotated[str, Field(description=
            "Measurement source channel, such as CHAN1 through CHAN4.")],
        item: Annotated[str, Field(description=
            "Rigol measurement mnemonic, such as VPP, VMIN, FREQUENCY, PWIDTH, or RTIME.")],
    ) -> dict:
        v, err = await _raw(R.measure, channel, item)
        if err is not None:
            return _failure(err, channel=channel, item=item)
        return {"ok": err is None, "channel": channel, "item": item, "value": v, "error": err}

    @srv.tool(name="rigol_measure_between", description="[rigol] Two-source delay/phase between "
              "channels. item ∈ {RDELAY,FDELAY,RPHASE,FPHASE, RRDELAY,RFDELAY,FRDELAY,FFDELAY,…}.",
              annotations=anns(read_only=True), meta=meta("SAFE", "invisible", 300, bundle="rigol-mcp"))
    async def rigol_measure_between(
        source1: Annotated[str, Field(description=
            "First measurement source channel, such as CHAN1.")],
        source2: Annotated[str, Field(description=
            "Second measurement source channel, such as CHAN2.")],
        item: Annotated[str, Field(description=
            "Two-source timing mnemonic, such as RDELAY, FDELAY, RPHASE, or FPHASE.")],
    ) -> dict:
        v, err = await _raw(R.measure_between, source1, source2, item)
        if err is not None:
            return _failure(err, source1=source1, source2=source2, item=item)
        return {"ok": err is None, "value": v, "error": err}

    @srv.tool(name="rigol_get_waveform", description="[rigol] Download + analyse a channel's waveform "
              "(screen buffer). Returns text analysis: shape, frequency/period, amplitude, DC "
              "offset, warnings. raw_data=true returns full time/voltage arrays.",
              annotations=anns(read_only=True), meta=meta("SAFE", "invisible", 2000, bundle="rigol-mcp"))
    async def rigol_get_waveform(
        channel: Annotated[str, Field(description=
            "Waveform source channel, such as CHAN1 through CHAN4.")],
        raw_data: Annotated[bool, Field(description=
            "Return full waveform arrays when true; otherwise return compact derived analysis.")] = False,
    ) -> dict:
        v, err = await _raw(R.get_waveform, channel)
        if err is not None:
            return _failure(err, channel=channel)
        if raw_data:
            return {"ok": True, "channel": channel, "waveform": v}
        return {"ok": True, "channel": channel, "analysis": _describe(v)}

    @srv.tool(name="rigol_set_channel", description="[rigol] Configure a channel (only given params "
              "change). channel: CHAN1–CHAN4; scale_v_div; offset_v; coupling AC/DC/GND; probe ratio.",
              meta=meta("SAFE", "control", 500, bundle="rigol-mcp"))
    async def rigol_set_channel(
        channel: Annotated[str, Field(description=
            "Input channel to configure, CHAN1 through CHAN4.")],
        display: Annotated[bool | None, Field(description=
            "Optional channel display state; omit to leave unchanged.")] = None,
        scale_v_div: Annotated[float | None, Field(description=
            "Optional vertical scale in volts per division.")] = None,
        offset_v: Annotated[float | None, Field(description=
            "Optional vertical offset in volts.")] = None,
        coupling: Annotated[str | None, Field(description=
            "Optional coupling mode: AC, DC, or GND.")] = None,
        probe: Annotated[float | None, Field(description=
            "Optional probe attenuation ratio reported to the scope.")] = None,
    ) -> dict:
        _, err = await _raw(R.set_channel, channel, display=display, scale=_f(scale_v_div),
                            offset=_f(offset_v), coupling=coupling, probe=_f(probe))
        if err:
            return _failure(err, channel=channel)
        st, e2 = await _raw(R.get_scope_state)
        if e2:
            return _failure(e2, channel=channel, applied=True, readback_complete=False)
        return {"ok": True, "channel": (st or {}).get("channels", {}).get(channel.upper())}

    @srv.tool(name="rigol_set_timebase", description="[rigol] Set horizontal timebase. scale_s_div = "
              "seconds/division; offset_s shifts the window (trigger offset).",
              meta=meta("SAFE", "control", 400, bundle="rigol-mcp"))
    async def rigol_set_timebase(
        scale_s_div: Annotated[float | None, Field(description=
            "Optional horizontal scale in seconds per division.")] = None,
        offset_s: Annotated[float | None, Field(description=
            "Optional horizontal/trigger offset in seconds.")] = None,
    ) -> dict:
        _, err = await _raw(R.set_timebase, scale=_f(scale_s_div), offset=_f(offset_s))
        if err:
            return _failure(err)
        st, e2 = await _raw(R.get_scope_state)
        if e2:
            return _failure(e2, applied=True, readback_complete=False)
        return {"ok": True, "timebase": (st or {}).get("timebase")}

    @srv.tool(name="rigol_set_trigger", description="[rigol] Configure edge trigger. source: "
              "CHAN1–CHAN4/EXT; slope POS/NEG/RFAL; level in volts.",
              meta=meta("SAFE", "control", 400, bundle="rigol-mcp"))
    async def rigol_set_trigger(
        source: Annotated[str | None, Field(description=
            "Optional edge-trigger source: CHAN1 through CHAN4 or EXT.")] = None,
        slope: Annotated[str | None, Field(description=
            "Optional edge slope mnemonic: POS, NEG, or RFAL.")] = None,
        level: Annotated[float | None, Field(description=
            "Optional edge-trigger threshold in volts.")] = None,
    ) -> dict:
        _, err = await _raw(R.set_trigger, source=source, slope=slope, level=_f(level))
        if err:
            return _failure(err)
        st, e2 = await _raw(R.get_scope_state)
        if e2:
            return _failure(e2, applied=True, readback_complete=False)
        return {"ok": True, "trigger": (st or {}).get("trigger")}

    @srv.tool(name="rigol_set_cursors", description="[rigol] Set cursor mode (OFF/MANUAL/TRACK) and/or "
              "A/B X positions (seconds); returns cursor readouts (Δt, 1/Δt).",
              meta=meta("SAFE", "control", 400, bundle="rigol-mcp"))
    async def rigol_set_cursors(
        mode: Annotated[str | None, Field(description=
            "Optional cursor mode: OFF, MANUAL, or TRACK.")] = None,
        ax: Annotated[float | None, Field(description=
            "Optional A X-cursor position in seconds.")] = None,
        bx: Annotated[float | None, Field(description=
            "Optional B X-cursor position in seconds.")] = None,
    ) -> dict:
        if mode is not None:
            _, err = await _raw(R.set_cursor_mode, mode)
            if err:
                return _failure(err)
        else:
            mode, err = await _raw(R.get_cursor_mode)
            if err:
                return _failure(err)
        if mode and mode.upper() != "OFF" and (ax is not None or bx is not None):
            _, err = await _raw(R.set_cursor_positions, mode, ax=_f(ax), bx=_f(bx))
            if err:
                return _failure(err)
        vals, err = await _raw(R.get_cursor_values)
        if err is not None:
            return _failure(err)
        return {"ok": err is None, "cursors": vals, "error": err}

    @srv.tool(name="rigol_get_cursor_values", description="[rigol] Read cursor mode + all readouts "
              "(AX_s, BX_s, Δt, 1/Δt).", annotations=anns(read_only=True),
              meta=meta("SAFE", "invisible", 300, bundle="rigol-mcp"))
    async def rigol_get_cursor_values() -> dict:
        v, err = await _raw(R.get_cursor_values)
        if err is not None:
            return _failure(err)
        return {"ok": err is None, "cursors": v, "error": err}

    @srv.tool(name="rigol_run", description="[rigol] Start continuous acquisition.",
              meta=meta("SAFE", "control", 200, bundle="rigol-mcp"))
    async def rigol_run() -> dict:
        v, err = await _raw(R.run)
        return _failure(err) if err is not None else {"ok": True, "trigger_status": v, "error": None}

    @srv.tool(name="rigol_stop", description="[rigol] Stop acquisition and freeze the display "
              "(use before reading measurements/cursors for stable values).",
              meta=meta("SAFE", "control", 200, bundle="rigol-mcp"))
    async def rigol_stop() -> dict:
        v, err = await _raw(R.stop)
        return _failure(err) if err is not None else {"ok": True, "trigger_status": v, "error": None}

    @srv.tool(name="rigol_single", description="[rigol] Arm a single acquisition (stops after one "
              "trigger).", meta=meta("SAFE", "control", 200, bundle="rigol-mcp"))
    async def rigol_single() -> dict:
        v, err = await _raw(R.single)
        return _failure(err) if err is not None else {"ok": True, "trigger_status": v, "error": None}

    @srv.tool(name="rigol_autoscale", description="[rigol] Run the scope's auto-setup (timebase, "
              "vertical scale, trigger); returns the resulting configuration.",
              meta=meta("SAFE", "control", 800, bundle="rigol-mcp"))
    async def rigol_autoscale() -> dict:
        _, err = await _raw(R.autoscale)
        if err:
            return _failure(err)
        st, _ = await _raw(R.get_scope_state)
        return {"ok": True, "state": st}

    @srv.tool(name="rigol_screenshot", description="[rigol] Capture the scope display as a PNG; "
              "returns a resource link (not inline) to stay context-cheap.",
              annotations=anns(read_only=True), meta=meta("SAFE", "invisible", 200, bundle="rigol-mcp"))
    async def rigol_screenshot() -> dict:
        png, err = await _raw(R.screenshot_png)
        if err:
            return _failure(err)
        sid = int(time.time() * 1000) % 10_000_000
        out = config.FIGURE_DIR / f"rigol_{sid}.png"
        out.write_bytes(png)
        return {"ok": True, "bytes": len(png), "path": str(out),
                "render_uri": f"glitchlab://scope/screenshot/{sid}.png"}

    @srv.tool(name="rigol_send_raw", description="[rigol] Send an arbitrary SCPI command (escape "
              "hatch). Queries ('…?') return the response; writes auto-check the error queue. "
              "CAUTION: can put the scope in any state.",
              annotations=anns(open_world=True), meta=meta("CAUTION", "control", 400, bundle="rigol-mcp",
              safety={"note": "arbitrary SCPI to the scope"}))
    async def rigol_send_raw(
        command: Annotated[str, Field(description=
            "Exact SCPI query or write command to send to the bound Rigol session.")],
        dry_run: Annotated[bool, Field(description=
            "Return the command that would be sent without transmitting it.")] = False,
    ) -> dict:
        if dry_run:
            return {"ok": True, "dry_run": True, "would_send": command}
        v, err = await _raw(R.send_raw, command)
        core.auditor.record("rigol_send_raw", "CAUTION", {"command": command},
                            "executed" if err is None else "refused", result={"response": v})
        return _failure(err, response=v) if err is not None else {"ok": True, "response": v, "error": None}
