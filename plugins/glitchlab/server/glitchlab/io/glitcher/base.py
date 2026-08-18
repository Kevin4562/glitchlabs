"""Glitcher adapter interface (spec §1.3 thin adapter). GlitchLab integrates existing stacks.

A glitcher adapter turns a parameter tuple into one physical injection attempt against the DUT and
returns the raw target response for classification. Adapters never bypass the Safety Engine — the
control tools call `enforce()` before `attempt()`.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class GlitchParams:
    width: float = 1.0          # legacy alias for pulse_cycles (Husky glitch.repeat)
    offset: float = 0.0         # ext_offset — delay from trigger (glitch-clock cycles)
    voltage: float = 3.3        # target VCC nominal
    repeat: int = 1             # legacy event count; live safety profiles require exactly one
    ext_offset: float | None = None
    hp: bool = False            # high-power crowbar MOSFET
    lp: bool = True             # low-power crowbar MOSFET
    extra: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        d = {"width": self.width, "offset": self.offset, "voltage": self.voltage,
             "repeat": self.repeat, "hp": self.hp, "lp": self.lp}
        if self.ext_offset is not None:
            d["ext_offset"] = self.ext_offset
        d.update(self.extra)
        return d


@dataclass
class GlitchResult:
    outcome_hint: str | None            # adapter's cheap hint; classifier makes the final call
    raw_captures: list[dict] = field(default_factory=list)
    oracle_readings: list[dict] = field(default_factory=list)
    env_sample: dict | None = None
    duration_ms: float = 0.0
    reset_detected: bool = False
    expected: str | None = None
    meta: dict = field(default_factory=dict)


class GlitcherAdapter:
    id = "base"
    is_simulator = True

    def connect(self) -> dict:
        raise NotImplementedError

    def disconnect(self) -> None:
        pass

    @property
    def connected(self) -> bool:
        return False

    def capabilities(self) -> dict:
        return {}

    def program_target(self, hexfile: str, mcu: str = "") -> dict:
        return {"ok": False, "error": "programming not supported by this adapter"}

    def arm(self, params: GlitchParams) -> None:
        """Configure (but do not fire) the next glitch."""

    def attempt(self, params: GlitchParams, payload: bytes | None = None) -> GlitchResult:
        """Perform one injection and return the target's raw response."""
        raise NotImplementedError

    def power_cycle(self) -> dict:
        return {"ok": False, "error": "power_cycle not supported"}

    def safe_shutdown(self) -> None:
        """Drive outputs to a safe disarmed state (glitch disabled)."""
