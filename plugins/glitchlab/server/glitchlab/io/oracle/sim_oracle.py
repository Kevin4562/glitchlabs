"""Deterministic target-observation oracle for offline end-to-end testing."""
from __future__ import annotations

import math
import time
from typing import Optional

from .base import Oracle, OracleCapabilities, OracleReading


class SimOracle(Oracle):
    name = "simulated-connection"
    plugin_id = "simulated_connection"
    read_only = True
    capabilities = OracleCapabilities(
        transport="simulation",
        process_isolated=False,
        read_only=True,
        provides_runtime_liveness=True,
        provides_connection_health=True,
    )

    def __init__(
        self,
        center_ext_offset: float = 1000.0,
        ext_sigma: float = 320.0,
        center_width: float = 7.0,
        width_sigma: float = 1.5,
        peak_rate: float = 0.35,
        project_id: str | None = None,
    ) -> None:
        self.project_id = str(project_id) if project_id is not None else None
        self.center_ext_offset = float(center_ext_offset)
        self.ext_sigma = float(ext_sigma)
        self.center_width = float(center_width)
        self.width_sigma = float(width_sigma)
        self.peak_rate = float(peak_rate)

    @classmethod
    def config_schema(cls) -> dict:
        return {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "center_ext_offset": {"type": "number", "default": 1000.0},
                "ext_sigma": {"type": "number", "exclusiveMinimum": 0, "default": 320.0},
                "center_width": {"type": "number", "default": 7.0},
                "width_sigma": {"type": "number", "exclusiveMinimum": 0, "default": 1.5},
                "peak_rate": {"type": "number", "minimum": 0, "maximum": 1, "default": 0.35},
            },
        }

    def read(self, context: Optional[dict] = None) -> OracleReading:
        started = time.time()
        params = (context or {}).get("params", {}) or {}
        ext_offset = float(params.get("ext_offset", 0) or 0)
        width = float(params.get("width", 0) or 0)
        d_ext = (ext_offset - self.center_ext_offset) / self.ext_sigma
        d_width = (width - self.center_width) / self.width_sigma
        probability = self.peak_rate * math.exp(-0.5 * (d_ext * d_ext + d_width * d_width))
        seed = abs(hash((round(ext_offset, 3), round(width, 3), (context or {}).get("attempt_index", 0))))
        sample = (seed * 2654435761) % 1000 / 1000.0
        if sample < probability:
            verdict, detail = "success", {"observed": True, "sim": True, "probability": round(probability, 4)}
        elif sample < probability + 0.06:
            verdict, detail = "reset", {"sim": True}
        else:
            verdict, detail = "no-effect", {"observed": False, "sim": True, "probability": round(probability, 4)}
        return OracleReading(self.name, verdict, (time.time() - started) * 1000.0, detail)
