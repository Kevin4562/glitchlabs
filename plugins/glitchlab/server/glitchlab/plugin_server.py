"""Codex plugin entrypoint: stdio MCP plus a localhost browser UI."""
from __future__ import annotations

import socket
import threading
import time

import uvicorn

from . import config
from .app_core import get_core
from .mcp_server import build_server
from .viewer.app import build_viewer


_UI_READY = threading.Event()
_UI_URL: str | None = None
_UI_ERROR: str | None = None


def _ui_worker() -> None:
    global _UI_URL, _UI_ERROR
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        sock.bind((config.VIEWER_HOST, 0))
        sock.listen(2048)
        port = int(sock.getsockname()[1])
        _UI_URL = f"http://{config.VIEWER_HOST}:{port}/"
        _UI_READY.set()
        server = uvicorn.Server(uvicorn.Config(
            build_viewer(get_core()),
            host=config.VIEWER_HOST,
            port=port,
            log_level="critical",
            access_log=False,
        ))
        server.run(sockets=[sock])
    except Exception as exc:  # pragma: no cover - platform/runtime dependent
        _UI_ERROR = f"{type(exc).__name__}: {exc}"
        _UI_READY.set()
        try:
            sock.close()
        except OSError:
            pass


def ensure_ui_started(timeout: float = 15.0) -> dict[str, object]:
    if not _UI_READY.is_set():
        threading.Thread(target=_ui_worker, name="glitchlab-ui", daemon=True).start()
    _UI_READY.wait(timeout)
    return {
        "ok": bool(_UI_URL and not _UI_ERROR),
        "ui_url": _UI_URL,
        "error": _UI_ERROR,
        "dynamic_port": True,
        "host": config.VIEWER_HOST,
    }


def ui_status() -> dict[str, object]:
    status = ensure_ui_started()
    if status["ok"]:
        time.sleep(0.02)
    return status


def main() -> None:
    ensure_ui_started()
    build_server(get_core()).run(transport="stdio")


if __name__ == "__main__":
    main()
