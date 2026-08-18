"""ntfy.sh push notifications (spec §6.3 unattended-run alerting).

Pushes phone/desktop alerts on glitch successes and campaign errors so autonomous multi-day/-week
FI campaigns do not need a human watching the screen. The destination is kept in
GlitchLab's private per-user settings file and is never shipped with the plugin.

Non-blocking: every post is fired on a daemon thread so it never stalls the event loop. Delivery
failures never break a campaign, but are retained as status so the UI can explain missing alerts.
"""
from __future__ import annotations

import threading
import urllib.request
from datetime import datetime, timezone


class Notifier:
    def __init__(self, topic: str | None, base_url: str = "https://ntfy.sh", enabled: bool = True):
        self._lock = threading.Lock()
        self._last_attempt_at: str | None = None
        self._last_success_at: str | None = None
        self._last_error: str | None = None
        self._sent_count = 0
        self.configure(topic, base_url, enabled)

    def configure(self, topic: str | None, base_url: str, enabled: bool) -> None:
        """Apply a private destination without exposing it through status output."""
        with self._lock:
            self.topic = str(topic or "").strip() or None
            self.base = (base_url or "https://ntfy.sh").rstrip("/")
            self.enabled = bool(enabled and self.topic)
            self._last_error = None

    @property
    def url(self) -> str | None:
        return f"{self.base}/{self.topic}" if self.topic else None

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).isoformat(timespec="seconds")

    def status(self) -> dict:
        topic = self.topic or ""
        masked = (topic[:4] + "…" + topic[-4:]) if len(topic) > 10 else ("configured" if topic else "")
        with self._lock:
            return {
                "enabled": self.enabled,
                "configured": bool(self.topic),
                "topic": masked,
                "base_url": self.base,
                "last_attempt_at": self._last_attempt_at,
                "last_success_at": self._last_success_at,
                "last_error": self._last_error,
                "sent_count": self._sent_count,
            }

    def post(self, message: str, title: str | None = None, priority: int | None = None,
             tags: list[str] | None = None) -> bool:
        if not self.enabled:
            with self._lock:
                self._last_error = "notifications disabled or topic missing"
            return False

        with self._lock:
            self._last_attempt_at = self._now()
            self._last_error = None

        def _send():
            try:
                req = urllib.request.Request(self.url, data=message.encode("utf-8"), method="POST")
                if title:
                    req.add_header("Title", title)
                if priority:
                    req.add_header("Priority", str(priority))
                if tags:
                    req.add_header("Tags", ",".join(tags))
                with urllib.request.urlopen(req, timeout=8):
                    pass
                with self._lock:
                    self._last_success_at = self._now()
                    self._last_error = None
                    self._sent_count += 1
            except Exception as exc:
                with self._lock:
                    self._last_error = f"{type(exc).__name__}: {exc}"[:240]

        threading.Thread(target=_send, daemon=True).start()
        return True

    def attach_bus(self, bus, rig_name: str = "") -> None:
        """Subscribe to campaign events and push an alert for each notable one."""
        def on(ev):
            k, d = ev.kind, (ev.data or {})
            try:
                if k == "success":
                    self.post(f"GLITCH SUCCESS on {rig_name}\nparams={d.get('params', {})}",
                              title="Glitch SUCCESS", priority=5, tags=["tada", "rotating_light"])
                elif k == "handoff_done":
                    ok = d.get("ok")
                    self.post(f"Handoff {'complete' if ok else 'FAILED'}: {d.get('type')}",
                              title="Handoff " + ("done" if ok else "failed"),
                              priority=5 if ok else 4, tags=["package"])
                elif k == "sweep_done":
                    s = d.get("successes", 0)
                    t = d.get("timing") or {}
                    self.post(f"Sweep finished: {d.get('done')} attempts, {s} success(es), "
                              f"{t.get('elapsed_s', '?')}s.",
                              title="Sweep done" + (" (SUCCESS)" if s else ""),
                              priority=5 if s else 3, tags=["checkered_flag"])
                elif k == "sweep_refused":
                    self.post(f"Sweep REFUSED: {d.get('rule')} - {d.get('detail')}",
                              title="Sweep refused", priority=4, tags=["no_entry"])
                elif k == "campaign_error":
                    self.post(f"Campaign ERROR: {d.get('error')}",
                              title="Campaign error", priority=5, tags=["warning"])
            except Exception:
                pass
        bus.on(on)
