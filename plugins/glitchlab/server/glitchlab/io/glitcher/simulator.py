"""Deterministic simulated glitcher + target (spec §24 testing).

Emits reproducible outcome distributions with a success cluster centered at a configurable
(width, offset), so the whole pipeline (record→classify→store→analyze→render) runs with no rig.
The success probability is a 2D Gaussian bump; reset/exception surround the success ridge.
"""
from __future__ import annotations

import hashlib
import math
import time

from .base import GlitchParams, GlitchResult, GlitcherAdapter


class SimulatorGlitcher(GlitcherAdapter):
    id = "simulator"
    is_simulator = True

    def __init__(self, success_center=(7.0, 1000.0), success_sigma=(1.5, 320.0),
                 peak_rate=0.18, seed=1234, expected_output="BASELINE_OK\n",
                 width_range=None, offset_range=None, **_):
        self.success_center = success_center
        self.success_sigma = success_sigma
        self.peak_rate = peak_rate
        self.seed = seed
        self._connected = False
        self.expected = expected_output
        self.width_range = list(width_range) if width_range else None
        self.offset_range = list(offset_range) if offset_range else None
        self.fallback_reason = None
        self._programmed = None
        self._attempt_index = 0

    def connect(self) -> dict:
        self._connected = True
        return {"ok": True, "adapter": self.id, "simulator": True,
                "note": self.fallback_reason or "deterministic simulator"}

    def disconnect(self) -> None:
        self._connected = False

    @property
    def connected(self) -> bool:
        return self._connected

    def capabilities(self) -> dict:
        return {"adapter": self.id, "simulator": True, "glitch": "voltage-crowbar (simulated)",
                "width_range": self.width_range, "offset_range": self.offset_range,
                "success_center": self.success_center, "programmed": self._programmed}

    def prepare(self) -> dict:
        return {"ok": True, "simulator": True, "baseline_verdict": "no-effect"}

    def program_target(self, hexfile: str, mcu: str = "") -> dict:
        self._programmed = {"hexfile": hexfile, "mcu": mcu}
        return {"ok": True, "simulator": True, "flashed": hexfile, "mcu": mcu}

    def _rand(self, params: GlitchParams) -> float:
        self._attempt_index += 1
        key = f"{self.seed}:{self._attempt_index}:{params.width}:{params.offset}:{params.repeat}"
        h = hashlib.sha256(key.encode()).hexdigest()
        return int(h[:8], 16) / 0xFFFFFFFF

    def attempt(self, params: GlitchParams, payload: bytes | None = None) -> GlitchResult:
        t0 = time.time()
        cx, cy = self.success_center
        sx, sy = self.success_sigma
        off = params.ext_offset if params.ext_offset is not None else params.offset
        d2 = ((params.width - cx) / sx) ** 2 + ((off - cy) / sy) ** 2
        p_success = self.peak_rate * math.exp(-0.5 * d2)
        r = self._rand(params)
        # nearby ridge -> reset/exception; far -> no-effect
        ring = math.exp(-0.5 * (math.sqrt(d2) - 1.0) ** 2)
        if r < p_success:
            outcome = "success"
            text = "FAULT_EFFECT\n"
        elif r < p_success + 0.18 * ring:
            outcome = "exception"
            text = "TARGET_EXCEPTION\n"
        elif r < p_success + 0.35 * ring:
            outcome = "reset"
            text = "\x00\x00TARGET_RESET\r\n"
        else:
            outcome = "no-effect"
            text = self.expected
        dur = (time.time() - t0) * 1000 + 4.0
        return GlitchResult(
            outcome_hint=outcome,
            raw_captures=[{"channel": "uart", "payload": text, "encoding": "utf-8"}],
            oracle_readings=[{"oracle_name": "sim-uart", "verdict": outcome, "latency_ms": 2.0}],
            env_sample={"ambient_temp_c": 24.0 + 0.5 * math.sin(time.time() / 60),
                        "board_temp_c": 31.0},
            duration_ms=dur, reset_detected=(outcome == "reset"), expected=self.expected,
            meta={"p_success": round(p_success, 4), "attempt_valid": True})

    def power_cycle(self) -> dict:
        return {"ok": True, "simulator": True, "action": "power_cycled"}

    def safe_shutdown(self) -> None:
        self._connected = False
