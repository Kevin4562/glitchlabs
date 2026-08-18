"""Deterministic Unicode text-art encoders (spec §10.3).

Five encodings, axis-labeled, legend-carrying, stable across calls for cache friendliness.
Positional jitter is NEVER applied here (jitter is a pixel-render concern only, §10.3).
Low-trial cells are always visually distinguished (`~`).
"""
from __future__ import annotations

import numpy as np

from ..storage import taxonomy
from .grid import GridData, si

# glyph legends (stable)
CLASS_GLYPH = {c.key: c.glyph for c in taxonomy.DEFAULT_TAXONOMY}
RAMP = [" ", "·", "░", "▒", "▓", "█"]           # no-data, <2, 2-5, 5-10, 10-20, >20 %
SPARK = "▁▂▃▄▅▆▇█"


def _row_label(v: float) -> str:
    return f"{si(v):>7}"


def categorical_map(g: GridData, max_cols: int = 24, max_rows: int = 24) -> str:
    """(a) dominant-class glyph per cell."""
    xs, ys = _downsample_axes(g, max_cols, max_rows)
    lines = [f"{'':>8}{g.x_name} ({g.x_unit}) →"]
    header = " " * 8 + " ".join(f"{si(x):>3}" for x in xs)
    lines.append(header)
    for y in ys:
        cells = []
        for x in xs:
            i, j = g.ys.index(y), g.xs.index(x)
            key = g.dominant[i][j]
            gl = CLASS_GLYPH.get(key, "?")
            if key != "no-data" and g.low_conf_mask()[i, j]:
                gl = "~"
            cells.append(f"{gl:>3}")
        lines.append(f"{_row_label(y)} " + " ".join(cells))
    lines.append(f"{g.y_name} ({g.y_unit}) ↑")
    legend = ("legend:  ' '=no-data  .=no-effect  ·=reset  o=exception  O=false-pos  ★=success"
              "  ~=low-trial")
    lines.append(legend)
    return "\n".join(lines)


def success_rate_map(g: GridData, max_cols: int = 24, max_rows: int = 24) -> str:
    """(b) success-rate ramp glyph per cell with explicit no-data + low-confidence markers."""
    xs, ys = _downsample_axes(g, max_cols, max_rows)
    rate = g.rate
    low = g.low_conf_mask()
    lines = [f"{'':>8}{g.x_name} ({g.x_unit}) →"]
    lines.append(" " * 8 + " ".join(f"{si(x):>2}" for x in xs))
    for y in ys:
        cells = []
        for x in xs:
            i, j = g.ys.index(y), g.xs.index(x)
            if g.trials[i, j] == 0:
                cells.append(" ")
            elif low[i, j]:
                cells.append("~")
            else:
                r = rate[i, j]
                idx = 1 if r < 0.02 else 2 if r < 0.05 else 3 if r < 0.10 else 4 if r < 0.20 else 5
                cells.append(RAMP[idx])
        lines.append(f"{_row_label(y)}  " + "  ".join(cells))
    lines.append("ramp: ' '=no-data  ·=<2%  ░=2-5%  ▒=5-10%  ▓=10-20%  █=>20%   (~=low-trial)")
    return "\n".join(lines)


def braille_map(g: GridData) -> str:
    """(c) braille high-density map — 2x4 subgrid per char (success presence)."""
    rate = np.nan_to_num(g.rate, nan=0.0)
    H, W = rate.shape
    if H == 0 or W == 0:
        return "(empty)"
    thresh = max(rate.max() * 0.4, 0.01)
    dots = [(0, 0x01), (1, 0x02), (2, 0x04), (0, 0x08), (1, 0x10), (2, 0x20), (3, 0x40), (3, 0x80)]
    out_rows = []
    for by in range(0, H, 4):
        line = []
        for bx in range(0, W, 2):
            code = 0
            for dy in range(4):
                for dx in range(2):
                    yy, xx = by + dy, bx + dx
                    if yy < H and xx < W and rate[yy, xx] >= thresh:
                        # braille bit layout
                        bit = {(0, 0): 0x01, (1, 0): 0x02, (2, 0): 0x04, (3, 0): 0x40,
                               (0, 1): 0x08, (1, 1): 0x10, (2, 1): 0x20, (3, 1): 0x80}[(dy, dx)]
                        code |= bit
            line.append(chr(0x2800 + code))
        out_rows.append("".join(line))
    return "\n".join(out_rows) + f"\n(braille: dot set where success-rate ≥ {thresh:.0%})"


def marginals_sparkline(g: GridData) -> str:
    """(d) 1D marginal sparklines along each axis (success rate)."""
    def spark(vals: np.ndarray) -> str:
        if vals.size == 0 or np.nanmax(vals) == 0:
            return SPARK[0] * max(vals.size, 1)
        norm = vals / (np.nanmax(vals) or 1)
        return "".join(SPARK[min(int(v * (len(SPARK) - 1) + 0.5), len(SPARK) - 1)] for v in norm)

    with np.errstate(invalid="ignore"):
        xm = np.where(g.trials.sum(0) > 0, g.success.sum(0) / np.maximum(g.trials.sum(0), 1), 0)
        ym = np.where(g.trials.sum(1) > 0, g.success.sum(1) / np.maximum(g.trials.sum(1), 1), 0)
    return (f"{g.x_name:<7} marginal (success rate):  {spark(xm)}\n"
            f"{g.y_name:<7} marginal (success rate):  {spark(ym)}")


def sparse_cells(g: GridData, rate_min: float = 0.05, trials_min: int = 20) -> str:
    """(e) sparse notable-cell list for very large/sparse maps."""
    rate = g.rate
    lines = [f"success cells (rate ≥ {rate_min:.0%}, trials ≥ {trials_min}):"]
    found = False
    for i, y in enumerate(g.ys):
        for j, x in enumerate(g.xs):
            if g.trials[i, j] >= trials_min and rate[i, j] >= rate_min:
                found = True
                conf = " ✓confirmed" if g.trials[i, j] >= 40 else ""
                lines.append(f"  ({g.x_name}={si(x)}, {g.y_name}={si(y)})  "
                             f"rate={rate[i,j]:.0%}  n={int(g.trials[i,j])}{conf}")
    if not found:
        lines.append("  (none above threshold)")
    return "\n".join(lines)


def trace_sparkline(samples: np.ndarray, width: int = 64) -> str:
    """Unicode sparkline of a scope trace, downsampled (spec §17 textmap tier)."""
    if samples.size == 0:
        return "(no samples)"
    if samples.size > width:
        idx = np.linspace(0, samples.size - 1, width).astype(int)
        samples = samples[idx]
    lo, hi = float(samples.min()), float(samples.max())
    if hi - lo < 1e-12:
        return SPARK[0] * len(samples)
    norm = (samples - lo) / (hi - lo)
    return "".join(SPARK[min(int(v * (len(SPARK) - 1) + 0.5), len(SPARK) - 1)] for v in norm)


def series_sparkline(vals: list[float], width: int = 48) -> str:
    arr = np.asarray(vals, dtype=float)
    return trace_sparkline(arr, width)


def _downsample_axes(g: GridData, max_cols: int, max_rows: int):
    xs = g.xs
    ys = g.ys
    if len(xs) > max_cols:
        idx = np.linspace(0, len(xs) - 1, max_cols).astype(int)
        xs = [xs[k] for k in sorted(set(idx))]
    if len(ys) > max_rows:
        idx = np.linspace(0, len(ys) - 1, max_rows).astype(int)
        ys = [ys[k] for k in sorted(set(idx))]
    return xs, ys
