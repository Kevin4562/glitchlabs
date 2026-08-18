"""In-process pub/sub event bus + UI command bus.

Two channels share this module:

* **Event bus** — domain events (`attempt_recorded`, `sweep_progress`, `sweep_done`,
  `outcome`, `scope_capture`, `danger_state`, `audit`). MCP subscriptions (spec §12) and the
  viewer both consume it. This is the "data" channel that keeps the UI live without polling.

* **UI command bus** — commands pushed *to* connected browsers so that **visible MCP tools**
  (spec goal: "controls the active pages and hits the buttons on the pages") make the human's
  screen actually move: navigate, click, fill fields, highlight. Browsers also push
  acknowledgements back, which the MCP tool returns to the agent.

Everything is asyncio-native and debounced where the spec requires it (§12: ≤1 update/sec).
"""
from __future__ import annotations

import asyncio
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Callable


@dataclass
class Event:
    kind: str
    data: dict
    ts: float = field(default_factory=time.time)
    seq: int = 0


class EventBus:
    """Async fan-out bus with a bounded replay ring for late subscribers / cursor polling."""

    def __init__(self, ring_size: int = 2000) -> None:
        self._subs: set[asyncio.Queue] = set()
        self._sync_subs: list[Callable[[Event], None]] = []
        self._ring: deque[Event] = deque(maxlen=ring_size)
        self._seq = 0
        self._lock = asyncio.Lock()

    def publish(self, kind: str, data: dict) -> Event:
        self._seq += 1
        ev = Event(kind=kind, data=data, seq=self._seq)
        self._ring.append(ev)
        for q in list(self._subs):
            try:
                q.put_nowait(ev)
            except asyncio.QueueFull:
                pass
        for fn in list(self._sync_subs):
            try:
                fn(ev)
            except Exception:
                pass
        return ev

    def subscribe(self, maxsize: int = 1000) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=maxsize)
        self._subs.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self._subs.discard(q)

    def on(self, fn: Callable[[Event], None]) -> None:
        """Register a synchronous callback (used by the store/CSV mirror)."""
        self._sync_subs.append(fn)

    def since(self, cursor: int) -> list[Event]:
        return [e for e in self._ring if e.seq > cursor]

    @property
    def cursor(self) -> int:
        return self._seq


class UICommandBus:
    """Pushes UI-control commands to connected viewer browsers and collects acknowledgements.

    `send()` returns the acknowledgement dict from the first browser that applied the command,
    so a *visible* MCP tool can report exactly what happened on screen.
    """

    def __init__(self) -> None:
        self._clients: dict[str, asyncio.Queue] = {}
        self._acks: dict[str, asyncio.Future] = {}
        self._cmd_id = 0
        self._last_state: dict[str, Any] = {"page": "live", "fields": {}, "clients": 0}

    def register(self, client_id: str) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=200)
        self._clients[client_id] = q
        self._last_state["clients"] = len(self._clients)
        return q

    def unregister(self, client_id: str) -> None:
        self._clients.pop(client_id, None)
        self._last_state["clients"] = len(self._clients)

    @property
    def client_count(self) -> int:
        return len(self._clients)

    def note_state(self, state: dict) -> None:
        self._last_state.update(state)

    @property
    def last_state(self) -> dict:
        return dict(self._last_state)

    async def send(self, action: str, payload: dict | None = None, timeout: float = 4.0) -> dict:
        """Broadcast a command to all viewers; await an ack from any one of them."""
        self._cmd_id += 1
        cmd_id = f"cmd{self._cmd_id}"
        cmd = {"type": "command", "id": cmd_id, "action": action, "payload": payload or {}}
        if not self._clients:
            return {"ok": False, "applied": False, "reason": "no_viewer_connected",
                    "action": action, "payload": payload or {}}
        loop = asyncio.get_event_loop()
        fut: asyncio.Future = loop.create_future()
        self._acks[cmd_id] = fut
        for q in list(self._clients.values()):
            try:
                q.put_nowait(cmd)
            except asyncio.QueueFull:
                pass
        try:
            ack = await asyncio.wait_for(fut, timeout=timeout)
            return {"ok": True, "applied": True, "action": action, **ack}
        except asyncio.TimeoutError:
            return {"ok": False, "applied": False, "reason": "ack_timeout", "action": action}
        finally:
            self._acks.pop(cmd_id, None)

    def resolve_ack(self, cmd_id: str, ack: dict) -> None:
        fut = self._acks.get(cmd_id)
        if fut and not fut.done():
            fut.set_result(ack)
