"""ScopeAdapter — single SCPI session behind an async lock (spec §16.2 concurrency rule).

An instrument is a single-session resource: all tools funnel through one session. The embedded Web
Control page is a separate human-only channel. The adapter also persists the bound `instrument`
record with live-read capabilities + probed source syntax (§16.3 step 3).
"""
from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import numpy as np

from ... import config
from .dho924s import DHO924S
from .discovery import discover_resource


class ScopeAdapter:
    def __init__(self, store=None) -> None:
        self.store = store
        self.dev: DHO924S | None = None
        self.resource: str | None = None
        self.idn: str | None = None
        self.caps: dict[str, Any] = {}
        self.source_syntax: dict[str, Any] = {}
        self.instrument_id: str | None = None
        self._lock = asyncio.Lock()
        self._driven_by_mcp = False

    @property
    def bound(self) -> bool:
        return self.dev is not None

    # -- discovery + bind ------------------------------------------------------------
    async def discover(self, hint_ip: str | None = None) -> dict:
        return await asyncio.to_thread(discover_resource, hint_ip)

    async def bind(self, resource: str | None = None, hint_ip: str | None = None,
                   probe_awg: bool = False) -> dict:
        async with self._lock:
            return await asyncio.to_thread(self._bind_sync, resource, hint_ip, probe_awg)

    def _bind_sync(self, resource: str | None, hint_ip: str | None,
                   probe_awg: bool = False) -> dict:
        import re
        # Resolve the scope IP from hint / explicit resource / discovery.
        ip = hint_ip
        if resource and not ip:
            m = re.search(r"TCPIP\d*::([^:]+)::", resource)
            ip = m.group(1) if m else None
        if resource is None and ip is None:
            disc = discover_resource(hint_ip)
            if not disc.get("ok"):
                return disc
            resource = disc["resource"]
            m = re.search(r"TCPIP\d*::([^:]+)::", resource)
            ip = m.group(1) if m else None
        # Connection candidates, in order. The raw SCPI SOCKET (port 5555) is PREFERRED because it
        # is independent of the VXI-11 link table: killed processes leak VXI-11 links ("error
        # creating link: 9") that lock out INSTR until a manual scope reset, but the socket port has
        # no such limit and TCP frees a dead client's socket on its own — so this self-heals with no
        # operator present. INSTR (VXI-11) is kept as a fallback.
        candidates = []
        if ip:
            candidates.append(f"TCPIP0::{ip}::5555::SOCKET")
        if resource and resource not in candidates:
            candidates.append(resource)
        if not candidates:
            return {"ok": False, "error": "no scope IP/resource resolved", "resource": None}
        last_err = None
        for res in candidates:
            try:
                self.dev = DHO924S(res)
                resource = res
                break
            except Exception as e:
                last_err = e
                self.dev = None
        if self.dev is None:
            return {"ok": False, "error": f"open failed: {last_err}", "resource": candidates}
        self.resource = resource
        self.idn = self.dev.idn
        self.caps = self.dev.capabilities()
        # AWG source-syntax probe is OFF by default: the DHO924S does not answer these queries,
        # so each one flashes a "Remote cmd error" on the scope and blocks for the full VISA
        # timeout. The AWG is unused in this project (rig_config scope_source.used=false), so we
        # skip it entirely. Pass probe_awg=True only when actually driving the generator.
        if probe_awg:
            try:
                self.source_syntax = self.dev.probe_source_syntax()
            except Exception as e:
                self.source_syntax = {"working_root": None, "error": str(e)}
        else:
            self.source_syntax = {"working_root": None,
                                  "note": "AWG source-syntax probe skipped (AWG unused; avoids "
                                          "scope 'Remote cmd error' + VISA timeouts)"}
        if self.store is not None:
            self.instrument_id = self.store.upsert_instrument(
                kind="oscilloscope", idn=self.idn, model=self.caps.get("model", "DHO924S"),
                serial=self.caps.get("serial", ""), firmware=self.caps.get("firmware", ""),
                resource_string=resource, capabilities=self.caps, source_syntax=self.source_syntax,
                safety_limits={"rated_max_input_v": self.caps.get("rated_max_input_v")},
                iid="scope-dho924s")
        return {"ok": True, "resource": resource, "idn": self.idn, "capabilities": self.caps,
                "source_syntax": self.source_syntax, "instrument_id": self.instrument_id}

    async def unbind(self) -> None:
        async with self._lock:
            if self.dev:
                await asyncio.to_thread(self.dev.close)
            self.dev = None

    # -- SAFE operations -------------------------------------------------------------
    async def measure(self, ch: int = 1) -> dict:
        async with self._lock:
            self._driven_by_mcp = True
            return await asyncio.to_thread(self.dev.measure_set, ch)

    async def capture(self, ch: int = 1, frozen: bool = False) -> tuple[np.ndarray, dict]:
        async with self._lock:
            self._driven_by_mcp = True
            return await asyncio.to_thread(self.dev.read_waveform, ch, frozen)

    # -- single-shot triggered acquisition (for scope-assisted timing discovery) ------
    async def arm_single(self, trig_source: str | None = None, trig_level_v: float | None = None,
                         slope: str = "POSitive") -> dict:
        async with self._lock:
            self._driven_by_mcp = True
            return await asyncio.to_thread(self.dev.arm_single, trig_source, trig_level_v, slope)

    async def wait_trigger(self, timeout_s: float = 5.0, poll_s: float = 0.05) -> dict:
        """Poll until the single-shot acquisition completes (status 'STOP')."""
        import time as _t
        t0 = _t.time()
        last = None
        while _t.time() - t0 < timeout_s:
            async with self._lock:
                last = await asyncio.to_thread(self.dev.trigger_status)
            if str(last).upper().startswith("STOP"):
                return {"ok": True, "status": last, "waited_s": _t.time() - t0}
            await asyncio.sleep(poll_s)
        return {"ok": False, "status": last, "timeout": True, "waited_s": _t.time() - t0}

    async def screenshot(self) -> bytes:
        async with self._lock:
            self._driven_by_mcp = True
            return await asyncio.to_thread(self.dev.screenshot_png)

    async def configure_acquisition(self, **kw) -> dict:
        async with self._lock:
            return await asyncio.to_thread(lambda: self.dev.configure_acquisition(**kw))

    async def configure_channel(self, ch: int, **kw) -> dict:
        async with self._lock:
            return await asyncio.to_thread(lambda: self.dev.configure_channel(ch, **kw))

    def status(self) -> dict:
        return {"bound": self.bound, "resource": self.resource, "idn": self.idn,
                "driven_by_mcp": self._driven_by_mcp, "instrument_id": self.instrument_id,
                "source_syntax": self.source_syntax.get("working_root"),
                "webcontrol": config.SCOPE_WEBCONTROL_URL}

    def rated_max_input_v(self) -> float | None:
        return self.caps.get("rated_max_input_v")

    # -- shared raw access (used by the bundled rigol-mcp toolset) -------------------
    async def raw(self, fn, *args, **kw):
        """Run fn(pyvisa_inst, *args) against the SINGLE bound session under the async lock.

        This lets the bundled rigol-mcp scope functions operate on the SAME connection the rest
        of GlitchLab uses (spec §16.2 single-session rule) — no second SCPI session to the scope.
        Binds first if needed.
        """
        if not self.bound:
            r = await self.bind()
            if not r.get("ok"):
                raise RuntimeError(f"scope not bound: {r.get('error')}")
        async with self._lock:
            self._driven_by_mcp = True
            return await asyncio.to_thread(lambda: fn(self.dev.inst, *args, **kw))
