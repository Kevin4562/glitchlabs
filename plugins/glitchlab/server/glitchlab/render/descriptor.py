"""The `summary` descriptor — the workhorse (spec §10.2).

Returns structured semantics (axes, per-class totals, coverage/no-data fraction, success clusters
with bounding boxes, global hotspot, 1D marginals, flags, suggested refine bbox). This is what an
agent needs to *decide next parameters* in ~150-400 tokens.
"""
from __future__ import annotations

from typing import Any

import numpy as np

from .grid import GridData, build_grid


def _clusters(g: GridData, success_keys: set[str], min_trials: int = 1) -> list[dict]:
    """DBSCAN over success cells → clusters with centroid + bbox + peak rate + confidence."""
    pts, weights, rates = [], [], []
    rate = g.rate
    for i, y in enumerate(g.ys):
        for j, x in enumerate(g.xs):
            s = g.success[i, j]
            if s > 0 and g.trials[i, j] >= min_trials:
                pts.append([x, y]); weights.append(s); rates.append(rate[i, j])
    if not pts:
        return []
    pts_a = np.array(pts, dtype=float)
    try:
        from sklearn.cluster import DBSCAN
        # scale axes so DBSCAN eps is meaningful across very different units
        span = pts_a.max(0) - pts_a.min(0)
        span[span == 0] = 1.0
        scaled = (pts_a - pts_a.min(0)) / span
        labels = DBSCAN(eps=0.25, min_samples=1).fit_predict(scaled)
    except Exception:
        labels = np.zeros(len(pts), dtype=int)
    out = []
    for lab in sorted(set(labels)):
        idx = np.where(labels == lab)[0]
        cpts = pts_a[idx]
        w = np.array(weights)[idx]
        cx = float(np.average(cpts[:, 0], weights=w))
        cy = float(np.average(cpts[:, 1], weights=w))
        peak = float(np.array(rates)[idx].max())
        n = int(w.sum())
        conf = "confirmed" if n >= 40 else "needs-reverification" if n >= 5 else "provisional"
        out.append({
            "class": "success",
            "centroid": {g.x_name: round(cx, 6), g.y_name: round(cy, 6)},
            "bbox": {g.x_name: [float(cpts[:, 0].min()), float(cpts[:, 0].max())],
                     g.y_name: [float(cpts[:, 1].min()), float(cpts[:, 1].max())]},
            "peak_rate": round(peak, 4), "n": n, "confidence": conf})
    out.sort(key=lambda c: c["peak_rate"], reverse=True)
    return out


def _marginals(g: GridData) -> dict:
    with np.errstate(invalid="ignore"):
        xm = np.where(g.trials.sum(0) > 0, g.success.sum(0) / np.maximum(g.trials.sum(0), 1), 0)
        ym = np.where(g.trials.sum(1) > 0, g.success.sum(1) / np.maximum(g.trials.sum(1), 1), 0)
    return {g.x_name: [[float(x), round(float(v), 4)] for x, v in zip(g.xs, xm)],
            g.y_name: [[float(y), round(float(v), 4)] for y, v in zip(g.ys, ym)]}


def build_summary(store, sweep_id: str | None = None, view: str = "success_rate", x_axis: str = "width",
                  y_axis: str = "offset", success_keys: set[str] | None = None,
                  axis_flags: dict | None = None, render_uri: str | None = None,
                  campaign_id: str | None = None) -> dict:
    success_keys = success_keys or {"success"}
    g = build_grid(store, sweep_id, x_axis, y_axis, success_keys, campaign_id=campaign_id)
    ncells = len(g.xs) * len(g.ys)
    cells_with_data = int((g.trials > 0).sum())
    total_attempts = int(g.trials.sum())
    no_data_fraction = 0.0 if ncells == 0 else round(1 - cells_with_data / ncells, 3)
    low_conf = int(g.low_conf_mask().sum())
    min_trials = int(g.trials[g.trials > 0].min()) if cells_with_data else 0

    # functional_unquantified count (notes-mode successes; per-sweep only)
    functional_unquantified = 0
    if sweep_id:
        fu_count = store.fetch_one(
            "SELECT COUNT(*) n FROM attempt WHERE sweep_id=? AND width IS NULL AND "
            "outcome_class IN ('success')", (sweep_id,))
        functional_unquantified = (fu_count or {}).get("n", 0)

    clusters = _clusters(g, success_keys)
    rate = g.rate
    hotspot = None
    if cells_with_data and np.nanmax(rate) > 0:
        fi = np.unravel_index(np.nanargmax(np.nan_to_num(rate, nan=-1)), rate.shape)
        hotspot = {x_axis: float(g.xs[fi[1]]), y_axis: float(g.ys[fi[0]]),
                   "rate": round(float(rate[fi]), 4), "trials": int(g.trials[fi])}

    suggested = None
    if clusters:
        b = clusters[0]["bbox"]
        # narrow slightly toward centroid
        suggested = b

    flags = {"confounded_regions": 0,
             "equipment_quantized_axes": (axis_flags or {}).get("equipment_quantized", [])}
    if sweep_id:
        anns = store.fetch_all("SELECT flag FROM annotation WHERE sweep_id=?", (sweep_id,))
        flags["confounded_regions"] = sum(1 for a in anns if a["flag"] == "confounded")

    return {
        "view": view,
        "axes": {
            "x": {"name": x_axis, "min": (g.xs[0] if g.xs else None),
                  "max": (g.xs[-1] if g.xs else None), "unit": g.x_unit},
            "y": {"name": y_axis, "min": (g.ys[0] if g.ys else None),
                  "max": (g.ys[-1] if g.ys else None), "unit": g.y_unit},
        },
        "outcome_classes": list(g.totals.keys()),
        "totals": {"attempts": total_attempts, **{k: int(v) for k, v in g.totals.items()}},
        "coverage": {"cells": ncells, "cells_with_data": cells_with_data,
                     "no_data_fraction": no_data_fraction, "min_trials_per_cell": min_trials,
                     "low_confidence_cells": low_conf,
                     "functional_unquantified": int(functional_unquantified)},
        "clusters": clusters,
        "hotspot": hotspot,
        "marginals": _marginals(g),
        "flags": flags,
        "suggested_refine_bbox": suggested,
        "render_uri": render_uri or (f"glitchlab://sweep/{sweep_id}/map.png?view={view}"
                                     if sweep_id else f"glitchlab://campaign/{campaign_id}/map.png"),
        "downsampled": False,
    }
