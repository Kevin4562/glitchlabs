from __future__ import annotations

import os
import json
from pathlib import Path

import pytest
from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client


@pytest.mark.asyncio
async def test_stdio_initialization_tools_and_dynamic_ui(tmp_path: Path) -> None:
    plugin_root = Path(__file__).resolve().parents[2]
    manifest = json.loads((plugin_root / ".mcp.json").read_text(encoding="utf-8"))
    configured = manifest["mcpServers"]["glitchlab"]
    env = dict(os.environ)
    env["GLITCHLAB_DATA"] = str(tmp_path / "stdio-data")
    params = StdioServerParameters(
        command=configured["command"],
        args=configured["args"],
        cwd=str(plugin_root),
        env=env,
    )
    async with stdio_client(params) as (read_stream, write_stream):
        async with ClientSession(read_stream, write_stream) as session:
            initialized = await session.initialize()
            tools = await session.list_tools()
            status = await session.call_tool("get_glitchlab_status", {})

    payload = status.structured_content
    assert initialized.server_info.name == "GlitchLab"
    assert initialized.server_info.version == "2.0.0"
    assert len(tools.tools) == 77
    assert payload["ok"] is True
    assert payload["dynamic_port"] is True
    assert payload["ui_url"].startswith("http://127.0.0.1:")
    assert payload["connector_count"] == 1
    assert payload["notifications"]["configured"] is False
