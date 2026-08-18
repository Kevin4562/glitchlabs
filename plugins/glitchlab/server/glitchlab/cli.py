"""Typer CLI (spec §21). Launch server, run headless sweeps, export."""
from __future__ import annotations

import typer

app = typer.Typer(add_completion=False, help="GlitchLab — FI campaign server + viewer")


@app.command()
def serve(host: str = "127.0.0.1", port: int = 43127, glitcher: str = ""):
    """Run the viewer + MCP HTTP server (shared core)."""
    import sys
    from . import run
    if glitcher:
        sys.argv += ["--glitcher", glitcher]
    sys.argv += ["--host", host, "--port", str(port)]
    run.main()


@app.command()
def mcp_stdio():
    """Run the standalone Codex MCP server and its dynamic-port browser UI."""
    from .plugin_server import main
    main()


if __name__ == "__main__":
    app()
