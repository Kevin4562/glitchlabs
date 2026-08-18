"""Read-only target-state interlock shared by MCP and viewer surfaces.

The adapter latch is authoritative while this process is alive.  Persisted sweep
status is the durable fallback after a viewer reload or server restart; a stale
running/paused sweep is treated as unknown-held instead of assuming that a
volatile target state is safe to disturb.
"""
from __future__ import annotations

from typing import Any


_PRESERVED_SWEEP_STATUSES = {"candidate-preserved"}
_UNKNOWN_HELD_SWEEP_STATUSES = {"infrastructure-failure"}
_POSSIBLY_LIVE_SWEEP_STATUSES = {"running", "paused"}


def target_state_interlock(core: Any) -> dict[str, Any]:
    """Return a durable, fail-closed summary without connecting to hardware."""
    active = dict(getattr(core, "active", {}) or {})
    sweep_id = active.get("sweep_id")
    sweep: dict[str, Any] = {}
    if sweep_id:
        try:
            sweep = dict(core.store.get_sweep(sweep_id) or {})
        except Exception:
            sweep = {}

    sweep_status = str(sweep.get("status") or "") or None
    try:
        engine_running = bool(
            sweep_id
            and getattr(core, "sweep_engine", None) is not None
            and core.sweep_engine.is_running(sweep_id)
        )
    except Exception:
        engine_running = False

    glitcher = getattr(core, "glitcher", None)
    adapter_preserved = bool(getattr(glitcher, "_preserve", False))
    preserve_reason = getattr(glitcher, "_preserve_reason", None)
    leave_io_unchanged = bool(
        getattr(glitcher, "_preserve_leave_io_unchanged", False)
    )

    state = "clear"
    source = "none"
    reason = None
    if adapter_preserved:
        state = "preserved"
        source = "adapter_latch"
        reason = preserve_reason or "adapter reports a preserved volatile target state"
    elif sweep_status in _PRESERVED_SWEEP_STATUSES:
        state = "preserved"
        source = "persisted_sweep_status"
        reason = "sweep ended with a candidate-preserved target state"
    elif sweep_status in _UNKNOWN_HELD_SWEEP_STATUSES:
        state = "unknown_held"
        source = "persisted_sweep_status"
        reason = "sweep ended on an infrastructure failure; current target state is unknown"
    elif sweep_status in _POSSIBLY_LIVE_SWEEP_STATUSES and not engine_running:
        state = "unknown_held"
        source = "stale_sweep_status"
        reason = (
            f"persisted sweep status is {sweep_status}, but no live sweep task owns it"
        )

    blocking = state != "clear"
    return {
        "state": state,
        "blocking": blocking,
        "preserved": state == "preserved",
        "unknown_held": state == "unknown_held",
        "source": source,
        "reason": reason,
        "sweep_id": sweep_id,
        "sweep_status": sweep_status,
        "engine_running": engine_running,
        "adapter_preserved": adapter_preserved,
        "preserve_reason": preserve_reason,
        "preserve_leave_io_unchanged": leave_io_unchanged,
        "candidate_dir": str(getattr(glitcher, "_candidate_dir", "") or "") or None,
        "blocked_operations": [
            "start_sweep",
            "preflight_check",
            "read_connector",
            "discover_timing",
            "trigger_recovery",
            "flash_target",
            "scope_bind",
            "scope_unbind",
            "scope_access",
        ] if blocking else [],
        "clearance": (
            "Explicit discard is MCP-only, destructive, and requires the exact loss acknowledgement."
            if blocking else None
        ),
    }


def target_state_refusal(core: Any, operation: str) -> dict[str, Any] | None:
    """Return a structured refusal when an operation could disturb held state."""
    state = target_state_interlock(core)
    if not state["blocking"]:
        return None
    return {
        "ok": False,
        "refused": True,
        "reason": "target_state_preserved" if state["preserved"] else "target_state_unknown_held",
        "violated_rule": "preserved_target_state_interlock",
        "operation": operation,
        "detail": state["reason"],
        "target_state": state,
    }
