"""Plugin runtime and browser-UI discovery tools."""
from __future__ import annotations

from typing import Any

from . import anns, meta


def register(srv, core) -> None:
    @srv.tool(
        name="get_glitchlab_status",
        title="Get GlitchLab UI and runtime status",
        description=(
            "Return the localhost browser URL, private data/config locations, active target, "
            "notification state, and discovered connector count. Call this before opening the UI."
        ),
        annotations=anns(read_only=True),
        meta=meta("SAFE", "visible", 1200),
    )
    def get_glitchlab_status() -> dict[str, Any]:
        from .. import __version__, config
        from ..connections import describe_connectors
        from ..plugin_server import ui_status

        ui = ui_status()
        try:
            connector_count = len(describe_connectors())
        except Exception:
            connector_count = 0
        return {
            **ui,
            "version": __version__,
            "data_dir": str(config.DATA_DIR),
            "rig_config": str(core.rig.path or config.USER_RIG_CONFIG),
            "target_profile": str(core.rig.project_profile_path or ""),
            "target_model": core.rig.target_model,
            "glitcher": core.rig.glitcher_id,
            "connector_count": connector_count,
            "notifications": core.notifier.status(),
        }
