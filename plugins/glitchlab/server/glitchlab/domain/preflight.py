"""Project-owned, fail-closed pre-campaign validation.

The adapter selected by the active project owns its power/reset sequence, oracle,
and synchronized evidence devices. Running a second generic scope/oracle routine
here could change synchronized instrument settings after the target adapter had
validated them and invalidate the no-glitch baseline.
"""
from __future__ import annotations

import asyncio
from typing import Any


class Preflight:
    def __init__(self, core) -> None:
        self.core = core

    async def check(self, signal_vmin: float = 0.3) -> dict[str, Any]:
        del signal_vmin  # compatibility; project policy owns the actual thresholds
        core = self.core
        stages: list[dict[str, Any]] = []
        try:
            glitcher = core.ensure_glitcher(connect=True)
            connection = dict(core.glitcher_connect_result or {})
            connection_ok = bool(
                connection.get("ok") is True
                and (
                    connection.get("simulator") is True
                    or ((connection.get("health") or {}).get("ok") is True)
                )
            )
            stages.append({
                "name": "glitcher_connection",
                "ok": connection_ok,
                "evidence": connection,
            })
            if not connection_ok:
                raise RuntimeError("glitcher identity/readback/health gate did not pass")

            if not hasattr(glitcher, "prepare"):
                raise RuntimeError("active glitcher has no project preflight/prepare contract")
            preparation = await asyncio.to_thread(glitcher.prepare)
            preparation_ok = isinstance(preparation, dict) and preparation.get("ok") is True
            stages.append({
                "name": "project_no_glitch_baseline",
                "ok": preparation_ok,
                "evidence": preparation,
            })
            if not preparation_ok:
                raise RuntimeError(f"project no-glitch baseline failed: {preparation!r}")

            result = {
                "ok": True,
                "project_id": getattr(glitcher, "project_id", None),
                "stages": stages,
                "problems": [],
                "warnings": [],
                "connection": connection,
                "baseline": preparation,
            }
        except Exception as exc:
            try:
                if core.glitcher is not None:
                    core.glitcher.safe_shutdown()
            except Exception:
                pass
            preserved_evidence = getattr(
                core.glitcher, "_last_preflight_evidence", None
            ) if core.glitcher is not None else None
            if isinstance(preserved_evidence, dict) and preserved_evidence:
                stages.append({
                    "name": "project_no_glitch_baseline",
                    "ok": False,
                    "evidence": preserved_evidence,
                })
            result = {
                "ok": False,
                "project_id": getattr(core.glitcher, "project_id", None),
                "stages": stages,
                "problems": [repr(exc)],
                "warnings": [],
            }

        core.bus.publish("preflight_result", result)
        if not result["ok"]:
            core.bus.publish(
                "campaign_error", {"error": "preflight failed", "detail": result["problems"]}
            )
        return result
