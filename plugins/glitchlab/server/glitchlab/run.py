"""Single-process launcher: companion viewer + MCP Streamable-HTTP server, sharing one AppCore.

Both uvicorn servers run on the same asyncio event loop, so the MCP tools, the sweep engine, and the
browser all operate on the same live Python objects (spec §3 / §19: the human viewer is a peer
reader of the same store; the MCP server is the product).
"""
from __future__ import annotations

import argparse
import asyncio
import os

import uvicorn

from . import config
from .app_core import get_core
from .mcp_server import build_server
from .viewer.app import build_viewer


async def _serve():
    ap = argparse.ArgumentParser("glitchlab-run")
    ap.add_argument("--host", default=config.VIEWER_HOST)
    ap.add_argument("--port", type=int, default=config.VIEWER_PORT)
    ap.add_argument("--mcp-port", type=int, default=config.VIEWER_PORT + 1)
    ap.add_argument(
        "--project-profile",
        default=None,
        help="project YAML selected before AppCore/hardware construction",
    )
    ap.add_argument(
        "--glitcher",
        default=None,
        help="explicit adapter override (simulator|chipwhisperer_husky)",
    )
    args, _ = ap.parse_known_args()

    if args.project_profile:
        os.environ["GLITCHLAB_PROJECT_PROFILE"] = args.project_profile
    if args.glitcher:
        os.environ["GLITCHLAB_GLITCHER_OVERRIDE"] = args.glitcher
    core = get_core()

    viewer_app = build_viewer(core)
    srv = build_server(core)
    mcp_app = srv.streamable_http_app(streamable_http_path="/mcp", stateless_http=True,
                                      host=args.host)

    v = uvicorn.Server(uvicorn.Config(viewer_app, host=args.host, port=args.port,
                                      log_level="warning", loop="asyncio"))
    m = uvicorn.Server(uvicorn.Config(mcp_app, host=args.host, port=args.mcp_port,
                                      log_level="warning", loop="asyncio"))
    # ASCII output is intentional: redirected Windows consoles may still use cp1252.
    print(f"[GlitchLab] viewer  -> http://{args.host}:{args.port}")
    print(f"[GlitchLab] MCP HTTP -> http://{args.host}:{args.mcp_port}/mcp")
    print(f"[GlitchLab] glitcher = {args.glitcher or core.rig.glitcher_id}")
    _profile = core.rig.project_profile
    _evidence = dict(_profile.get("evidence") or {})
    _required_evidence = set(_evidence.get("required_for_success") or [])
    _project_owns_rigol = bool(_evidence.get("rigol")) and (
        not _required_evidence or "rigol" in _required_evidence
    )
    if (bool((_profile.get("viewer") or {}).get("autobind_scope", False))
            and not _project_owns_rigol):
        asyncio.create_task(core.autobind_scope())
    try:
        await asyncio.gather(v.serve(), m.serve())
    finally:
        shutdown = await core.shutdown_for_exit()
        if shutdown.get("exit_blocked"):
            print(
                "[GlitchLab] shutdown blocked: target state is preserved; "
                "process and rig lease remain held. Use the audited discard operation "
                "before requesting a normal shutdown."
            )
            while bool(getattr(core.glitcher, "_preserve", False)):
                await asyncio.sleep(1.0)
            core.release_devices()


def main():
    try:
        asyncio.run(_serve())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
