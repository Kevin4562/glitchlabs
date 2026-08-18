"""Prediction engine (spec §8): warm-start + success-probability model + adaptive guidance.

Warm-starts from the known-good database; trains a GradientBoosting classifier on historical attempts
to predict P(success | params). Cross-model transfer flags priors from similar targets as unverified.
"""
from __future__ import annotations

from typing import Any

import numpy as np


def warm_start(store, target_model: str, injection_type="voltage") -> dict:
    kg = store.get_known_good(target_model, injection_type)
    if kg:
        entry = kg[0]
        return {"source": "known_good", "target_model": target_model,
                "bbox": entry["known_good"], "provenance": entry["provenance"],
                "transferred": False,
                "note": "stored known-good ranges — start the coarse sweep here"}
    # cross-model transfer: any model's known-good, flagged unverified
    any_kg = store.fetch_all("SELECT DISTINCT target_model FROM parameter_profile")
    if any_kg:
        alt = any_kg[0]["target_model"]
        kg2 = store.get_known_good(alt, injection_type)
        if kg2:
            return {"source": "cross_model_transfer", "from_model": alt,
                    "bbox": kg2[0]["known_good"], "transferred": True,
                    "note": f"TRANSFERRED PRIORS from {alt} — UNVERIFIED for {target_model}"}
    return {"source": "none", "transferred": False,
            "note": "no priors; start broad and coarse"}


def predict_parameters(store, target_model: str, injection_type="voltage",
                       context_sweep_id: str | None = None) -> dict:
    """Train a quick success-probability model on this target's history, return predicted hotspot."""
    rows = store.fetch_all(
        "SELECT a.width w, a.offset off, a.voltage v, "
        "CASE WHEN a.outcome_class='success' THEN 1 ELSE 0 END y "
        "FROM attempt a JOIN sweep sw ON a.sweep_id=sw.id JOIN session s ON sw.session_id=s.id "
        "JOIN campaign c ON s.campaign_id=c.id JOIN target t ON c.target_id=t.id "
        "WHERE t.model=? AND a.width IS NOT NULL AND a.offset IS NOT NULL", (target_model,))
    ws = warm_start(store, target_model, injection_type)
    if len(rows) < 20 or sum(r["y"] for r in rows) < 2:
        return {"target_model": target_model, "warm_start": ws,
                "predicted_hotspot": None, "model": "insufficient_data",
                "note": "not enough history to train; use warm_start bbox"}
    X = np.array([[r["w"], r["off"], (r["v"] or 0)] for r in rows], dtype=float)
    y = np.array([r["y"] for r in rows])
    try:
        from sklearn.ensemble import GradientBoostingClassifier
        model = GradientBoostingClassifier(n_estimators=60, max_depth=3)
        model.fit(X, y)
        # grid search predicted hotspot
        wr = np.linspace(X[:, 0].min(), X[:, 0].max(), 12)
        orr = np.linspace(X[:, 1].min(), X[:, 1].max(), 12)
        best, bp = None, -1.0
        for w in wr:
            for o in orr:
                p = model.predict_proba([[w, o, X[:, 2].mean()]])[0][1]
                if p > bp:
                    bp, best = p, (w, o)
        return {"target_model": target_model, "warm_start": ws,
                "predicted_hotspot": {"width": round(float(best[0]), 3),
                                      "offset": round(float(best[1]), 3),
                                      "p_success": round(float(bp), 4)},
                "model": "GradientBoosting", "train_n": int(len(rows))}
    except Exception as e:
        return {"target_model": target_model, "warm_start": ws, "predicted_hotspot": None,
                "model": f"error:{e}"}
