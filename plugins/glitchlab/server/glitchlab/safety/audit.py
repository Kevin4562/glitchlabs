"""Audit writer + MCP logging notification hook (spec §4.2 audit_record, §18.3, §22 J3).

Every write and every actuation — executed, refused, or dry-run — writes an audit_record and
emits an event on the bus (which the MCP server surfaces as a logging notification, and the viewer
shows in its danger/audit panel).
"""
from __future__ import annotations

from .enforce import Decision


class Auditor:
    def __init__(self, store) -> None:
        self.store = store

    def record(self, tool: str, danger: str, params: dict, decision: str,
               violated_rule: str | None = None, result: dict | None = None) -> int:
        return self.store.audit(tool, danger, params, decision, violated_rule, result)

    def record_decision(self, tool: str, params: dict, dec: Decision,
                        result: dict | None = None) -> int:
        return self.record(tool, dec.danger, params, dec.decision, dec.violated_rule, result)
