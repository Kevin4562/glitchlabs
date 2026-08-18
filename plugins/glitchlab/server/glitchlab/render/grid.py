"""Build a 2D parameter-space grid from the attempt store — shared by descriptor/textart/image.

Honors the three-state model (§4.4): cells with no attempts are `no-data`, distinct from 0% success.
Low-trial cells are tracked so every tier can visually distinguish sparse noise from signal (§10.3).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import numpy as np


@dataclass
class GridData:
    x_name: str
    y_name: str
    x_unit: str
    y_unit: str
    xs: list[float]                 # sorted unique x cell centers
    ys: list[float]                 # sorted unique y cell centers
    trials: np.ndarray              # [len(ys), len(xs)] total trials per cell
    success: np.ndarray             # [len(ys), len(xs)] success count per cell
    dominant: list[list[str]]       # dominant class key per cell ('no-data' if empty)
    class_counts: dict[str, np.ndarray] = field(default_factory=dict)  # key -> [ys,xs]
    totals: dict[str, int] = field(default_factory=dict)
    low_conf_threshold: int = 5

    @property
    def rate(self) -> np.ndarray:
        with np.errstate(divide="ignore", invalid="ignore"):
            r = np.where(self.trials > 0, self.success / np.maximum(self.trials, 1), np.nan)
        return r

    def low_conf_mask(self) -> np.ndarray:
        return (self.trials > 0) & (self.trials < self.low_conf_threshold)


def build_grid(store, sweep_id: str | None = None, x_axis: str = "width", y_axis: str = "offset",
               success_keys: set[str] | None = None, campaign_id: str | None = None) -> GridData:
    """Build the 2D grid for one sweep, or aggregated across every sweep of a campaign."""
    success_keys = success_keys or {"success"}
    if campaign_id:
        where = ("sweep_id IN (SELECT sw.id FROM sweep sw JOIN session se ON sw.session_id=se.id "
                 "WHERE se.campaign_id=?)")
        args: tuple = (campaign_id,)
    else:
        where, args = "sweep_id=?", (sweep_id,)
    rows = store.fetch_all(
        f"SELECT {x_axis} AS xv, {y_axis} AS yv, outcome_class, COUNT(*) n "
        f"FROM attempt WHERE {where} AND {x_axis} IS NOT NULL AND {y_axis} IS NOT NULL "
        f"GROUP BY xv, yv, outcome_class", args)
    xs = sorted({r["xv"] for r in rows})
    ys = sorted({r["yv"] for r in rows})
    xi = {v: i for i, v in enumerate(xs)}
    yi = {v: i for i, v in enumerate(ys)}
    nY, nX = max(len(ys), 1), max(len(xs), 1)
    trials = np.zeros((nY, nX))
    success = np.zeros((nY, nX))
    class_counts: dict[str, np.ndarray] = {}
    totals: dict[str, int] = {}
    for r in rows:
        i, j = yi[r["yv"]], xi[r["xv"]]
        oc, n = r["outcome_class"], r["n"]
        trials[i, j] += n
        totals[oc] = totals.get(oc, 0) + n
        class_counts.setdefault(oc, np.zeros((nY, nX)))[i, j] += n
        if oc in success_keys:
            success[i, j] += n
    dominant: list[list[str]] = []
    for i in range(nY):
        row = []
        for j in range(nX):
            if trials[i, j] == 0:
                row.append("no-data")
            else:
                best, bestn = "no-effect", -1
                for oc, arr in class_counts.items():
                    if arr[i, j] > bestn:
                        best, bestn = oc, arr[i, j]
                row.append(best)
        dominant.append(row)
    x_unit = "cyc" if x_axis == "width" else ("V" if x_axis == "voltage" else "")
    # `offset` here carries the glitch ext_offset (glitch-clock cycles) for this rig.
    y_unit = "cyc" if y_axis in ("offset", "ext_offset") else ""
    return GridData(x_axis, y_axis, x_unit, y_unit, xs, ys, trials, success, dominant,
                    class_counts, totals)


def si(v: float) -> str:
    """Compact SI/scientific formatting for axis labels (§10.3 rendering rules)."""
    if v is None:
        return "?"
    av = abs(v)
    if av >= 1e9:
        return f"{v/1e9:.2f}G"
    if av >= 1e6:
        return f"{v/1e6:.2f}M"
    if av >= 1e3:
        return f"{v/1e3:.1f}k"
    if av == int(av):
        return str(int(v))
    return f"{v:.3g}"
