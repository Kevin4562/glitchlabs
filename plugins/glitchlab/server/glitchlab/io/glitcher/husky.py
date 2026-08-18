"""Connector-driven ChipWhisperer Husky fault-delivery adapter."""
from __future__ import annotations

import json
import time
from typing import Any

from ...connections import ConnectionModule, ConnectionReading, make_connection_from_project
from .base import GlitchParams, GlitchResult, GlitcherAdapter
from .husky_health import require_husky_health, require_readback


class HuskyGlitcher(GlitcherAdapter):
    """Own fault delivery while a private connector owns target semantics."""

    id = "chipwhisperer_husky"
    is_simulator = False

    def __init__(
        self,
        *,
        project_profile: dict[str, Any],
        project_id: str | None = None,
        clkgen_freq: int = 7_370_000,
        trigger_line: str = "tio4",
        husky_serial: str | None = None,
        width_ceiling: int | None = None,
        offset_ceiling: int | None = None,
        vcc_ceiling: float | None = None,
        allow_both_mosfets: bool = False,
        **_: Any,
    ) -> None:
        if width_ceiling is None or offset_ceiling is None or vcc_ceiling is None:
            raise ValueError(
                "live Husky use requires target pulse_cycles_max, ext_offset_max, and vcc_max_v"
            )
        if not isinstance(project_profile, dict) or not isinstance(project_profile.get("connector"), dict):
            raise ValueError("live Husky use requires a target profile with a connector declaration")
        self.project_profile = dict(project_profile)
        self.project_id = str(project_id or project_profile.get("id") or "unknown-target")
        self.width_ceiling = int(width_ceiling)
        self.offset_ceiling = int(offset_ceiling)
        self.vcc_ceiling = float(vcc_ceiling)
        if self.width_ceiling < 1 or self.offset_ceiling < 0 or self.vcc_ceiling <= 0:
            raise ValueError("target safety limits are invalid")
        import chipwhisperer as cw  # optional live-only dependency

        self.cw = cw
        self.scope = None
        self.connection: ConnectionModule | None = None
        self.clkgen_freq = int(clkgen_freq)
        self.trigger_line = str(trigger_line)
        self.husky_serial = str(husky_serial or "")
        self.allow_both_mosfets = bool(allow_both_mosfets)
        self._caps: dict[str, Any] = {}
        self._preserve = False
        self._preserve_reason: str | None = None

    def _disarm(self) -> None:
        if self.scope is None:
            return
        try:
            self.scope.glitch.enabled = False
        except Exception:
            pass
        for pin in ("glitch_hp", "glitch_lp"):
            try:
                setattr(self.scope.io, pin, False)
            except Exception:
                pass

    def connect(self) -> dict[str, Any]:
        self.scope = self.cw.scope(sn=self.husky_serial or None)
        try:
            scope = self.scope
            if self.husky_serial and str(getattr(scope, "sn", "")) != self.husky_serial:
                raise RuntimeError("the connected Husky identity does not match the target profile")
            self._disarm()
            scope.clock.clkgen_src = "system"
            scope.clock.adc_mul = 1
            scope.clock.clkgen_freq = self.clkgen_freq
            scope.trigger.module = "basic"
            scope.trigger.triggers = self.trigger_line
            scope.adc.basic_mode = "rising_edge"
            scope.adc.samples = int((self.project_profile.get("capture") or {}).get("samples", 5000))
            scope.adc.offset = int((self.project_profile.get("capture") or {}).get("offset", 0))
            time.sleep(0.05)
            health = require_husky_health(scope, "live Husky connect")
            require_readback("clock frequency", self.clkgen_freq, int(scope.clock.clkgen_freq))
            require_readback("trigger line", self.trigger_line, str(scope.trigger.triggers))

            self.connection = make_connection_from_project(
                self.project_profile, project_id=self.project_id
            )
            self.connection.bind_glitcher(self)
            connection = self.connection.connect()
            if not isinstance(connection, dict) or connection.get("ok") is not True:
                raise RuntimeError(f"target connector failed closed: {connection!r}")
            self._caps = {
                "adapter": self.id,
                "simulator": False,
                "serial": getattr(scope, "sn", None),
                "firmware": getattr(scope, "fw_version", None),
                "clkgen_freq": self.clkgen_freq,
                "trigger_line": self.trigger_line,
                "width_ceiling": self.width_ceiling,
                "offset_ceiling": self.offset_ceiling,
                "vcc_ceiling": self.vcc_ceiling,
                "health": health,
                "connector": self.connection.describe(),
                "connector_connection": connection,
            }
            return {"ok": True, **self._caps}
        except Exception:
            self._disarm()
            try:
                if self.connection is not None:
                    self.connection.disconnect()
            finally:
                self.connection = None
            try:
                if self.scope is not None:
                    self.scope.dis()
            finally:
                self.scope = None
            raise

    def disconnect(self) -> None:
        self._disarm()
        try:
            if self.connection is not None:
                self.connection.disconnect()
        finally:
            self.connection = None
        try:
            if self.scope is not None:
                self.scope.dis()
        finally:
            self.scope = None

    @property
    def connected(self) -> bool:
        return self.scope is not None and self.connection is not None

    def capabilities(self) -> dict[str, Any]:
        return dict(self._caps)

    def _configure_glitch(self, params: GlitchParams) -> dict[str, Any]:
        if self.scope is None:
            raise RuntimeError("Husky is not connected")
        scope = self.scope
        width = int(params.width)
        offset = int(params.ext_offset if params.ext_offset is not None else params.offset)
        if not 1 <= width <= self.width_ceiling:
            raise ValueError(f"pulse cycles outside target limit 1..{self.width_ceiling}")
        if not 0 <= offset <= self.offset_ceiling:
            raise ValueError(f"external offset outside target limit 0..{self.offset_ceiling}")
        hp = bool(params.hp)
        lp = bool(params.lp) if (params.lp or params.hp) else True
        if hp and lp and not self.allow_both_mosfets:
            raise ValueError("simultaneous crowbar paths are forbidden by the target profile")

        scope.glitch.enabled = True
        scope.glitch.clk_src = "pll"
        scope.glitch.output = "enable_only"
        scope.glitch.trigger_src = "ext_single"
        scope.glitch.num_glitches = 1
        scope.glitch.repeat = width
        scope.glitch.ext_offset = offset
        scope.io.glitch_hp = hp
        scope.io.glitch_lp = lp
        if "fine_width" in params.extra:
            scope.glitch.width = int(round(float(params.extra["fine_width"])))
        if "fine_offset" in params.extra:
            scope.glitch.offset = int(round(float(params.extra["fine_offset"])))
        require_readback("pulse cycles", width, int(scope.glitch.repeat))
        require_readback("external offset", offset, int(scope.glitch.ext_offset))
        require_readback("glitch count", 1, int(scope.glitch.num_glitches))
        require_readback("output mode", "enable_only", str(scope.glitch.output))
        require_readback("trigger mode", "ext_single", str(scope.glitch.trigger_src))
        if bool(scope.io.glitch_hp) != hp or bool(scope.io.glitch_lp) != lp:
            raise RuntimeError("crowbar path readback mismatch")
        return {
            "pulse_cycles": int(scope.glitch.repeat),
            "ext_offset": int(scope.glitch.ext_offset),
            "hp": hp,
            "lp": lp,
            "fine_width": int(scope.glitch.width),
            "fine_offset": int(scope.glitch.offset),
            "output": str(scope.glitch.output),
            "num_glitches": int(scope.glitch.num_glitches),
        }

    def prepare(self) -> dict[str, Any]:
        if self.connection is None:
            raise RuntimeError("target connector is not connected")
        health = self.connection.probe_status(phase="preflight")
        if isinstance(health, dict) and health.get("ok") is False:
            return {"ok": False, "connector_health": health}
        baseline = self.connection.prepare_attempt({"phase": "preflight", "glitch_enabled": False})
        if not isinstance(baseline, dict) or baseline.get("ok") is not True:
            return {"ok": False, "connector_health": health, "baseline": baseline}
        return {"ok": True, "connector_health": health, "baseline": baseline}

    def attempt(self, params: GlitchParams, payload: bytes | None = None) -> GlitchResult:
        del payload
        if self.scope is None or self.connection is None:
            raise RuntimeError("live rig is not connected")
        started = time.perf_counter()
        effective = self._configure_glitch(params)
        connector_parameters = params.extra.get("connector_parameters") or {}
        self.connection.configure_attempt(connector_parameters)
        context = {
            "project_id": self.project_id,
            "requested": {
                "pulse_cycles": int(params.width),
                "ext_offset": int(params.ext_offset if params.ext_offset is not None else params.offset),
                "hp": bool(params.hp),
                "lp": bool(params.lp),
            },
            "effective": effective,
        }
        reading: ConnectionReading
        classification: dict[str, Any]
        trigger_result: dict[str, Any] = {}
        timed_out = False
        try:
            prepared = self.connection.prepare_attempt(context)
            if not isinstance(prepared, dict) or prepared.get("ok") is not True:
                raise RuntimeError(f"connector preparation failed: {prepared!r}")
            self.scope.arm()
            trigger_result = self.connection.trigger(context)
            if not isinstance(trigger_result, dict) or trigger_result.get("ok") is not True:
                raise RuntimeError(f"connector trigger failed: {trigger_result!r}")
            timed_out = bool(self.scope.capture())
            if timed_out:
                reading = ConnectionReading(
                    self.connection.name,
                    "exception",
                    detail={"infrastructure_failure": True, "reason": "capture_timeout"},
                )
            else:
                reading = self.connection.read(context)
            classification = self.connection.classify_attempt(
                primary=reading,
                context=context,
                evidence={"trigger": trigger_result, "capture_timeout": timed_out},
                parameters=connector_parameters,
            )
        finally:
            self._disarm()

        self._preserve = bool(classification.get("preserve"))
        self._preserve_reason = str(classification.get("classification") or "") if self._preserve else None
        elapsed_ms = (time.perf_counter() - started) * 1000.0
        observation = reading.as_reading()
        detail = {
            "schema_version": "glitchlab.project-connection/v1",
            "connector_id": self.connection.connector_id,
            "connector_parameters": dict(connector_parameters),
            "classification": classification.get("classification"),
            "verified": bool(classification.get("verified")),
            "confirmed": bool(classification.get("verified")),
            "evidence_complete": bool(classification.get("verified")),
            "required_checks": dict(classification.get("required_checks") or {}),
            "underlying_connection": observation,
            "trigger": trigger_result,
            "capture_timeout": timed_out,
        }
        return GlitchResult(
            outcome_hint=reading.verdict,
            raw_captures=[{
                "channel": "connector",
                "payload": json.dumps(detail, sort_keys=True),
                "encoding": "json",
            }],
            oracle_readings=[{
                "oracle_name": self.connection.name,
                "verdict": reading.verdict,
                "latency_ms": reading.latency_ms,
                "detail": detail,
            }],
            env_sample=None,
            duration_ms=elapsed_ms,
            reset_detected=reading.verdict == "reset",
            expected=None,
            meta={
                "effective": effective,
                "verified": bool(classification.get("verified")),
                "preserve_target": self._preserve,
                "attempt_valid": not timed_out,
                "connector_classification": classification,
            },
        )

    def power_cycle(self) -> dict[str, Any]:
        if self.connection is None:
            return {"ok": False, "error": "target connector is not connected"}
        result = self.connection.recover({"reason": "requested_recovery"})
        if result.get("ok") is True:
            self._preserve = False
            self._preserve_reason = None
        return result

    def program_target(self, hexfile: str, mcu: str = "") -> dict[str, Any]:
        del hexfile, mcu
        return {
            "ok": False,
            "unsupported": True,
            "error": "firmware programming belongs in a target connector with an explicit write policy",
        }

    def safe_shutdown(self) -> None:
        self._disarm()
