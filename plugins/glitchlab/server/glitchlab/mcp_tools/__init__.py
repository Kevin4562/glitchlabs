"""MCP tool registration (spec §11). Split by plane: read / write_data / control / scope / ui.

Every tool declares a `danger` level and machine-readable `safety` metadata in its MCP `meta`
(spec §11), and read-only tools are annotated readOnlyHint/openWorldHint=False.

"invisible" MCP tools read/gather data without moving the UI; "visible" MCP tools (ui.py) drive the
active pages and hit the buttons so a human watching sees the screen move.
"""
from __future__ import annotations

try:
    from mcp.types import ToolAnnotations
except Exception:  # pragma: no cover
    ToolAnnotations = None


def anns(read_only: bool = False, destructive: bool = False, open_world: bool = False,
         idempotent: bool = False):
    if ToolAnnotations is None:
        return None
    try:
        return ToolAnnotations(readOnlyHint=read_only, destructiveHint=destructive,
                               openWorldHint=open_world, idempotentHint=idempotent)
    except Exception:
        return None


def meta(danger: str = "SAFE", kind: str = "invisible", max_tokens: int = 1500, **extra) -> dict:
    m = {"danger": danger, "kind": kind, "max_tokens": max_tokens}
    m.update(extra)
    return m
