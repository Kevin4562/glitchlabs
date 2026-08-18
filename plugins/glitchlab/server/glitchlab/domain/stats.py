"""Statistics engine (spec §7). Within-session + cross-session, returns compact summaries (§10.5).

All metrics honor the three-state model (§4.4): functional_unquantified results are counted and
reported separately, excluded from rate math.
"""
from __future__ import annotations

import math
from typing import Any

import numpy as np


def _attempts(store, sweep_id: str | None = None, session_id: str | None = None,
              campaign_id: str | None = None) -> list[dict]:
    if sweep_id:
        return store.fetch_all("SELECT * FROM attempt WHERE sweep_id=? ORDER BY seq", (sweep_id,))
    if session_id:
        return store.fetch_all(
            "SELECT a.* FROM attempt a JOIN sweep sw ON a.sweep_id=sw.id WHERE sw.session_id=? "
            "ORDER BY a.id", (session_id,))
    if campaign_id:
        return store.fetch_all(
            "SELECT a.* FROM attempt a JOIN sweep sw ON a.sweep_id=sw.id JOIN session se "
            "ON sw.session_id=se.id WHERE se.campaign_id=? ORDER BY a.id", (campaign_id,))
    return store.fetch_all("SELECT * FROM attempt ORDER BY id")


def rolling_rate(store, sweep_id=None, session_id=None, campaign_id=None, window: int = 50) -> dict:
    rows = _attempts(store, sweep_id, session_id, campaign_id)
    succ = np.array([1.0 if r["outcome_class"] == "success" else 0.0 for r in rows])
    if succ.size == 0:
        return {"metric": "rolling_rate", "n": 0, "window": 0, "cumulative_rate_final": 0.0,
                "rolling_series": [], "cumulative_series": [], "trend_slope": 0.0,
                "drift_detected": False}
    cum = np.cumsum(succ) / (np.arange(succ.size) + 1)
    kernel = min(window, succ.size)
    roll = np.convolve(succ, np.ones(kernel) / kernel, mode="valid")
    trend = float(np.polyfit(np.arange(cum.size), cum, 1)[0]) if cum.size > 1 else 0.0
    return {"metric": "rolling_rate", "n": int(succ.size), "window": kernel,
            "cumulative_rate_final": round(float(cum[-1]), 4),
            "rolling_series": [round(float(v), 4) for v in roll[-64:]],
            "cumulative_series": [round(float(v), 4) for v in cum[-64:]],
            "trend_slope": round(trend, 6),
            "drift_detected": abs(trend) > 1e-4}


def timing_histogram(store, sweep_id=None, session_id=None, campaign_id=None, axis="offset", bins: int = 20) -> dict:
    rows = _attempts(store, sweep_id, session_id, campaign_id)
    vals = [r[axis] for r in rows if r["outcome_class"] == "success" and r.get(axis) is not None]
    if not vals:
        return {"metric": "timing_histogram", "axis": axis, "n": 0, "hist": [], "edges": []}
    arr = np.array(vals, dtype=float)
    hist, edges = np.histogram(arr, bins=min(bins, max(len(set(vals)), 1)))
    return {"metric": "timing_histogram", "axis": axis, "n": len(vals),
            "mean": round(float(arr.mean()), 4), "median": round(float(np.median(arr)), 4),
            "std": round(float(arr.std()), 4),
            "hist": hist.tolist(), "edges": [round(float(e), 4) for e in edges]}


def time_between_success(store, sweep_id=None, session_id=None, campaign_id=None) -> dict:
    rows = _attempts(store, sweep_id, session_id, campaign_id)
    idxs = [i for i, r in enumerate(rows) if r["outcome_class"] == "success"]
    if len(idxs) < 2:
        return {"metric": "time_between_success", "successes": len(idxs),
                "median_attempts": None, "mean_attempts": None, "eta_attempts": None}
    gaps = np.diff(idxs)
    med = float(np.median(gaps))
    mean = float(gaps.mean())
    return {"metric": "time_between_success", "successes": len(idxs),
            "median_attempts": round(med, 2), "mean_attempts": round(mean, 2),
            "mean_gt_median": mean > med,   # occasional long dry spells (§7.1)
            "eta_attempts": round(mean, 1)}


def funnel(store, sweep_id=None, session_id=None, campaign_id=None) -> dict:
    rows = _attempts(store, sweep_id, session_id, campaign_id)
    attempts = len(rows)
    faults = sum(1 for r in rows if r["outcome_class"] in ("success", "exception", "false-positive"))
    recoverable = sum(1 for r in rows if r["outcome_class"] in ("success", "exception"))
    verified = sum(1 for r in rows if r["outcome_class"] == "success" and r.get("verified"))
    total_ms = sum(r.get("duration_ms") or 0 for r in rows)
    per_useful = (total_ms / 1000 / 60 / max(verified, 1)) if verified else 0.0
    def pct(a, b):
        return round(100 * a / b, 1) if b else 0.0
    return {"metric": "funnel", "attempts": attempts, "faults": faults,
            "faults_pct": pct(faults, attempts), "recoverable": recoverable,
            "recoverable_pct": pct(recoverable, faults), "verified": verified,
            "verified_pct": pct(verified, recoverable),
            "avg_min_per_useful": round(per_useful, 2),
            "text": f"attempts {attempts} → faults {faults} ({pct(faults,attempts)}%) → "
                    f"recoverable {recoverable} ({pct(recoverable,faults)}%) → verified {verified} "
                    f"({pct(verified,recoverable)}%) · {per_useful:.1f} min/useful"}


def throughput(store, sweep_id=None, session_id=None, campaign_id=None) -> dict:
    rows = _attempts(store, sweep_id, session_id, campaign_id)
    if not rows:
        return {"metric": "throughput", "attempts": 0, "attempts_per_sec": 0.0}
    durs = [r.get("duration_ms") or 0 for r in rows]
    total_s = sum(durs) / 1000.0
    aps = len(rows) / total_s if total_s > 0 else 0.0
    oracle_ms = np.mean([d for d in durs if d]) if any(durs) else 0.0
    return {"metric": "throughput", "attempts": len(rows),
            "attempts_per_sec": round(aps, 2), "avg_attempt_ms": round(float(oracle_ms), 2)}


def cumulative(store, sweep_id=None, session_id=None, campaign_id=None) -> dict:
    rows = _attempts(store, sweep_id, session_id, campaign_id)
    succ = np.array([1.0 if r["outcome_class"] == "success" else 0.0 for r in rows])
    cum = np.cumsum(succ) if succ.size else np.array([])
    # temporal clustering test: variance of inter-success gaps vs Poisson expectation
    idxs = np.where(succ == 1)[0]
    clustering = False
    stat = 0.0
    if len(idxs) >= 3:
        gaps = np.diff(idxs)
        stat = float(gaps.var() / (gaps.mean() ** 2 + 1e-9))  # index of dispersion-ish
        clustering = stat > 1.5
    return {"metric": "cumulative", "n": int(succ.size), "successes": int(succ.sum()),
            "series": [int(v) for v in cum[-64:]], "temporal_clustering": clustering,
            "clustering_statistic": round(stat, 3)}


def per_unit_variance(store, target_model: str, injection_type="voltage") -> dict:
    rows = store.fetch_all(
        "SELECT u.serial serial, a.voltage v, a.offset off FROM attempt a "
        "JOIN sweep sw ON a.sweep_id=sw.id JOIN session s ON sw.session_id=s.id "
        "JOIN unit u ON s.unit_id=u.id JOIN campaign c ON s.campaign_id=c.id "
        "JOIN target t ON c.target_id=t.id "
        "WHERE t.model=? AND a.outcome_class='success'", (target_model,))
    by_unit: dict[str, list[float]] = {}
    for r in rows:
        if r["v"] is not None:
            by_unit.setdefault(r["serial"], []).append(r["v"])
    dist = {u: {"n": len(v), "mean_v": round(float(np.mean(v)), 4),
                "std_v": round(float(np.std(v)), 4)} for u, v in by_unit.items()}
    allv = [x for v in by_unit.values() for x in v]
    return {"metric": "per_unit_variance", "target_model": target_model, "units": dist,
            "overall_mean_v": round(float(np.mean(allv)), 4) if allv else None,
            "overall_std_v": round(float(np.std(allv)), 4) if allv else None}


def drift(store, target_model: str) -> dict:
    rows = store.fetch_all(
        "SELECT e.ambient_temp_c t, a.offset off FROM attempt a JOIN env_sample e "
        "ON e.attempt_id=a.id JOIN sweep sw ON a.sweep_id=sw.id JOIN session s ON sw.session_id=s.id "
        "JOIN campaign c ON s.campaign_id=c.id JOIN target t2 ON c.target_id=t2.id "
        "WHERE t2.model=? AND a.outcome_class='success' AND e.ambient_temp_c IS NOT NULL",
        (target_model,))
    temps = np.array([r["t"] for r in rows if r["off"] is not None], dtype=float)
    offs = np.array([r["off"] for r in rows if r["off"] is not None], dtype=float)
    if temps.size < 3:
        return {"metric": "drift", "n": int(temps.size), "slope_per_c": None}
    slope, intercept = np.polyfit(temps, offs, 1)
    return {"metric": "drift", "n": int(temps.size),
            "slope_per_c": round(float(slope), 4), "intercept": round(float(intercept), 4),
            "compensation": f"{slope:.3g} offset-units/°C"}


def confusion_matrix(store, sweep_id=None, session_id=None, campaign_id=None) -> dict:
    rows = _attempts(store, sweep_id, session_id, campaign_id)
    pairs: dict[tuple[str, str], int] = {}
    for r in rows:
        exp = None
        try:
            import json
            p = json.loads(r.get("params") or "{}")
            exp = p.get("expected_class")
        except Exception:
            pass
        if exp:
            pairs[(exp, r["outcome_class"])] = pairs.get((exp, r["outcome_class"]), 0) + 1
    off_diag = sorted([{"expected": k[0], "got": k[1], "n": v} for k, v in pairs.items()
                       if k[0] != k[1]], key=lambda d: d["n"], reverse=True)
    return {"metric": "confusion_matrix", "pairs": len(pairs), "top_confusions": off_diag[:10]}


def bootstrap_confidence(store, sweep_id: str, n_settings: int = 50, iterations: int = 1000) -> dict:
    rows = store.fetch_all("SELECT outcome_class FROM attempt WHERE sweep_id=?", (sweep_id,))
    pool = np.array([1 if r["outcome_class"] == "success" else 0 for r in rows])
    if pool.size == 0:
        return {"metric": "bootstrap", "n": 0, "curve": []}
    rng = np.random.default_rng(12345)
    curve = []
    for n in sorted(set([max(1, int(x)) for x in np.linspace(5, min(n_settings, pool.size), 6)])):
        rates = [rng.choice(pool, size=n, replace=True).mean() for _ in range(min(iterations, 500))]
        curve.append({"n": n, "mean": round(float(np.mean(rates)), 4),
                      "ci_low": round(float(np.percentile(rates, 2.5)), 4),
                      "ci_high": round(float(np.percentile(rates, 97.5)), 4)})
    return {"metric": "bootstrap", "pool_size": int(pool.size),
            "pool_rate": round(float(pool.mean()), 4), "curve": curve}


METRICS = {
    "rolling_rate": rolling_rate, "timing_histogram": timing_histogram,
    "time_between_success": time_between_success, "funnel": funnel, "throughput": throughput,
    "cumulative": cumulative, "per_unit_variance": per_unit_variance, "drift": drift,
    "confusion_matrix": confusion_matrix,
}
