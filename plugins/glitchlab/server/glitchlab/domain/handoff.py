"""Fail-closed boundary for target-specific post-success handoff."""
from __future__ import annotations

from typing import Any, Optional


class HandoffRunner:
    """Retained only so old callers receive a structured refusal."""

    def __init__(self, core) -> None:
        self.core = core

    def run(self, spec: Optional[dict] = None, out_dir: Optional[str] = None,
            dry_run: bool = False) -> dict[str, Any]:
        result = {
            "ok": False,
            "refused": True,
            "violated_rule": "safe_project_handoff_not_configured",
            "detail": (
                "no generic handoff is safe for every target; preserve the target and "
                "implement a separately reviewed connector-owned handoff"
            ),
            "dry_run": bool(dry_run),
        }
        self.core.auditor.record(
            "run_handoff", "CAUTION",
            {"out_dir": out_dir, "requested_spec": spec},
            "refused", violated_rule=result["violated_rule"],
        )
        return result
