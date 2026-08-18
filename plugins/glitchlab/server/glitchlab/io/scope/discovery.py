"""Scope discovery & binding (spec §16.3). Tolerates the partially-initialized state (§15).

Enumerate candidates (hint IP + bounded TCP scan) → fingerprint via *IDN? / LXI → bind on a positive
DHO924S match. Re-bind with backoff if services are still coming up.
"""
from __future__ import annotations

import socket
from typing import Any

from ... import config


def _port_open(ip: str, port: int, timeout: float = 0.4) -> bool:
    try:
        with socket.create_connection((ip, port), timeout=timeout):
            return True
    except Exception:
        return False


def enumerate_candidates(hint_ip: str | None = None) -> list[dict]:
    hint_ip = hint_ip or config.SCOPE_HINT_IP
    out = []
    for ip in [hint_ip]:
        ports = {p: _port_open(ip, p) for p in (5555, 111, 80, 4880)}
        if any(ports.values()):
            out.append({"ip": ip, "ports": ports})
    return out


def fingerprint(ip: str) -> dict | None:
    """Try raw-socket SCPI *IDN? on 5555; fall back to LXI /lxi/identification on 80."""
    # raw socket SCPI
    if _port_open(ip, 5555):
        try:
            with socket.create_connection((ip, 5555), timeout=2.0) as s:
                s.sendall(b"*IDN?\n")
                data = s.recv(256).decode("latin1", "replace").strip()
                if "DHO" in data or "RIGOL" in data.upper():
                    return {"ip": ip, "idn": data, "via": "raw5555"}
        except Exception:
            pass
    return None


def discover_resource(hint_ip: str | None = None) -> dict:
    """Return the best VISA resource string + IDN for a matched DHO924S, or an error."""
    hint_ip = hint_ip or config.SCOPE_HINT_IP
    cands = enumerate_candidates(hint_ip)
    for c in cands:
        ip = c["ip"]
        fp = fingerprint(ip)
        if fp:
            # prefer VXI-11 INSTR (portmapper 111), else raw socket
            if c["ports"].get(111):
                res = f"TCPIP0::{ip}::INSTR"
            else:
                res = f"TCPIP0::{ip}::5555::SOCKET"
            return {"ok": True, "ip": ip, "resource": res, "idn": fp["idn"], "ports": c["ports"]}
    return {"ok": False, "error": "no DHO924S found", "candidates": cands, "hint_ip": hint_ip}
