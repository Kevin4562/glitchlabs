"""Fail-closed ChipWhisperer-Husky configuration and health helpers.

The older adapters treated many failed writes as optional and then reported the
requested values as though they were hardware readbacks.  These helpers keep
configuration validation small, deterministic, and unit-testable with a fake
scope.  They intentionally know nothing about a specific target.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any


class HuskyConfigurationError(RuntimeError):
    """The connected scope cannot safely execute the requested shot."""


def lite_percent_to_husky_steps(percent: float, phase_shift_steps: int) -> int:
    """Convert CW-Lite phase-percent units to Husky integer PLL phase steps."""
    steps = int(phase_shift_steps)
    if steps <= 0:
        raise ValueError("phase_shift_steps must be positive")
    value = float(percent)
    if not -50.0 <= value <= 50.0:
        raise ValueError("phase percentage must be within [-50, 50]")
    return int(round(value / 100.0 * steps))


def require_one_glitch(scope: Any) -> None:
    """Program and verify exactly one Husky glitch event."""
    scope.glitch.num_glitches = 1
    if int(scope.glitch.num_glitches) != 1:
        raise HuskyConfigurationError(
            f"Husky refused one-pulse mode: read back {scope.glitch.num_glitches!r}"
        )


def read_husky_health(scope: Any) -> dict[str, Any]:
    """Return lock/alarm state without converting missing telemetry into success."""
    try:
        xadc_status = str(scope.XADC.status)
        result = {
            "glitch_mmcm_locked": bool(scope.glitch.mmcm_locked),
            "clkgen_locked": bool(scope.clock.clkgen_locked),
            "adc_locked": bool(scope.clock.adc_locked),
            "xadc_status": xadc_status,
        }
    except Exception as exc:
        return {"ok": False, "error": f"Husky health telemetry unavailable: {exc!r}"}
    result["xadc_ok"] = xadc_status.strip().lower() == "good"
    result["ok"] = bool(
        result["glitch_mmcm_locked"]
        and result["clkgen_locked"]
        and result["adc_locked"]
        and result["xadc_ok"]
    )
    return result


def require_husky_health(scope: Any, stage: str) -> dict[str, Any]:
    health = read_husky_health(scope)
    if health.get("ok") is not True:
        raise HuskyConfigurationError(f"Husky health failed at {stage}: {health}")
    return health


def configure_replicant_phase(
    scope: Any, *, width_percent: float = 40.0, offset_percent: float = -45.0
) -> dict[str, Any]:
    """Apply the published Lite phase values using Husky's actual unit system."""
    if not hasattr(scope.glitch, "phase_shift_steps"):
        raise HuskyConfigurationError("connected scope is not a ChipWhisperer Husky")
    steps = int(scope.glitch.phase_shift_steps)
    width = lite_percent_to_husky_steps(width_percent, steps)
    offset = lite_percent_to_husky_steps(offset_percent, steps)
    scope.glitch.width = width
    scope.glitch.offset = offset
    width_rb = int(scope.glitch.width)
    offset_rb = int(scope.glitch.offset)
    if (width_rb, offset_rb) != (width, offset):
        raise HuskyConfigurationError(
            "Husky phase readback mismatch: "
            f"requested {(width, offset)}, read {(width_rb, offset_rb)}"
        )
    return {
        "phase_shift_steps": steps,
        "lite_percent": {"width": float(width_percent), "offset": float(offset_percent)},
        "requested_steps": {"width": width, "offset": offset},
        "readback_steps": {"width": width_rb, "offset": offset_rb},
    }


def require_readback(name: str, requested: Any, actual: Any) -> Any:
    """Raise with a useful field name when a critical setting was coerced."""
    if actual != requested:
        raise HuskyConfigurationError(
            f"Husky {name} readback mismatch: requested {requested!r}, read {actual!r}"
        )
    return actual
