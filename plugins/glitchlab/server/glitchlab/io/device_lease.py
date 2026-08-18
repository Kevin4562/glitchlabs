"""Cross-process ownership guard for shared fault-injection instruments."""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any


class DeviceOwnershipError(RuntimeError):
    """Another process may own a device, or ownership could not be verified."""


def _process_rows() -> list[dict[str, Any]]:
    try:
        import psutil
    except Exception as exc:  # fail closed for a live rig
        raise DeviceOwnershipError(f"psutil is required for the owner guard: {exc!r}") from exc
    rows: list[dict[str, Any]] = []
    for proc in psutil.process_iter(["pid", "ppid", "name", "cmdline"]):
        try:
            info = proc.info
            rows.append(
                {
                    "pid": int(info.get("pid") or 0),
                    "ppid": int(info.get("ppid") or 0),
                    "name": str(info.get("name") or ""),
                    "command": " ".join(info.get("cmdline") or []),
                }
            )
        except (OSError, ValueError, getattr(psutil, "Error", Exception)):
            continue
    return rows


def find_device_owner_conflicts(current_pid: int | None = None) -> list[dict[str, Any]]:
    """Find likely Husky/J-Link/Rigol owners outside this process tree."""
    pid = int(current_pid or os.getpid())
    rows = _process_rows()
    own_tree = {pid}
    by_pid = {row["pid"]: row for row in rows}
    # On Windows a venv ``python.exe`` launcher can spawn the real interpreter
    # as its child while retaining the exact same command line.  Ignore only
    # that identical direct-launcher ancestor; it is not the process holding the
    # device handle.  Do not add it to ``own_tree`` because that would also
    # whitelist unrelated sibling Python processes.
    safe_launcher_ancestors: set[int] = set()
    child = by_pid.get(pid)
    while child is not None:
        parent = by_pid.get(child.get("ppid", 0))
        if parent is None:
            break
        parent_name = str(parent.get("name") or "").lower()
        parent_command = str(parent.get("command") or "").strip().lower()
        child_command = str(child.get("command") or "").strip().lower()
        if (
            parent_name.startswith("python")
            and parent_command
            and parent_command == child_command
        ):
            safe_launcher_ancestors.add(int(parent["pid"]))
            child = parent
            continue
        break
    # Worker children belong to this GlitchLab process and are allowed. Do not
    # add ancestors: doing so would also whitelist sibling processes launched by
    # the same shell/agent, including a stale GlitchLab instance.
    changed = True
    while changed:
        changed = False
        for row in rows:
            if row["ppid"] in own_tree and row["pid"] not in own_tree:
                own_tree.add(row["pid"])
                changed = True

    process_names = {
        "glitchlab.exe",
        "jlink.exe",
        "jlinkgdbservercl.exe",
        "jlinkgdbserver.exe",
        "jlinkremoteservercl.exe",
        "jlinkrttclient.exe",
        "openocd.exe",
        "pyocd.exe",
        "stm32cubeprogrammer.exe",
    }
    command_markers = (
        "glitchlab.run",
        "chipwhisperer",
        "rigol_dho900.py",
        "dho924s",
    )
    conflicts = []
    script_host_names = {
        "python.exe", "pythonw.exe", "python3.exe", "python3.11.exe",
        "python3.12.exe", "python3.13.exe", "python3.14.exe",
    }
    for row in rows:
        if row["pid"] in own_tree or row["pid"] in safe_launcher_ancestors:
            continue
        name = row["name"].lower()
        command = row["command"].lower().replace("\\", "/")
        # A launch shell often contains the child's complete command line (and
        # therefore words such as ``glitchlab.run``) but cannot retain the
        # Python/J-Link USB handle after spawning it.  Match script markers only
        # on Python hosts; known native owners and live Rigol sockets are checked
        # separately.  This avoids a fresh GlitchLab process rejecting its own
        # harmless parent PowerShell wrapper without whitelisting sibling Python
        # device owners.
        marker_owner = name in script_host_names and any(
            marker in command for marker in command_markers
        )
        if name in process_names or marker_owner:
            conflicts.append(row)

    return conflicts


class DeviceLease:
    """Hold an OS-released exclusive file lock for one GlitchLab rig owner."""

    def __init__(self, path: str | os.PathLike):
        self.path = Path(path)
        self._handle = None

    def acquire(self) -> dict[str, Any]:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = self.path.open("a+b")
        try:
            if os.name == "nt":
                import msvcrt

                handle.seek(0)
                if handle.tell() == 0 and self.path.stat().st_size == 0:
                    handle.write(b" ")
                    handle.flush()
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:  # pragma: no cover - Windows is the live rig
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except Exception as exc:
            handle.close()
            raise DeviceOwnershipError(f"rig lease is already held: {self.path}: {exc}") from exc
        handle.seek(0)
        handle.truncate()
        payload = {"pid": os.getpid(), "cwd": os.getcwd()}
        handle.write(json.dumps(payload, sort_keys=True).encode("utf-8"))
        handle.flush()
        self._handle = handle
        return {"ok": True, "path": str(self.path), **payload}

    def release(self) -> None:
        handle, self._handle = self._handle, None
        if handle is None:
            return
        try:
            if os.name == "nt":
                import msvcrt

                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:  # pragma: no cover
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()

    def __enter__(self):
        self.acquire()
        return self

    def __exit__(self, exc_type, exc, tb):
        self.release()
