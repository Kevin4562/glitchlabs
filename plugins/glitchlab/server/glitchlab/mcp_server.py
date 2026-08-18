"""Build and register the complete GlitchLab MCP server."""
from __future__ import annotations

from mcp.server import MCPServer

from .app_core import get_core
from .mcp_tools import (
    connections,
    control,
    projects,
    prompts,
    read,
    resources,
    rigol,
    runtime,
    scope,
    ui,
    workflow,
    write_data,
)


def build_server(core=None) -> MCPServer:
    core = core or get_core()
    srv = MCPServer(
        name="GlitchLab",
        title="GlitchLab — Fault-Injection Campaign Server",
        version="2.0.0",
        instructions=(
            "GlitchLab runs bounded, evidence-preserving fault-injection campaigns. Call "
            "get_glitchlab_status to obtain the browser UI URL, then get_workflow_state and "
            "get_glitch_workflow(mode='discover'|'reproduce'). Treat outcome_class='success' as a "
            "candidate until the active connector's persisted required_checks are all true and the "
            "attempt is verified. Timeouts and incomplete observations are never confirmation. Use "
            "list_connectors before defining a sweep. Run preflight_check before every live epoch, "
            "start at the lowest practical injection energy, stay inside both rig and target limits, "
            "stop on infrastructure failure, preserve unresolved candidates, and never continue pulsing "
            "after full confirmation. The simulator is explicit and is never a fallback for a failed live "
            "adapter. Read/data tools do not move the UI; ui_* tools visibly drive it. Never use manual "
            "recording or SQL tools to manufacture verification."
        ),
    )
    runtime.register(srv, core)
    read.register(srv, core)
    connections.register(srv, core)
    workflow.register(srv, core)
    write_data.register(srv, core)
    projects.register(srv, core)
    control.register(srv, core)
    scope.register(srv, core)
    rigol.register(srv, core)
    ui.register(srv, core)
    resources.register(srv, core)
    prompts.register(srv, core)
    return srv
