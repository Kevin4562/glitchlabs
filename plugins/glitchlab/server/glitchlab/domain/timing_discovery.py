"""Scope-assisted glitch-timing discovery — TARGET-AGNOSTIC (spec §16.4 / goal).

The hardest part of testing a new target is often not the glitch shape but *when* to fire it. A
target with no prior characterization has no known window, so this module derives one from data:

    1. Arm the oscilloscope for ONE edge-triggered acquisition on the target's trigger line
       (typically the reset line) — the same t=0 the glitcher fires its ext_offset from.
    2. Reboot the target through the glitcher (this produces the trigger edge).
    3. Read the trigger-aligned trace of the target's *signal* channel (typically the power/core
       rail, whose current draw reveals when the CPU is actually computing).
    4. Analyse the power-activity envelope to find the active compute region, and convert it to a
       suggested **ext_offset window in glitch-clock cycles** — turning "sweep 0..∞" into a bounded,
       data-driven search you can seed a campaign with.

Nothing here is target-specific: channels, the trigger level, the capture window and the glitch
clock all come from the active ``projects/*.yaml`` profile (with sane defaults) and the
reboot comes from whatever glitcher is bound. Any target with a trigger edge + a measurable signal
works across new targets when the target profile provides safe timing limits.
"""
from __future__ import annotations

import asyncio
from typing import Optional

import numpy as np


class TimingDiscovery:
    def __init__(self, core) -> None:
        self.core = core

    def _profile(self) -> dict:
        return dict(self.core.rig.project_profile or {})

    async def characterize(self, *, trigger_channel: Optional[int] = None,
                           signal_channel: Optional[int] = None, window_s: Optional[float] = None,
                           trig_level_v: Optional[float] = None, slope: str = "POSitive",
                           glitch_clock_hz: Optional[float] = None,
                           signal_scale_v: Optional[float] = None,
                           trigger_scale_v: Optional[float] = None) -> dict:
        core = self.core
        prof = self._profile()
        td = prof.get("timing_discovery", {}) or {}
        channel_map = (((prof.get("evidence") or {}).get("rigol") or {}).get("channels") or {})
        trigger_channel = trigger_channel or td.get("trigger_channel") or channel_map.get("trigger", 2)
        signal_channel = signal_channel or td.get("signal_channel") or channel_map.get("signal", 1)
        window_s = window_s or td.get("window_s", 300e-6)
        trig_level_v = trig_level_v if trig_level_v is not None else td.get("trig_level_v", 1.65)
        signal_scale_v = signal_scale_v or td.get("signal_scale_v", 0.5)
        trigger_scale_v = trigger_scale_v or td.get("trigger_scale_v", 1.0)
        glitch_cfg = ((prof.get("glitcher") or {}).get("config") or {})
        glitch_clock_hz = float(glitch_clock_hz or td.get("glitch_clock_hz")
                                or glitch_cfg.get("clock_hz") or 100e6)

        scope = core.scope
        if scope is None:
            return {"ok": False, "error": "no scope adapter"}
        if scope.bound:
            return {
                "ok": False,
                "refused": True,
                "error": "companion scope is already bound",
                "detail": "unbind it first so timing discovery can own and release one scoped session",
            }

        # Acquire the live rig lease before opening the companion SCPI session.
        # The ordinary campaign path refuses a pre-bound companion scope because
        # its synchronized evidence collector owns that same Rigol endpoint.
        g = core.ensure_glitcher(connect=True)
        reboot = getattr(g, "power_cycle", None) or getattr(g, "_bringup", None)
        if reboot is None:
            return {"ok": False, "error": "bound glitcher exposes no reboot/bringup"}

        hint = ((core.rig.instruments or {}).get("scope") or {}).get("hint_ip")
        bound = await scope.bind(hint_ip=hint)
        if not bound.get("ok"):
            return {"ok": False, "error": "scope not bound", "detail": bound}

        try:
            # Vertical + timebase setup (10 divisions across the window; offset
            # zero clears stale display state).  All requested fields must be
            # acknowledged by the adapter; setup errors are not timing data.
            signal_cfg = await scope.configure_channel(
                signal_channel, scale_v_per_div=signal_scale_v,
                offset_v=0.0, coupling="DC"
            )
            trigger_cfg = await scope.configure_channel(
                trigger_channel, scale_v_per_div=trigger_scale_v,
                offset_v=0.0, coupling="DC"
            )
            acquisition_cfg = await scope.configure_acquisition(timebase_s=window_s / 10.0)
            if signal_cfg.get("scale_v_per_div") != signal_scale_v:
                return {"ok": False, "error": "signal channel configuration was not applied",
                        "detail": signal_cfg}
            if trigger_cfg.get("scale_v_per_div") != trigger_scale_v:
                return {"ok": False, "error": "trigger channel configuration was not applied",
                        "detail": trigger_cfg}
            if acquisition_cfg.get("timebase_s") != window_s / 10.0:
                return {"ok": False, "error": "timebase configuration was not applied",
                        "detail": acquisition_cfg}

            # Auto-calibrate from the actual idle-high reset level.  Measurement
            # failure is allowed to fall back to the project value, but trigger
            # and trace acquisition below remain fail-closed.
            auto_level = None
            try:
                tm = await scope.measure(trigger_channel)
                vmax = tm.get("vmax")
                if vmax is not None and 0.05 < abs(vmax) < 1e6:
                    auto_level = round(0.5 * float(vmax), 4)
            except Exception:
                pass
            eff_level = auto_level if auto_level is not None else trig_level_v

            armed = await scope.arm_single(
                trig_source=f"CHANnel{trigger_channel}",
                trig_level_v=eff_level,
                slope=slope,
            )
            if armed.get("armed") is not True:
                return {"ok": False, "error": "scope did not acknowledge single-shot arm",
                        "detail": armed}
            await asyncio.sleep(0.15)
            reboot_result = await asyncio.to_thread(reboot)
            if isinstance(reboot_result, dict) and reboot_result.get("ok") is not True:
                return {"ok": False, "error": "target reboot was refused or failed",
                        "detail": reboot_result}

            wait = await scope.wait_trigger(timeout_s=max(3.0, window_s * 30 + 2.0))
            if wait.get("ok") is not True:
                return {"ok": False, "triggered": False,
                        "error": "scope did not observe the requested physical trigger",
                        "trigger_status": wait.get("status"), "detail": wait}

            volts, meta = await scope.capture(signal_channel, frozen=True)
            volts = np.asarray(volts, dtype=float)
            try:
                dt = float(meta["dt_s"])
                t0 = float(meta["t0_s"])
            except (KeyError, TypeError, ValueError) as exc:
                return {"ok": False, "error": "waveform preamble is invalid", "detail": repr(exc)}
            if volts.size < 8 or not np.all(np.isfinite(volts)) or not np.isfinite(dt) or dt <= 0:
                return {"ok": False, "error": "captured waveform is empty or invalid",
                        "trace_points": int(volts.size), "dt_s": dt}
            analysis = self._analyze(volts, dt, t0, glitch_clock_hz)
            timing_valid = bool(
                analysis.get("active_region_s")
                and analysis.get("suggested_ext_offset")
            )
            result = {
                "ok": timing_valid,
                "triggered": True,
                "trigger_status": wait.get("status"),
                "trigger_level_v": eff_level,
                "trigger_level_auto": auto_level,
                "glitch_clock_hz": glitch_clock_hz,
                "window_s": window_s,
                "timebase_s_per_div": window_s / 10.0,
                "trigger_channel": trigger_channel,
                "signal_channel": signal_channel,
                "scope_configuration": {
                    "signal": signal_cfg,
                    "trigger": trigger_cfg,
                    "acquisition": acquisition_cfg,
                    "arm": armed,
                },
                "reboot": reboot_result,
                "trace": {"points": int(volts.size), "dt_s": dt, "t0_s": t0,
                          "vmin": float(volts.min()), "vmax": float(volts.max())},
                **analysis,
            }
            if not timing_valid:
                result["error"] = "trace contained no valid post-trigger activity window"
            core.bus.publish("timing_discovered", {k: result[k] for k in
                             ("ok", "suggested_ext_offset", "active_region_s", "triggered")
                             if k in result})
            return result
        finally:
            await scope.unbind()

    @staticmethod
    def _analyze(volts: np.ndarray, dt: float, t0: float, clk: float) -> dict:
        n = volts.size
        if n < 8:
            return {"suggested_ext_offset": None, "note": "trace too short"}
        t = t0 + np.arange(n) * dt
        win = max(4, int(round(1e-6 / dt)))          # ~1 µs activity-smoothing window
        v = volts - np.median(volts)
        env = np.sqrt(np.convolve(v * v, np.ones(win) / win, mode="same"))  # rolling RMS envelope
        pre = env[t < 0]
        if pre.size >= 8:
            floor, noise = float(np.median(pre)), float(np.std(pre))
        else:
            floor, noise = float(np.percentile(env, 20)), float(np.std(env[:max(8, n // 10)]))
        thr = floor + 4.0 * noise + 1e-9
        active = (env > thr) & (t >= 0)
        idx = np.where(active)[0]
        activity = {"floor": round(floor, 6), "threshold": round(thr, 6),
                    "env_max": round(float(env.max()), 6)}
        if idx.size == 0:
            return {"active_region_s": None, "suggested_ext_offset": None, "activity": activity,
                    "note": "no post-trigger activity above noise; widen window_s or check the "
                            "signal channel / probe / trigger level"}
        t_start, t_end = float(t[idx[0]]), float(t[idx[-1]])
        seg_lo, seg_hi = idx[0], idx[-1] + 1
        dip_i = seg_lo + int(np.argmin(volts[seg_lo:seg_hi]))   # deepest core-rail dip = heavy compute
        dip_t = float(t[dip_i])
        return {
            "active_region_s": [t_start, t_end],
            "suggested_ext_offset": {"min": max(0, int(t_start * clk)), "max": int(t_end * clk),
                                     "center_candidate": max(0, int(dip_t * clk)),
                                     "unit": "glitch_clock_cycles"},
            "deepest_dip": {"t_s": dip_t, "v": float(volts[dip_i]), "ext_offset": max(0, int(dip_t * clk))},
            "calibration": {"glitch_clock_hz": clk, "ns_per_cycle": 1e9 / clk, "cycles_per_us": clk / 1e6},
            "activity": activity,
        }
