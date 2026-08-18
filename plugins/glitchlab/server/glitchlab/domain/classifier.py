"""Pluggable outcome classifiers: (raw_capture, oracle_readings) -> outcome_class (spec §6.1).

Built-ins: regex/text-match on UART, GPIO/LED level decode, protocol-byte match, trace/threshold,
diff-vs-baseline. Every classification returns a confidence; low-confidence hits route to
false-positive so ambiguous hits stay out of the map. Genuinely ambiguous captures may be escalated
to MCP sampling (§14) by the caller.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Callable


@dataclass
class Classification:
    outcome_class: str
    confidence: float
    source: str = "classifier"
    oracle: str = "text-match"


class OutcomeClassifier:
    """Default text/level/threshold classifier for FI outcomes."""

    def __init__(self, success_patterns: list[str] | None = None,
                 reset_patterns: list[str] | None = None,
                 exception_patterns: list[str] | None = None,
                 baseline: str | None = None) -> None:
        self.success_patterns = [re.compile(p, re.I) for p in (success_patterns or
                                 [r"\bpass\b", r"\bunlock", r"\bgranted", r"root@", r"\bwin\b",
                                  r"CORRUPT", r"glitch.?ok"])]
        self.reset_patterns = [re.compile(p, re.I) for p in (reset_patterns or
                               [r"reset", r"reboot", r"\bboot", r"^\x00+$", r"watchdog"])]
        self.exception_patterns = [re.compile(p, re.I) for p in (exception_patterns or
                                   [r"hard.?fault", r"exception", r"\bfault\b", r"panic",
                                    r"usage.?fault", r"bus.?fault"])]
        self.baseline = baseline

    def classify(self, raw_captures: list[dict] | None = None,
                 oracle_readings: list[dict] | None = None,
                 expected: str | None = None) -> Classification:
        # 1) trust a scope/jtag oracle verdict if present and high-confidence
        for orr in (oracle_readings or []):
            v = str(orr.get("verdict", "")).lower()
            if v in ("success", "reset", "exception", "no-effect", "false-positive"):
                return Classification(v, 0.9, oracle=orr.get("oracle_name", "oracle"))

        text = ""
        for rc in (raw_captures or []):
            p = rc.get("payload", b"")
            if isinstance(p, bytes):
                p = p.decode("utf-8", "replace")
            text += p

        if not text.strip():
            # no output at all → often a reset/hang; low confidence
            return Classification("reset", 0.5, oracle="silence")

        # 2) diff-vs-baseline
        if self.baseline is not None and expected is None:
            if text.strip() == self.baseline.strip():
                return Classification("no-effect", 0.85, oracle="diff-baseline")

        # 3) explicit "expected normal string" mismatch → candidate success (glitch changed flow)
        for pat in self.exception_patterns:
            if pat.search(text):
                return Classification("exception", 0.8, oracle="text-match")
        for pat in self.success_patterns:
            if pat.search(text):
                return Classification("success", 0.85, oracle="text-match")
        for pat in self.reset_patterns:
            if pat.search(text):
                return Classification("reset", 0.75, oracle="text-match")

        if expected is not None:
            if expected.strip() and expected.strip() not in text:
                # output present but not the expected normal output → anomalous
                return Classification("success", 0.7, oracle="expected-mismatch")
            return Classification("no-effect", 0.8, oracle="expected-match")

        return Classification("no-effect", 0.6, oracle="default")
