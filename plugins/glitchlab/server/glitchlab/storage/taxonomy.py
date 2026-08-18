"""Outcome taxonomy — the foundational multi-class data model (spec §2.1, §4.2 outcome_class).

Outcomes are categorical, configurable, and open-ended. Binary pass/fail is forbidden. The default
taxonomy is `no-effect / reset / exception / success / false-positive`; campaigns add classes.

Colors/markers are colorblind-safe and paired with a glyph so no view relies on color alone (§19.3).
Glyphs match the text-art legend in §10.3.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class OutcomeClass:
    key: str
    label: str
    color: str          # hex, colorblind-safe
    marker: str         # matplotlib marker
    glyph: str          # text-art glyph (§10.3)
    is_success: bool = False
    is_collateral: bool = False
    sort_order: int = 0


# Default global taxonomy (spec §2 principle 1). Glyphs align with §10.3(a) legend.
DEFAULT_TAXONOMY: list[OutcomeClass] = [
    OutcomeClass("no-data", "No data", "#2A2F3A", "s", " ", sort_order=-1),
    OutcomeClass("no-effect", "No effect", "#5B6472", ".", ".", sort_order=0),
    OutcomeClass("reset", "Reset", "#3B82F6", "v", "·", sort_order=1),
    OutcomeClass("exception", "Exception", "#F59E0B", "^", "o", sort_order=2),
    OutcomeClass("false-positive", "False positive", "#A855F7", "x", "O", sort_order=3),
    OutcomeClass("success", "Success", "#22C55E", "*", "★", is_success=True, sort_order=4),
    # collateral examples (created on demand per campaign)
    OutcomeClass("flash-erased", "Flash erased", "#EF4444", "P", "E", is_collateral=True, sort_order=5),
]

# Measurement three-state model (spec §4.4) — never conflated.
MEASUREMENT_STATES = ("not_attempted", "functional_unquantified", "quantified")

FAULT_MODELS = ("instruction-skip-1", "instruction-skip-2", "replay", "bit-flip", "stuck-at")

RAW_CHANNELS = ("uart", "gpio", "led", "protocol-sidechannel", "trace", "stdout")


def default_taxonomy_dicts() -> list[dict]:
    return [c.__dict__ for c in DEFAULT_TAXONOMY]


def by_key() -> dict[str, OutcomeClass]:
    return {c.key: c for c in DEFAULT_TAXONOMY}


def success_keys() -> set[str]:
    return {c.key for c in DEFAULT_TAXONOMY if c.is_success}
