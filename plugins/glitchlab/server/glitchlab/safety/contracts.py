"""Declarative danger contracts (spec §18.3). Every CAUTION/DANGER tool declares one.

The contract names the danger level, the enforced-limit paths (looked up in rig_config), the
preconditions, the forbidden states, what makes it fail closed, and reversibility. The model sees
exactly which tools can cause harm and the rules for using them correctly (§18.2).
"""
from __future__ import annotations

from dataclasses import dataclass, field

SAFE = "SAFE"
CAUTION = "CAUTION"
DANGER = "DANGER"


@dataclass
class Contract:
    tool: str
    danger: str
    summary: str
    enforced_limits: list[tuple[str, ...]] = field(default_factory=list)   # rig_config limit paths
    preconditions: list[str] = field(default_factory=list)
    forbidden: list[str] = field(default_factory=list)
    fails_closed_if: list[str] = field(default_factory=list)
    dry_run: bool = True
    reversible: bool = True

    def to_metadata(self) -> dict:
        return {
            "danger": self.danger,
            "summary": self.summary,
            "enforced_limits": [".".join(p) for p in self.enforced_limits],
            "preconditions": self.preconditions,
            "forbidden": self.forbidden,
            "fails_closed_if": self.fails_closed_if,
            "dry_run": self.dry_run,
            "reversible": self.reversible,
        }


# -- Control plane (CAUTION) — DUT-facing (§11.3, §18.5) -----------------------------
CONTRACTS: dict[str, Contract] = {
    "control_sweep": Contract(
        "control_sweep", CAUTION,
        "start/pause/resume/stop an automated glitch sweep on real hardware",
        enforced_limits=[("glitch", "pulse_cycles_max"), ("glitch", "ext_offset_max"),
                         ("glitch", "num_glitches_max"),
                         ("rate", "max_attempts_per_second"), ("target_power", "vcc_max_v")],
        preconditions=["rig_limits_present", "glitcher_bound"],
        forbidden=["width_above_limit", "voltage_above_limit", "rate_above_limit"],
        fails_closed_if=["limits_missing", "glitcher_unbound"]),
    "set_next_parameters": Contract(
        "set_next_parameters", CAUTION,
        "feed the next parameter tuple to a live loop (agent-driven adaptive search)",
        enforced_limits=[("glitch", "pulse_cycles_max"), ("glitch", "ext_offset_max"),
                         ("glitch", "num_glitches_max"), ("target_power", "vcc_max_v")],
        preconditions=["rig_limits_present"],
        forbidden=["width_above_limit", "offset_above_limit", "voltage_above_limit"],
        fails_closed_if=["limits_missing"]),
    "trigger_recovery": Contract(
        "trigger_recovery", CAUTION,
        "out-of-band power-cycle a hung/bricked target via the relay",
        enforced_limits=[("recovery", "min_seconds_between_cycles"),
                         ("recovery", "max_cycles_per_minute")],
        preconditions=["rig_limits_present"], forbidden=["rate_above_limit"],
        fails_closed_if=["limits_missing"]),
    "discard_preserved_target_state": Contract(
        "discard_preserved_target_state", DANGER,
        "explicitly power off and destroy a volatile state that GlitchLab preserved",
        preconditions=["glitcher_bound", "preserved_state_present",
                       "exact_loss_acknowledgement"],
        forbidden=["implicit_candidate_loss", "discard_without_acknowledgement"],
        fails_closed_if=["glitcher_unbound", "no_preserved_state",
                         "acknowledgement_mismatch"],
        dry_run=True, reversible=False),
    "move_stage": Contract(
        "move_stage", CAUTION, "position an XYZ EM/laser stage",
        enforced_limits=[("stage", "soft_limits")],
        preconditions=["soft_limits_present"], forbidden=["position_outside_soft_limits"],
        fails_closed_if=["limits_missing"]),

    # -- Scope DANGER (§11.4, §18.4) -------------------------------------------------
    "scope_channel_configure": Contract(
        "scope_channel_configure", DANGER,
        "vertical scale + REQUIRED probe_ratio; over-range refusal (input-integrity)",
        enforced_limits=[("scope_input", "rated_max_input_v")],
        preconditions=["probe_ratio_declared", "rated_max_input_known"],
        forbidden=["report_voltage_without_probe_ratio", "scale_implies_over_rated_input"],
        fails_closed_if=["rated_max_input_unknown", "probe_ratio_unset"]),
    "scope_source_configure": Contract(
        "scope_source_configure", DANGER,
        "configure the AWG (bounded, off-before-config, declared load)",
        enforced_limits=[("scope_source", "amplitude_vpp_max"), ("scope_source", "offset_v_abs_max"),
                         ("scope_source", "frequency_hz_max")],
        preconditions=["output_off_before_reconfigure", "load_impedance_declared"],
        forbidden=["amplitude_or_offset_outside_limits", "enable_output_into_undeclared_load"],
        fails_closed_if=["limits_missing", "load_undeclared"]),
    "scope_source_output": Contract(
        "scope_source_output", DANGER,
        "enable/disable AWG output (auto-off on error/disconnect); no back-drive",
        enforced_limits=[("scope_source", "amplitude_vpp_max")],
        preconditions=["load_declared_passive", "source_configured"],
        forbidden=["enable_into_undeclared_or_driven_load", "back_drive_generator"],
        fails_closed_if=["limits_missing", "load_undeclared"]),
}


def contract_for(tool: str) -> Contract | None:
    return CONTRACTS.get(tool)


def all_contracts_metadata() -> dict[str, dict]:
    return {t: c.to_metadata() for t, c in CONTRACTS.items()}
