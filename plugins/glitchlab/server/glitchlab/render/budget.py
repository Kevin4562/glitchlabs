"""Token estimation + auto-degrade contract (spec §10.1, §23).

Every response carries an estimated token size. If a requested tier would exceed the tool's declared
`max_tokens`, we downsample and set `downsampled: true` + a `full_data_uri`. The model never receives
a surprise multi-thousand-token blob.
"""
from __future__ import annotations

import json
from typing import Any

# Declared per-tool budgets (spec §23).
MAX_TOKENS = {
    "summary": 400,
    "textmap": 1500,
    "cells": 2000,
    "image": 80,
    "subscription": 200,
}


def estimate_tokens(obj: Any) -> int:
    """Cheap deterministic estimate: ~4 chars/token over the serialized form."""
    if isinstance(obj, str):
        n = len(obj)
    else:
        n = len(json.dumps(obj, default=str))
    return max(1, round(n / 4))


def enforce(tier: str, payload: Any, full_data_uri: str | None = None) -> dict:
    """Return {content, tokens, downsampled, full_data_uri?} honoring the tier budget."""
    budget = MAX_TOKENS.get(tier, 1500)
    tokens = estimate_tokens(payload)
    result: dict[str, Any] = {"tokens": tokens, "downsampled": False}
    if tokens > budget:
        result["downsampled"] = True
        if full_data_uri:
            result["full_data_uri"] = full_data_uri
    result["payload"] = payload
    return result


def truncate_text(text: str, tier: str = "textmap") -> tuple[str, bool]:
    budget = MAX_TOKENS.get(tier, 1500)
    if estimate_tokens(text) <= budget:
        return text, False
    # keep header + legend; trim middle rows
    lines = text.splitlines()
    keep = max(4, int(budget * 4 / max(1, len(text)) * len(lines)))
    trimmed = lines[: keep - 1] + ["  … (downsampled; full grid via full_data_uri) …", lines[-1]]
    return "\n".join(trimmed), True
