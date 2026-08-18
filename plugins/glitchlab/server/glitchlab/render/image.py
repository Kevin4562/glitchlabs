"""Matplotlib PNG/SVG renderers for the `image` tier + viewer figures (spec §10.1, §17, §20).

Styled to the §20 visual system: near-black bg (#0E1116), elevated panels (#161A22), colorblind-safe
outcome colors from the taxonomy, monospace values. Render-only positional jitter appears ONLY here.
"""
from __future__ import annotations

import io
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
from matplotlib.colors import LinearSegmentedColormap, ListedColormap, BoundaryNorm

from .. import config
from ..storage import taxonomy
from .grid import GridData, build_grid, si

BG = "#0E1116"
PANEL = "#161A22"
FG = "#C9D1D9"
GRID = "#232A34"
ACCENT = "#22D3EE"

_CLASS = {c.key: c for c in taxonomy.DEFAULT_TAXONOMY}


def _style(ax):
    ax.set_facecolor(PANEL)
    for s in ax.spines.values():
        s.set_color(GRID)
    ax.tick_params(colors=FG, labelsize=8)
    ax.xaxis.label.set_color(FG)
    ax.yaxis.label.set_color(FG)
    ax.title.set_color(FG)


def _fig(w=6.4, h=4.2):
    fig = plt.figure(figsize=(w, h), dpi=120, facecolor=BG)
    ax = fig.add_subplot(111)
    ax.set_facecolor(PANEL)
    _style(ax)
    return fig, ax


def _save(fig, out: Path | None, fmt="png") -> bytes | str:
    buf = io.BytesIO()
    fig.tight_layout()
    fig.savefig(buf, format=fmt, facecolor=BG, bbox_inches="tight")
    plt.close(fig)
    data = buf.getvalue()
    if out is not None:
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(data)
        return str(out)
    return data


def parameter_map_png(store, sweep_id: str, view: str = "success_rate", x_axis="width",
                      y_axis="offset", out: Path | None = None, fmt="png"):
    g = build_grid(store, sweep_id, x_axis, y_axis)
    fig, ax = _fig()
    if view == "categorical":
        keys = [c.key for c in taxonomy.DEFAULT_TAXONOMY]
        cmap = ListedColormap([_CLASS[k].color for k in keys])
        idx = np.full(g.trials.shape, keys.index("no-data"), dtype=int)
        for i in range(len(g.ys)):
            for j in range(len(g.xs)):
                idx[i, j] = keys.index(g.dominant[i][j]) if g.dominant[i][j] in keys else 0
        norm = BoundaryNorm(np.arange(-0.5, len(keys) + 0.5), cmap.N)
        ax.imshow(idx, aspect="auto", origin="lower", cmap=cmap, norm=norm,
                  extent=_extent(g))
        handles = [plt.Line2D([0], [0], marker="s", color=PANEL, markerfacecolor=_CLASS[k].color,
                              markersize=8, linestyle="", label=_CLASS[k].label)
                   for k in keys if k in g.totals or k in ("no-data", "success")]
        ax.legend(handles=handles, loc="upper left", fontsize=6, facecolor=PANEL,
                  edgecolor=GRID, labelcolor=FG, ncol=2)
    else:
        rate = np.ma.masked_invalid(g.rate)
        cmap = LinearSegmentedColormap.from_list("succ", ["#0E1116", "#164E3B", "#22C55E", "#86EFAC"])
        cmap.set_bad(BG)
        im = ax.imshow(rate, aspect="auto", origin="lower", cmap=cmap, vmin=0,
                       vmax=max(0.001, float(np.nanmax(g.rate)) if g.rate.size else 0.2),
                       extent=_extent(g))
        cb = fig.colorbar(im, ax=ax, fraction=0.046, pad=0.02)
        cb.ax.tick_params(colors=FG, labelsize=7)
        cb.set_label("success rate", color=FG, fontsize=8)
        # bounding box for top cluster
        from .descriptor import _clusters
        cl = _clusters(g, {"success"})
        if cl:
            b = cl[0]["bbox"]
            bx, by = b[x_axis], b[y_axis]
            ax.add_patch(plt.Rectangle((bx[0], by[0]), max(bx[1]-bx[0], _dx(g)),
                                       max(by[1]-by[0], _dy(g)), fill=False, edgecolor="#22D3EE",
                                       lw=1.5, linestyle="--"))
            ax.annotate("suggested refine", (bx[0], by[1]), color="#22D3EE", fontsize=7)
    ax.set_xlabel(f"{x_axis} ({g.x_unit})")
    ax.set_ylabel(f"{y_axis} ({g.y_unit})")
    ax.set_title(f"Parameter-space map · {view}", fontsize=10)
    return _save(fig, out, fmt)


def _extent(g: GridData):
    if not g.xs or not g.ys:
        return [0, 1, 0, 1]
    return [min(g.xs), max(g.xs), min(g.ys), max(g.ys)]


def _dx(g):
    return (max(g.xs) - min(g.xs)) / max(len(g.xs), 1) if len(g.xs) > 1 else 1


def _dy(g):
    return (max(g.ys) - min(g.ys)) / max(len(g.ys), 1) if len(g.ys) > 1 else 1


def waveform_png(samples: np.ndarray, dt_s: float, t0_s: float = 0.0, out: Path | None = None,
                 title="Scope trace"):
    fig, ax = _fig(7, 3)
    t = t0_s + np.arange(samples.size) * dt_s
    ax.plot(t * 1e6, samples, color=ACCENT, lw=0.8)
    ax.set_xlabel("time (µs)")
    ax.set_ylabel("volts")
    ax.set_title(title, fontsize=10)
    ax.grid(True, color=GRID, lw=0.4)
    return _save(fig, out)


def timeseries_png(values: list[float], out: Path | None = None, title="Rolling hit-rate",
                   ylabel="rate"):
    fig, ax = _fig(6, 2.6)
    ax.plot(values, color=ACCENT, lw=1.2)
    ax.fill_between(range(len(values)), values, color=ACCENT, alpha=0.12)
    ax.set_ylabel(ylabel)
    ax.set_xlabel("attempt #")
    ax.set_title(title, fontsize=10)
    ax.grid(True, color=GRID, lw=0.4)
    return _save(fig, out)
