"""Limit validation, fail-closed, structured refusals (spec §18.1-§18.5).

The Safety Engine sits between every actuating tool and the hardware. Nothing actuates without
passing its contract. Target limits come from the selected project profile and can only be made
more restrictive by an operator-owned rig-wide ceiling.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ..config import RigConfig
from . import contracts as C


@dataclass
class Decision:
    allowed: bool
    decision: str                 # executed | refused | dry_run
    danger: str
    violated_rule: str | None = None
    detail: str = ""
    effective: dict | None = None  # the validated/normalized args to actuate with

    def refusal_dict(self) -> dict:
        return {"ok": False, "refused": True, "danger": self.danger,
                "violated_rule": self.violated_rule, "detail": self.detail}


class SafetyEngine:
    def __init__(self, rig: RigConfig) -> None:
        self.rig = rig

    # -- helpers ---------------------------------------------------------------------
    def _limit(self, *path, default=None):
        return self.rig.limit(*path, default=default)

    def _fail_closed(self, contract: C.Contract, rule: str, detail: str) -> Decision:
        return Decision(False, "refused", contract.danger, rule, detail)

    # -- main entry ------------------------------------------------------------------
    def check(self, tool: str, args: dict, dry_run: bool = False,
              context: dict | None = None) -> Decision:
        contract = C.contract_for(tool)
        if contract is None:
            # not a guarded tool → SAFE
            return Decision(True, "executed", C.SAFE, effective=dict(args))
        context = context or {}
        args = dict(args)

        # dispatch per-tool validation
        handler = getattr(self, f"_check_{tool}", None)
        if handler is None:
            return self._fail_closed(contract, "no_validator",
                                     f"{tool} has a contract but no validator")
        dec = handler(contract, args, context)
        if dec.allowed and dry_run:
            dec.decision = "dry_run"
            dec.allowed = False   # dry-run never actuates (§18.1 rule 3)
        return dec

    # -- CONTROL PLANE ---------------------------------------------------------------
    def _check_control_sweep(self, contract, args, ctx) -> Decision:
        if not ctx.get("glitcher_bound", True):
            return self._fail_closed(contract, "glitcher_unbound", "no glitcher bound; fail closed")
        # Pre-campaign acknowledgment gate (goal TASK 1.2): a LIVE (non-simulator) campaign may
        # not start until the operator/agent has acknowledged the ACTIVE target's limits via
        # acknowledge_target(...). Simulator campaigns are exempt.
        if (not ctx.get("dry_run", False)
                and not ctx.get("is_simulator", False)
                and not ctx.get("target_acknowledged", False)):
            return self._fail_closed(contract, "target_unacknowledged",
                "live campaign requires acknowledge_target(<model>) confirming the active "
                "target's enforced limits (see the configure-target skill)")
        wmax = self._limit("glitch", "pulse_cycles_max",
                           default=self._limit("glitch", "width_cycles_max"))
        omax = self._limit("glitch", "ext_offset_max")
        nmax = self._limit("glitch", "num_glitches_max")
        rate = self._limit("rate", "max_attempts_per_second")
        vmax = self._limit("target_power", "vcc_max_v")
        if wmax is None or omax is None or nmax is None or rate is None or vmax is None:
            return self._fail_closed(contract, "limits_missing",
                                     "control_sweep limits not fully configured")
        spec = args.get("param_spec") or ctx.get("param_spec") or {}
        if int(spec.get("scope_capture_every", 0) or 0) != 0:
            rigol_cfg = ((self.rig.project_profile.get("evidence") or {}).get("rigol") or {})
            if rigol_cfg:
                return self._fail_closed(
                    contract,
                    "companion_scope_conflicts_with_project_evidence",
                    "scope_capture_every must be 0 because the active project evidence collector "
                    "owns the Rigol connection and captures synchronized CH1/CH2/CH3 evidence",
                )
        if spec.get("stop_on_infrastructure_error", True) is not True:
            return self._fail_closed(
                contract, "infrastructure_stop_required",
                "live and simulated sweeps must stop on infrastructure failure; invalid shots "
                "remain retryable and never consume requested coverage",
            )
        axes = spec.get("axes") or spec
        # validate any pulse/offset/voltage bounds present in the spec
        w = _span_max(axes.get("pulse_cycles", axes.get("width")))
        if w is not None and w > wmax:
            return self._fail_closed(contract, "width_above_limit",
                                     f"width {w} exceeds enforced limit {wmax}")
        if _span_min(axes.get("pulse_cycles", axes.get("width"))) is not None and _span_min(
            axes.get("pulse_cycles", axes.get("width"))
        ) < 1:
            return self._fail_closed(contract, "width_below_limit",
                                     "pulse_cycles must be at least 1")
        off = _span_max(axes.get("ext_offset"))
        if off is not None and off > omax:
            return self._fail_closed(contract, "offset_above_limit",
                                     f"ext_offset {off} exceeds enforced limit {omax}")
        off_min = _span_min(axes.get("ext_offset"))
        configured_off_min = self._limit("glitch", "ext_offset_min", default=0)
        if off_min is not None and off_min < configured_off_min:
            return self._fail_closed(contract, "offset_below_limit",
                                     f"ext_offset {off_min} is below {configured_off_min}")
        v = _span_max(axes.get("voltage"))
        if v is not None and v > vmax:
            return self._fail_closed(contract, "voltage_above_limit",
                                     f"voltage {v} exceeds enforced limit {vmax}")
        num_glitches = int(spec.get("num_glitches", 1))
        if num_glitches > nmax:
            return self._fail_closed(contract, "num_glitches_above_limit",
                                     f"num_glitches {num_glitches} exceeds enforced limit {nmax}")
        mosfet_values = axes.get("mosfet", spec.get("mosfet"))
        if self._limit("glitch", "hp_lp_both_forbidden", default=False):
            values = mosfet_values if isinstance(mosfet_values, (list, tuple)) else [mosfet_values]
            if "both" in values:
                return self._fail_closed(contract, "both_mosfets_forbidden",
                                         "the active project forbids HP+LP together")
        return Decision(True, "executed", contract.danger, effective=args,
                        detail=f"within limits (pulse≤{wmax}, offset≤{omax}, one event, "
                               f"≤{rate}/s, Vcc≤{vmax})")

    def _check_set_next_parameters(self, contract, args, ctx) -> Decision:
        wmax = self._limit("glitch", "pulse_cycles_max",
                           default=self._limit("glitch", "width_cycles_max"))
        omax = self._limit("glitch", "ext_offset_max")
        omin = self._limit("glitch", "ext_offset_min", default=0)
        nmax = self._limit("glitch", "num_glitches_max")
        vmax = self._limit("target_power", "vcc_max_v")
        if None in (wmax, omax, nmax, vmax):
            return self._fail_closed(contract, "limits_missing",
                                     "set_next_parameters limits not configured")
        p = args.get("params", args)
        w = p.get("pulse_cycles", p.get("width"))
        num_glitches, v = p.get("num_glitches", 1), p.get("voltage")
        # Only the hardware ext_offset (glitch-clock cycles) is bounded here. A generic logical
        # `offset` axis (e.g. a timing coordinate in ns) is NOT the crowbar delay and is left to
        # the caller's calibration — bounding it against ext_offset_max would be a unit mismatch.
        off = p.get("ext_offset")
        if w is not None and w > wmax:
            return self._fail_closed(contract, "width_above_limit",
                                     f"width {w} exceeds enforced limit {wmax}")
        if off is not None and (off > omax or off < omin):
            return self._fail_closed(contract, "offset_above_limit",
                                     f"ext_offset {off} outside [{omin},{omax}]")
        if num_glitches is not None and num_glitches > nmax:
            return self._fail_closed(contract, "num_glitches_above_limit",
                                     f"num_glitches {num_glitches} exceeds enforced limit {nmax}")
        if (self._limit("glitch", "hp_lp_both_forbidden", default=False)
                and (p.get("mosfet") == "both" or (p.get("hp") and p.get("lp")))):
            return self._fail_closed(contract, "both_mosfets_forbidden",
                                     "the active project forbids HP+LP together")
        if v is not None and v > vmax:
            return self._fail_closed(contract, "voltage_above_limit",
                                     f"voltage {v} exceeds enforced limit {vmax}")
        return Decision(True, "executed", contract.danger, effective=p,
                        detail=f"params within limits")

    def _check_trigger_recovery(self, contract, args, ctx) -> Decision:
        mins = self._limit("recovery", "min_seconds_between_cycles")
        maxpm = self._limit("recovery", "max_cycles_per_minute")
        if mins is None or maxpm is None:
            return self._fail_closed(contract, "limits_missing", "recovery limits not configured")
        recent = ctx.get("recent_recoveries", [])
        now = ctx.get("now", 0.0)
        if recent and now - recent[-1] < mins:
            return self._fail_closed(contract, "rate_above_limit",
                                     f"recovery rate-limited: <{mins}s since last cycle")
        window = [t for t in recent if now - t < 60]
        if len(window) >= maxpm:
            return self._fail_closed(contract, "rate_above_limit",
                                     f"recovery rate-limited: ≥{maxpm}/min")
        return Decision(True, "executed", contract.danger, effective=args)

    def _check_discard_preserved_target_state(self, contract, args, ctx) -> Decision:
        live_latch = bool(ctx.get("glitcher_bound") and ctx.get("preserved_state"))
        durable_latch = bool(ctx.get("persisted_blocking_state"))
        if not live_latch and not durable_latch and not ctx.get("glitcher_bound"):
            return self._fail_closed(contract, "glitcher_unbound",
                                     "discard requires a live or durably restored held-state latch")
        if not live_latch and not durable_latch:
            return self._fail_closed(contract, "no_preserved_state",
                                     "there is no live or durably restored held target state")
        expected = "DISCARD_PRESERVED_TARGET_STATE"
        if args.get("acknowledgement") != expected:
            return self._fail_closed(contract, "acknowledgement_mismatch",
                                     f"exact acknowledgement required: {expected}")
        return Decision(True, "executed", contract.danger, effective=args,
                        detail="acknowledged irreversible loss of the preserved volatile state")

    def _check_move_stage(self, contract, args, ctx) -> Decision:
        soft = self._limit("stage", "soft_limits")
        if soft is None:
            return self._fail_closed(contract, "limits_missing", "stage soft-limits not configured")
        for ax in ("x", "y", "z"):
            v = args.get(ax)
            lim = soft.get(ax)
            if v is not None and lim and not (lim[0] <= v <= lim[1]):
                return self._fail_closed(contract, "position_outside_soft_limits",
                                         f"{ax}={v} outside soft-limit {lim}")
        return Decision(True, "executed", contract.danger, effective=args)

    # -- SCOPE DANGER ----------------------------------------------------------------
    def _check_scope_channel_configure(self, contract, args, ctx) -> Decision:
        rated = self._limit("scope_input", "rated_max_input_v")
        if rated is None:
            rated = ctx.get("rated_max_input_v")
        if rated is None:
            return self._fail_closed(contract, "rated_max_input_unknown",
                                     "rated_max_input unknown; input-integrity fails closed")
        probe = args.get("probe_ratio")
        if probe is None:
            return self._fail_closed(contract, "probe_ratio_unset",
                                     "probe_ratio required; refusing to report untrusted voltages")
        scale = args.get("scale_v_per_div")
        if scale is not None:
            full_scale = scale * 8 * probe   # 8 vertical divisions
            if full_scale > rated * probe:
                return self._fail_closed(contract, "scale_implies_over_rated_input",
                    f"scale implies {full_scale:.1f}V > rated {rated*probe:.1f}V at {probe}x probe")
        eff = dict(args)
        eff["max_safe_measurement_v"] = rated * probe
        return Decision(True, "executed", contract.danger, effective=eff,
                        detail=f"probe {probe}x, max safe {rated*probe:.0f}V")

    def _check_scope_source_configure(self, contract, args, ctx) -> Decision:
        amax = self._limit("scope_source", "amplitude_vpp_max")
        omax = self._limit("scope_source", "offset_v_abs_max")
        fmax = self._limit("scope_source", "frequency_hz_max")
        if None in (amax, omax, fmax):
            return self._fail_closed(contract, "limits_missing", "AWG limits not configured")
        if not args.get("load_impedance"):
            return self._fail_closed(contract, "load_undeclared", "declared load required (Hi-Z/50Ω)")
        amp = args.get("amplitude_vpp", 0)
        off = args.get("offset_v", 0)
        freq = args.get("frequency_hz", 0)
        if amp > amax:
            return self._fail_closed(contract, "amplitude_or_offset_outside_limits",
                                     f"{amp} Vpp exceeds enforced limit {amax} Vpp")
        if abs(off) > omax:
            return self._fail_closed(contract, "amplitude_or_offset_outside_limits",
                                     f"offset {off}V exceeds enforced |limit| {omax}V")
        if freq > fmax:
            return self._fail_closed(contract, "amplitude_or_offset_outside_limits",
                                     f"{freq}Hz exceeds enforced limit {fmax}Hz")
        return Decision(True, "executed", contract.danger, effective=args)

    def _check_scope_source_output(self, contract, args, ctx) -> Decision:
        amax = self._limit("scope_source", "amplitude_vpp_max")
        if amax is None:
            return self._fail_closed(contract, "limits_missing", "AWG limits not configured")
        if args.get("enable"):
            load = args.get("load_impedance")
            if not load:
                return self._fail_closed(contract, "load_undeclared",
                                         "enabling output requires declared passive load")
            if args.get("load_driven"):
                return self._fail_closed(contract, "back_drive_generator",
                                         "refusing to enable output into a driven/powered node")
        return Decision(True, "executed", contract.danger, effective=args)


def _span_max(v):
    if v is None:
        return None
    if isinstance(v, (list, tuple)):
        return max(v)
    if isinstance(v, dict):
        return v.get("max", v.get("hi"))
    return v


def _span_min(v):
    if v is None:
        return None
    if isinstance(v, (list, tuple)):
        return min(v)
    if isinstance(v, dict):
        return v.get("min", v.get("lo"))
    return v
