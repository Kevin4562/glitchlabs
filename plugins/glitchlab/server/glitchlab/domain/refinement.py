"""Refinement graph: coarse→fine→precise (spec §6.3). Auto-suggests a narrowed bounding box.

After a coarse sweep, DBSCAN over success points yields the densest cluster; the suggested child
bbox is tightened around it. Child sweeps start at `needs-reverification` until a confirmation batch
validates the region; disagreements between a coarse cell's rate and its confirmation are surfaced.
"""
from __future__ import annotations

from typing import Any

from ..render.descriptor import _clusters
from ..render.grid import build_grid


def suggest_refine_box(store, sweep_id: str, x_axis="width", y_axis="offset",
                       tighten: float = 0.15) -> dict:
    g = build_grid(store, sweep_id, x_axis, y_axis)
    clusters = _clusters(g, {"success"})
    if not clusters:
        return {"sweep_id": sweep_id, "suggested": None,
                "reason": "no success cluster yet — widen coarse sweep or add trials"}
    top = clusters[0]
    b = top["bbox"]
    def pad(lo, hi):
        span = hi - lo
        return [lo - span * tighten, hi + span * tighten] if span else [lo, hi]
    box = {x_axis: pad(*b[x_axis]), y_axis: pad(*b[y_axis])}
    return {"sweep_id": sweep_id, "suggested_bbox": box, "centroid": top["centroid"],
            "peak_rate": top["peak_rate"], "cluster_n": top["n"],
            "confidence": top["confidence"],
            "recommended_repeats": 30 if top["confidence"] != "confirmed" else 10,
            "reason": "densest success cluster; child sweep starts needs-reverification"}


def sweep_tree(store, session_id: str) -> list[dict]:
    rows = store.fetch_all(
        "SELECT id,parent_sweep_id,name,kind,confidence,status FROM sweep WHERE session_id=? "
        "ORDER BY created_at", (session_id,))
    return rows
