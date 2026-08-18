"""Control-plane tools — DUT-facing, all CAUTION (spec §11.3, §18.5).

They actuate the rig: glitch a live target, cut its power, move a probe. No arm token (R4). Guarded
by code-enforced rig limits + dry_run + audit, and FAIL CLOSED when limits are unconfigured.
"""
from __future__ import annotations

import asyncio
import time
from typing import Annotated, Any, Literal

from pydantic import Field

from . import anns, meta
from .rig_state import target_state_refusal


def _rig_busy(exc: Exception, operation: str) -> dict:
    return {"ok": False, "refused": True, "reason": "rig_operation_in_progress",
            "violated_rule": "rig_operation_in_progress", "operation": operation,
            "detail": str(exc)}


def register(srv, core):
    store = core.store
    engine = core.sweep_engine

    @srv.tool(name="control_sweep", title="Validate or control a bounded glitch sweep",
              description="Validate, start, pause, resume, stop, or inspect one persisted sweep. "
              "The stored parameter plan is immutable; create a new sweep to change any setting. "
              "start may actuate real hardware; always call it first with dry_run=true. The live path "
              "requires the active target acknowledgment, passing preflight/connection checks, enforced "
              "voltage/pulse/ext-offset/event-count/rate bounds, single-shot-safe disarm, and audit logging. "
              "Live start/resume refuses while target state is preserved or unknown-held. "
              "It continues past connector-classified non-goals; it preserves/stops on a fully "
              "confirmed result or ambiguous partial evidence, and stops on infrastructure failure.",
              annotations=anns(destructive=False, open_world=True),
              meta=meta("CAUTION", "control", 500,
                        safety={"enforced": "glitch width/repeat/rate/vcc", "dry_run": True,
                                "fails_closed": "limits_missing|glitcher_unbound"}))
    async def control_sweep(
        action: Annotated[Literal["start", "pause", "resume", "stop", "status"], Field(description=
            "Lifecycle action. Use status for progress; start executes the immutable stored plan.")],
        sweep_id: Annotated[str, Field(description=
            "Existing sweep ID created by define_sweep. Never substitute a campaign or session ID.")],
        param_spec: Annotated[dict | None, Field(description=
            "Backward-compatible equality check only. Omit normally; a value differing from the stored plan is refused.")] = None,
        dry_run: Annotated[bool, Field(description=
            "For start: validate limits, effective plan, and prerequisites without arming or pulsing.")] = False,
        max_attempts: Annotated[int | None, Field(ge=1, description=
            "Optional hard cap for this invocation; it may only narrow the stored plan.")] = None,
    ) -> dict:
        action = action.lower()
        if action in {"start", "resume"} and not dry_run:
            refusal = target_state_refusal(core, f"control_sweep:{action}")
            if refusal:
                return refusal
        if action == "start":
            if dry_run:
                # dry-run is validate-only; run inline so the refusal/plan returns directly
                return await engine.run_sweep(sweep_id, param_spec, dry_run=True,
                                              max_attempts=max_attempts)
            return engine.start(sweep_id, param_spec, dry_run, max_attempts)
        if action == "pause":
            return engine.pause(sweep_id)
        if action == "resume":
            return engine.resume(sweep_id)
        if action == "stop":
            return engine.stop(sweep_id)
        if action == "status":
            sw = store.get_sweep(sweep_id)
            return {"ok": True, "sweep": sw, "running": engine.is_running(sweep_id)}
        return {"ok": False, "error": f"unknown action {action}"}

    @srv.tool(name="preflight_check", title="Run staged rig and connector preflight",
              description="Actuate a no-glitch preflight before every live epoch. It verifies the "
              "configured Husky identity and readbacks, power/reset release, project connector "
              "and negative baseline, and configured oscilloscope signals. Returns named stage evidence "
              "and fails closed on a bad Husky or connector connection. Refuses rather than touching a "
              "preserved or unknown-held target.",
              annotations=anns(destructive=False, open_world=True),
              meta=meta("CAUTION", "control", 300,
                        safety={"note": "scope read + connector bring-up (no glitch)"}))
    async def preflight_check() -> dict:
        from ..app_core import RigBusyError
        from ..domain.preflight import Preflight
        refusal = target_state_refusal(core, "preflight_check")
        if refusal:
            core.auditor.record("preflight_check", "CAUTION", {}, "refused",
                                violated_rule="preserved_target_state_interlock")
            return refusal
        try:
            async with core.exclusive_rig_operation("preflight_check"):
                core.auditor.record("preflight_check", "CAUTION", {}, "executed")
                return await Preflight(core).check()
        except RigBusyError as exc:
            core.auditor.record("preflight_check", "CAUTION", {}, "refused",
                                violated_rule="rig_operation_in_progress")
            return _rig_busy(exc, "preflight_check")

    @srv.tool(
        name="inspect_preserved_target_state",
        title="Re-read a preserved target with its private connector",
        description=(
            "Run one read-only observation through the active private connector while a candidate "
            "state is preserved. It does not power-cycle, pulse, reset, halt, resume, or write target "
            "memory. Unknown-held states whose power/reset must remain untouched are refused."
        ),
        annotations=anns(read_only=True, open_world=True, idempotent=True),
        meta=meta("CAUTION", "control", 3000,
                  safety={"target_writes": False, "reset": False, "halt": False}),
    )
    async def inspect_preserved_target_state() -> dict:
        from ..app_core import RigBusyError
        from .rig_state import target_state_interlock

        state = target_state_interlock(core)
        glitcher = core.glitcher
        if (
            state.get("adapter_preserved") is not True
            or state.get("preserve_leave_io_unchanged") is True
            or glitcher is None
            or not getattr(glitcher, "connected", False)
        ):
            return {
                "ok": False,
                "refused": True,
                "reason": "no_safely_rereadable_preserved_adapter_state",
                "target_state": state,
            }
        connection = getattr(glitcher, "connection", None)
        capabilities = getattr(connection, "capabilities", None)
        if connection is None or capabilities is None:
            return {"ok": False, "refused": True,
                    "reason": "active_adapter_has_no_private_connector"}
        unsafe = any(bool(getattr(capabilities, name, False)) for name in (
            "target_memory_writes", "persistent_target_writes", "target_reset",
            "target_halt", "target_resume",
        ))
        if not bool(getattr(capabilities, "read_only", False)) or unsafe:
            return {"ok": False, "refused": True,
                    "reason": "connector_is_not_preservation_safe"}
        try:
            async with core.exclusive_rig_operation("inspect_preserved_target_state"):
                reading = await asyncio.to_thread(
                    connection.read, {"phase": "preserved_state_inspection"}
                )
                result = {"ok": True, "reading": reading.as_reading()}
        except RigBusyError as exc:
            return _rig_busy(exc, "inspect_preserved_target_state")
        core.auditor.record(
            "inspect_preserved_target_state", "CAUTION", {}, "executed",
            result={"verdict": ((result.get("reading") or {}).get("verdict"))},
        )
        core.bus.publish("preserved_target_state_inspected", {
            "ok": result.get("ok"),
            "verdict": ((result.get("reading") or {}).get("verdict")),
        })
        return {**result, "target_state": target_state_interlock(core)}

    @srv.tool(name="discover_timing", title="Measure the target timing reference",
              description="Run the connector's no-glitch baseline event and capture the project profile's "
              "trigger and activity signals. Returns a measured timing window and suggested ext_offset range. "
              "Use this physical result—not nominal host delays—as the timing authority. Refuses when "
              "the project evidence collector owns the Rigol; that project must use its preflight and "
              "profile-managed known envelope. Also refuses when target state is held. Audited; does not arm the crowbar.",
              annotations=anns(destructive=False, open_world=True),
              meta=meta("CAUTION", "control", 320,
                        safety={"note": "scope read + target reboot only (no glitch)", "audited": True}))
    async def discover_timing(
        window_s: Annotated[float | None, Field(gt=0, description=
            "Acquisition window in seconds. Omit to use the active target profile.")] = None,
        trigger_channel: Annotated[int | None, Field(ge=1, le=4, description=
            "Scope channel carrying the reference trigger. Omit to use the project profile mapping.")] = None,
        signal_channel: Annotated[int | None, Field(ge=1, le=4, description=
            "Scope channel carrying rail/activity. Omit to use the project profile mapping.")] = None,
    ) -> dict:
        from ..app_core import RigBusyError
        from .scope import companion_scope_policy
        refusal = target_state_refusal(core, "discover_timing")
        if refusal:
            core.auditor.record("discover_timing", "CAUTION", {"window_s": window_s},
                                "refused", violated_rule="preserved_target_state_interlock")
            return refusal
        policy = companion_scope_policy(core)
        if policy["project_evidence_owned"]:
            core.auditor.record("discover_timing", "CAUTION", {"window_s": window_s},
                                "refused", violated_rule="project_evidence_owns_rigol")
            return {"ok": False, "refused": True,
                    "reason": "project_evidence_owns_rigol",
                    "detail": "Use the project preflight and its declared physical acceptance window; do not open a companion Rigol session.",
                    "policy": policy}
        from ..domain.timing_discovery import TimingDiscovery
        try:
            async with core.exclusive_rig_operation("discover_timing"):
                core.auditor.record("discover_timing", "CAUTION", {"window_s": window_s}, "executed")
                return await TimingDiscovery(core).characterize(window_s=window_s,
                                                                trigger_channel=trigger_channel,
                                                                signal_channel=signal_channel)
        except RigBusyError as exc:
            core.auditor.record("discover_timing", "CAUTION", {"window_s": window_s}, "refused",
                                violated_rule="rig_operation_in_progress")
            return _rig_busy(exc, "discover_timing")

    @srv.tool(name="run_handoff", title="Handoff unavailable without a connector hook",
              description="Compatibility stub that always refuses. No target handoff is offered until "
              "the active private connector supplies a reviewed preservation-safe implementation. "
              "Inspect or export persisted evidence instead.",
              annotations=anns(read_only=True),
              meta=meta("SAFE", "invisible", 300,
                        safety={"fails_closed": "connector_handoff_not_implemented"}))
    def run_handoff(
        dry_run: Annotated[bool, Field(description=
            "Retained for compatibility; the tool refuses in both preview and live modes.")] = False,
        out_dir: Annotated[str | None, Field(description=
            "Ignored compatibility argument; no handoff files are written.")] = None,
        confirmed_attempt_id: Annotated[int | None, Field(ge=1, description=
            "Optional confirmed attempt reference included in the refusal for traceability.")] = None,
    ) -> dict:
        return {"ok": False, "refused": True,
                "reason": "connector_handoff_not_implemented",
                "detail": "Preserve the target and inspect or export persisted evidence instead.",
                "dry_run": bool(dry_run), "confirmed_attempt_id": confirmed_attempt_id,
                "out_dir": out_dir}

    @srv.tool(name="acknowledge_target", title="Acknowledge active target safety limits",
              description="Before a live campaign, echo the active target safety envelope's pulse_cycles_max, ext_offset_max, "
              "num_glitches_max, and vcc_max_v exactly. Mismatches refuse and are audited. This is a safety review gate, "
              "not evidence that the Husky/connector is healthy; preflight_check is still required.",
              annotations=anns(destructive=False, open_world=False),
              meta=meta("CAUTION", "control", 350,
                        safety={"gate": "control_sweep target_unacknowledged", "audited": True}))
    def acknowledge_target(
        target_model: Annotated[str, Field(description=
            "Exact active rig target_model returned by get_workflow_state/describe_schema.")],
        stated: Annotated[dict | None, Field(description=
            "Exact reviewed limits: pulse_cycles_max, ext_offset_max, num_glitches_max, and vcc_max_v.")] = None,
    ) -> dict:
        return core.acknowledge_target(target_model, stated or {})

    @srv.tool(
        name="discard_preserved_target_state",
        title="Discard a preserved volatile target state",
        description=(
            "Irreversibly power off a target state that this GlitchLab process deliberately "
            "preserved after full confirmation, incomplete connector evidence, or an unclassified "
            "post-shot failure. This is never automatic. It requires the exact acknowledgement "
            "DISCARD_PRESERVED_TARGET_STATE and should be used only after evidence is saved or "
            "when intentionally beginning a new independent reproduction epoch."
        ),
        annotations=anns(destructive=True, open_world=True),
        meta=meta("DANGER", "control", 300,
                  safety={"irreversible": "volatile candidate state is power-cycled",
                          "dry_run": True, "audited": True}),
    )
    async def discard_preserved_target_state(
        acknowledgement: Annotated[str, Field(description=
            "Exact literal DISCARD_PRESERVED_TARGET_STATE after reviewing the preserved evidence.")],
        dry_run: Annotated[bool, Field(description=
            "Validate the interlock and show the loss operation without changing target state.")] = True,
    ) -> dict:
        from .rig_state import target_state_interlock

        state = target_state_interlock(core)
        glitcher = core.glitcher
        context = {
            "glitcher_bound": bool(glitcher is not None and glitcher.connected),
            "preserved_state": bool(getattr(glitcher, "_preserve", False)),
            "persisted_blocking_state": bool(state.get("blocking")),
        }
        args = {"acknowledgement": acknowledgement}
        decision = core.safety.check(
            "discard_preserved_target_state", args, dry_run=dry_run, context=context
        )
        core.auditor.record_decision("discard_preserved_target_state", args, decision)
        if not decision.allowed and decision.decision == "refused":
            return decision.refusal_dict()
        if decision.decision == "dry_run":
            return {"ok": True, "dry_run": True,
                    "would_discard": True,
                    "preserve_reason": (
                        getattr(glitcher, "_preserve_reason", None)
                        or state.get("reason")
                    ),
                    "target_state": state}
        from ..app_core import RigBusyError
        try:
            async with core.exclusive_rig_operation("discard_preserved_target_state"):
                if glitcher is None or not glitcher.connected:
                    # Restored persisted state has no in-memory adapter latch.
                    # Connecting configures the probes but does not alter target
                    # power/reset; latch the current unknown state before the
                    # explicitly acknowledged destructive transition.
                    glitcher = await asyncio.to_thread(core.ensure_glitcher, True)
                if not hasattr(glitcher, "discard_preserved_state"):
                    return {"ok": False, "refused": True,
                            "reason": "active_adapter_has_no_preserved_state_discard_api"}
                if not bool(getattr(glitcher, "_preserve", False)):
                    if not state.get("blocking") or not hasattr(glitcher, "hold_current_state"):
                        return {"ok": False, "refused": True,
                                "reason": "restored_held_state_could_not_be_latched"}
                    glitcher.hold_current_state(
                        "restored-persisted-unresolved-state-explicit-discard"
                    )
                result = await asyncio.to_thread(glitcher.discard_preserved_state, acknowledgement)
        except RigBusyError as exc:
            return _rig_busy(exc, "discard_preserved_target_state")
        except Exception as exc:
            return {"ok": False, "refused": True,
                    "reason": "preserved_state_discard_setup_failed",
                    "error": repr(exc), "target_state": state}
        if result.get("ok") is True and state.get("sweep_id"):
            core.store.set_sweep_status(state["sweep_id"], "aborted")
            core.store.set_session_status_for_sweep(state["sweep_id"], "aborted")
            core.active.pop("restored_unresolved_state", None)
        core.bus.publish("preserved_target_state_discarded", {
            "ok": result.get("ok"), "prior_reason": result.get("prior_reason")
        })
        return result

    @srv.tool(name="set_next_parameters", title="Adaptive tuple queue is not implemented",
              description="Compatibility stub that always refuses. No sweep engine consumes this "
              "queue, so accepting values would falsely imply that a live campaign changed.",
              annotations=anns(read_only=True),
              meta=meta("SAFE", "invisible", 300,
                        safety={"fails_closed": "adaptive_parameter_queue_not_implemented"}))
    def set_next_parameters(
        params: Annotated[dict, Field(description=
            "Complete next tuple using the active sweep's parameter names and hardware units.")],
        dry_run: Annotated[bool, Field(description="Validate the tuple without queueing it.")] = False,
    ) -> dict:
        return {"ok": False, "refused": True,
                "reason": "adaptive_parameter_queue_not_implemented",
                "params": params, "dry_run": bool(dry_run)}

    @srv.tool(name="trigger_recovery", description="Run the private connector's reviewed target recovery "
              "operation. CAUTION: rate-limited; fails closed if limits are unconfigured or "
              "target state is preserved/unknown-held. Use explicit discard for intentional state loss.",
              annotations=anns(destructive=True, open_world=True),
              meta=meta("CAUTION", "control", 300, safety={"enforced": "recovery rate-limit"}))
    async def trigger_recovery(
        dry_run: Annotated[bool, Field(description=
            "Validate recovery limits without changing target power/reset.")] = False,
    ) -> dict:
        refusal = target_state_refusal(core, "trigger_recovery")
        if refusal:
            core.auditor.record("trigger_recovery", "CAUTION", {}, "refused",
                                violated_rule="preserved_target_state_interlock")
            return refusal
        now = time.time()
        dec = core.safety.check("trigger_recovery", {}, dry_run=dry_run,
                                context={"recent_recoveries": core.recovery_times, "now": now})
        core.auditor.record_decision("trigger_recovery", {}, dec)
        if not dec.allowed and dec.decision == "refused":
            return dec.refusal_dict()
        if dec.decision == "dry_run":
            return {"ok": True, "dry_run": True, "detail": "would power-cycle target"}
        from ..app_core import RigBusyError

        def _recover():
            g = core.ensure_glitcher(connect=True)
            return g.power_cycle() if g else {"ok": False, "error": "no glitcher"}

        try:
            async with core.exclusive_rig_operation("trigger_recovery"):
                core.recovery_times.append(now)
                res = await asyncio.to_thread(_recover)
        except RigBusyError as exc:
            return _rig_busy(exc, "trigger_recovery")
        core.bus.publish("recovery", {"result": res})
        return {"ok": res.get("ok", False), **res}

    @srv.tool(name="flash_target", description="Flash a firmware .hex/.bin to the DUT via the "
              "glitcher's programmer (spec §25). CAUTION: touches the DUT (does not glitch); audited. "
              "Live flashing refuses while target state is preserved/unknown-held.",
              annotations=anns(destructive=True, open_world=True),
              meta=meta("CAUTION", "control", 400, safety={"note": "programs the target MCU"}))
    async def flash_target(
        hexfile: Annotated[str, Field(description="Absolute path to the operator-selected firmware image.")],
        mcu: Annotated[str | None, Field(description=
            "Programmer MCU identifier. Omit to use the active target model.")] = None,
        dry_run: Annotated[bool, Field(description="Validate the file and return the plan only.")] = False,
    ) -> dict:
        import os
        mcu = mcu or core.rig.target_model
        if not os.path.exists(hexfile):
            return {"ok": False, "error": f"firmware not found: {hexfile}"}
        if not dry_run:
            refusal = target_state_refusal(core, "flash_target")
            if refusal:
                core.auditor.record("flash_target", "CAUTION",
                                    {"hexfile": hexfile, "mcu": mcu}, "refused",
                                    violated_rule="preserved_target_state_interlock")
                return refusal
        core.auditor.record("flash_target", "CAUTION", {"hexfile": hexfile, "mcu": mcu},
                            "dry_run" if dry_run else "executed")
        if dry_run:
            return {"ok": True, "dry_run": True, "would_flash": hexfile, "mcu": mcu}
        from ..app_core import RigBusyError

        def _flash():
            g = core.ensure_glitcher(connect=True)
            return g.program_target(hexfile, mcu) if g else {"ok": False, "error": "no glitcher"}

        try:
            async with core.exclusive_rig_operation("flash_target"):
                res = await asyncio.to_thread(_flash)
        except RigBusyError as exc:
            return _rig_busy(exc, "flash_target")
        core.bus.publish("firmware_flashed", {"hexfile": hexfile, "mcu": mcu, "ok": res.get("ok")})
        return res

    @srv.tool(name="move_stage", description="Position an XYZ EM/laser stage. CAUTION: soft-limits "
              "enforced; fails closed if soft-limits unconfigured.",
              annotations=anns(destructive=False, open_world=True),
              meta=meta("CAUTION", "control", 300, safety={"enforced": "stage soft-limits"}))
    def move_stage(
        x: Annotated[float | None, Field(description="Requested X coordinate in configured stage units.")] = None,
        y: Annotated[float | None, Field(description="Requested Y coordinate in configured stage units.")] = None,
        z: Annotated[float | None, Field(description="Requested Z coordinate in configured stage units.")] = None,
        dry_run: Annotated[bool, Field(description=
            "Validate configured soft limits without moving a stage.")] = False,
    ) -> dict:
        dec = core.safety.check("move_stage", {"x": x, "y": y, "z": z}, dry_run=dry_run)
        core.auditor.record_decision("move_stage", {"x": x, "y": y, "z": z}, dec)
        if not dec.allowed and dec.decision == "refused":
            return dec.refusal_dict()
        if dec.decision == "dry_run":
            return {"ok": True, "dry_run": True, "would_move_to": {"x": x, "y": y, "z": z}}
        return {"ok": True, "moved_to": {"x": x, "y": y, "z": z},
                "note": "no stage attached in this rig" if not core.rig.limit("stage")
                else "moved"}
