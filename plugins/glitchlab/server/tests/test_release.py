from __future__ import annotations

import asyncio
import json
from pathlib import Path

from fastapi.testclient import TestClient
import pytest

from glitchlab import config
from glitchlab.app_core import get_core
from glitchlab.connections import describe_connectors, make_connection_from_config
from glitchlab.connections.registry import ConnectorRegistry
from glitchlab.io.glitcher.base import GlitcherAdapter
from glitchlab.mcp_server import build_server
from glitchlab.viewer.app import build_viewer


EXPECTED_TOOLS = {
    "get_glitchlab_status",
    "list_campaigns", "query_attempts", "get_parameter_map", "analyze_clusters",
    "get_statistics", "bootstrap_confidence", "predict_parameters", "get_known_good",
    "get_raw_capture", "run_query", "describe_schema",
    "list_connectors", "get_connector_schema", "validate_connector_parameters",
    "get_connector_sdk_instructions", "get_workflow_state", "get_attempt_evidence",
    "get_project_reproduction_recipe", "get_reproduction_recipe", "get_glitch_workflow",
    "record_attempt", "open_campaign", "open_session", "define_sweep", "annotate",
    "reclassify", "save_known_good", "create_project", "list_projects",
    "set_active_project", "move_campaign", "control_sweep", "preflight_check",
    "inspect_preserved_target_state", "discover_timing", "run_handoff",
    "acknowledge_target", "discard_preserved_target_state", "set_next_parameters",
    "trigger_recovery", "flash_target", "move_stage", "describe_instrument",
    "scope_discover", "scope_bind", "scope_unbind", "scope_measure", "scope_capture",
    "scope_configure_acquisition", "scope_screenshot", "scope_channel_configure",
    "scope_source_configure", "scope_source_output", "rigol_idn",
    "rigol_get_scope_state", "rigol_measure", "rigol_measure_between",
    "rigol_get_waveform", "rigol_set_channel", "rigol_set_timebase",
    "rigol_set_trigger", "rigol_set_cursors", "rigol_get_cursor_values", "rigol_run",
    "rigol_stop", "rigol_single", "rigol_autoscale", "rigol_screenshot",
    "rigol_send_raw", "ui_navigate", "ui_click", "ui_set_field", "ui_fill_form",
    "ui_highlight", "ui_toast", "ui_get_state",
}


def _tool_payload(result) -> dict:
    return json.loads(result.content[0].text)


def test_complete_mcp_surface_registers() -> None:
    server = build_server(get_core())
    tools = asyncio.run(server.list_tools())
    resources = asyncio.run(server.list_resources())
    prompts = asyncio.run(server.list_prompts())
    assert {tool.name for tool in tools} == EXPECTED_TOOLS
    assert len(resources) == 4
    assert len(prompts) == 7


def test_only_bundled_connector_is_nonfunctional_template() -> None:
    connectors = describe_connectors()
    assert [item["id"] for item in connectors] == ["generic-example"]
    assert connectors[0]["source"] == "bundled example"
    connector = make_connection_from_config({"id": "generic-example"})
    result = connector.connect()
    assert result["ok"] is False
    assert result["template_only"] is True


def test_private_connector_can_supply_fingerprinted_glitcher_adapter(tmp_path: Path) -> None:
    connector_root = tmp_path / "private-target"
    connector_root.mkdir()
    (connector_root / "__init__.py").write_text("", encoding="utf-8")
    (connector_root / "connector.py").write_text(
        "from glitchlab.connections import ConnectionModule\n"
        "class PrivateConnection(ConnectionModule):\n"
        "    connector_id = 'private-target'\n"
        "    def read(self, context=None):\n"
        "        raise NotImplementedError\n",
        encoding="utf-8",
    )
    (connector_root / "adapter.py").write_text(
        "from glitchlab.io.glitcher.base import GlitcherAdapter\n"
        "class PrivateGlitcher(GlitcherAdapter):\n"
        "    id = 'private-delivery'\n"
        "    @property\n"
        "    def connected(self): return False\n"
        "    def connect(self): return {'ok': False}\n"
        "    def disconnect(self): return None\n"
        "    def capabilities(self): return {}\n"
        "    def prepare(self): return {'ok': False}\n"
        "    def attempt(self, params, payload=None): raise RuntimeError('test only')\n"
        "    def power_cycle(self): return {'ok': False}\n"
        "    def program_target(self, hexfile, mcu=''): return {'ok': False}\n"
        "    def safe_shutdown(self): return None\n",
        encoding="utf-8",
    )
    (connector_root / "glitchlab_connector.toml").write_text(
        "[connector]\n"
        "id = 'private-target'\n"
        "api_version = 1\n"
        "entrypoint = 'connector:PrivateConnection'\n\n"
        "[glitcher]\n"
        "id = 'private-delivery'\n"
        "api_version = 1\n"
        "entrypoint = 'adapter:PrivateGlitcher'\n",
        encoding="utf-8",
    )
    registry = ConnectorRegistry(roots=[tmp_path])
    cls = registry.load_glitcher_class("private-delivery")
    assert issubclass(cls, GlitcherAdapter)
    descriptor = registry.descriptor("private-target").public()
    assert descriptor["private_glitcher"]["id"] == "private-delivery"


def test_notifications_are_private_and_redacted() -> None:
    core = get_core()
    private_value = "release-test-topic-value"
    core.configure_notifications(
        enabled=True, topic=private_value, base_url="https://ntfy.sh"
    )
    status = core.notifier.status()
    assert status["configured"] is True
    assert private_value not in json.dumps(status)
    assert config.SETTINGS_PATH.is_relative_to(config.DATA_DIR)
    assert private_value in config.SETTINGS_PATH.read_text(encoding="utf-8")
    core.configure_notifications(enabled=False, topic="", base_url="https://ntfy.sh")


def test_browser_ui_and_public_bootstrap() -> None:
    client = TestClient(build_viewer(get_core()))
    page = client.get("/")
    assert page.status_code == 200
    assert "GlitchLab" in page.text
    bootstrap = client.get("/api/bootstrap")
    assert bootstrap.status_code == 200
    body = bootstrap.json()
    assert body["notifications"]["configured"] is False
    assert body["notifications"]["topic"] == ""
    default_recipe = body["project_profile"]["default_recipe"]
    assert default_recipe["axes"]["pulse_cycles"] == {
        "start": 1, "stop": 12, "step": 1,
    }
    assert default_recipe["axes"]["ext_offset"] == {
        "start": 0, "stop": 3000, "step": 100,
    }
    assert default_recipe["samples_per_cell"] == 1
    connectors = client.get("/api/connectors").json()["connectors"]
    assert connectors[0]["source"] == "bundled example"


@pytest.mark.asyncio
async def test_safe_simulator_campaign_lifecycle() -> None:
    server = build_server(get_core())

    async def call(name: str, arguments: dict) -> dict:
        return _tool_payload(await server.call_tool(name, arguments))

    campaign = await call("open_campaign", {
        "name": "release lifecycle test",
        "objective": "verify the bundled simulator without hardware",
        "target_model": "SIMULATED_UNKNOWN_TARGET",
    })
    assert campaign["ok"] is True
    session = await call("open_session", {
        "campaign_id": campaign["campaign_id"],
        "operator": "release-test",
    })
    assert session["ok"] is True
    sweep = await call("define_sweep", {
        "session_id": session["session_id"],
        "kind": "fixed-point",
        "name": "single safe simulated attempt",
        "param_spec": {
            "axes": {
                "pulse_cycles": {"min": 1, "max": 1, "step": 1},
                "ext_offset": {"min": 0, "max": 0, "step": 1},
                "mosfet": ["lp"],
            },
            "repeats_per_cell": 1,
            "stop_on_success": True,
        },
    })
    assert sweep["ok"] is True

    preflight = await call("preflight_check", {})
    assert preflight["ok"] is True
    dry_run = await call("control_sweep", {
        "action": "start", "sweep_id": sweep["sweep_id"], "dry_run": True,
    })
    assert dry_run["ok"] is True
    acknowledgment = await call("acknowledge_target", {
        "target_model": "SIMULATED_UNKNOWN_TARGET",
        "stated": {
            "pulse_cycles_max": 32,
            "ext_offset_max": 5000,
            "num_glitches_max": 1,
            "vcc_max_v": 3.6,
        },
    })
    assert acknowledgment["ok"] is True
    started = await call("control_sweep", {
        "action": "start", "sweep_id": sweep["sweep_id"], "dry_run": False,
    })
    assert started["ok"] is True

    status = {}
    for _ in range(100):
        status = await call("control_sweep", {
            "action": "status", "sweep_id": sweep["sweep_id"],
        })
        if not status["running"]:
            break
        await asyncio.sleep(0.02)
    assert status["running"] is False
    assert status["sweep"]["status"] == "done"
    attempts = await call("query_attempts", {"sweep_id": sweep["sweep_id"]})
    assert len(attempts["attempts"]) == 1
    assert attempts["attempts"][0]["classification"] == "non_success"


def test_release_tree_has_no_private_target_or_desktop_implementation() -> None:
    plugin_root = Path(__file__).resolve().parents[2]
    forbidden = (
        "192.168.", "c:\\users\\", "c:/users/", "desktop.py", "glitchlab-desktop",
    )
    for path in plugin_root.rglob("*"):
        if path.resolve() == Path(__file__).resolve():
            continue
        if any(part in {".venv", "__pycache__", ".pytest_cache"} for part in path.parts):
            continue
        if not path.is_file() or path.suffix.lower() in {".jpg", ".png", ".webp"}:
            continue
        text = path.read_text(encoding="utf-8", errors="ignore").lower()
        assert not any(token in text for token in forbidden), path
    assert not (plugin_root / "server" / "glitchlab" / "desktop.py").exists()
