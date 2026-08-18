"""Sweep engine (spec §6.2): inject → capture → classify → persist → publish a live update.

Drives ordered iteration over a param_spec. Every attempt passes the Safety Engine before any
hardware I/O. Emits `attempt_recorded` / `sweep_progress` / `sweep_done` on the bus (§6.2) so MCP
subscriptions and the viewer stay live. The crowbar is left DISARMED between attempts (safe_guards).
"""
from __future__ import annotations

import asyncio
import hashlib
import itertools
import json
import math
import random
import time
from typing import Any

import numpy as np

from ..io.glitcher.base import GlitchParams


class SweepEngine:
    def __init__(self, core) -> None:
        self.core = core
        self._tasks: dict[str, asyncio.Task] = {}
        self._controls: dict[str, dict] = {}   # sweep_id -> {paused, stop}
        self._progress: dict[str, dict] = {}   # sweep_id -> {start, total, done, paused_s, ...}
        self._validated_live_plans: dict[str, str] = {}
        self.last_capture: dict[str, Any] | None = None

    # -- live sweep timing (elapsed / rate / ETA) ------------------------------------
    def timing(self, sweep_id: str) -> dict | None:
        """Real wall-clock timing for a running/finished sweep: elapsed, attempts/min,
        seconds/attempt, and ETA to finish the whole parameter window."""
        pr = self._progress.get(sweep_id)
        if not pr:
            return None
        elapsed = max(1e-6, (pr.get("end") or time.time()) - pr["start"]) - pr.get("paused_s", 0.0)
        done = int(pr.get("done", 0))
        total = int(pr.get("total", 0))
        aps = done / elapsed if elapsed > 0 else 0.0
        spa = elapsed / done if done else 0.0
        remaining = max(0, total - done)
        return {"sweep_id": sweep_id, "running": self.is_running(sweep_id),
                "elapsed_s": round(elapsed, 1), "done": done, "total": total,
                "remaining": remaining, "pct": round(100 * done / total, 1) if total else 0.0,
                "attempts_per_min": round(aps * 60, 1), "s_per_attempt": round(spa, 2),
                "eta_s": round(remaining * spa, 1) if spa else None}

    # -- param grid ------------------------------------------------------------------
    @staticmethod
    def _axis_values(spec: Any) -> list[float]:
        if isinstance(spec, dict):
            lo, hi = spec.get("min", spec.get("lo")), spec.get("max", spec.get("hi"))
            step = spec.get("step", 1)
            n = spec.get("steps")
            if not all(
                isinstance(value, (int, float)) and not isinstance(value, bool)
                and math.isfinite(float(value))
                for value in (lo, hi)
            ):
                raise ValueError("axis min/max must be finite numbers")
            if float(lo) > float(hi):
                raise ValueError("descending axis ranges are not supported; min must be <= max")
            if n:
                if (not isinstance(n, int) or isinstance(n, bool)
                        or n < 1 or n > 1_000_000):
                    raise ValueError("axis steps must be an integer within 1..1000000")
                return list(np.linspace(lo, hi, int(n)))
            if (not isinstance(step, (int, float)) or isinstance(step, bool)
                    or not math.isfinite(float(step)) or float(step) <= 0):
                raise ValueError("axis step must be a finite positive number")
            vals, v = [], lo
            while v <= hi + 1e-9:
                vals.append(round(v, 6))
                if len(vals) > 1_000_000:
                    raise ValueError("axis expands beyond the 1000000-point safety limit")
                v += step
            return vals
        if isinstance(spec, (list, tuple)) and len(spec) == 2 and all(isinstance(x, (int, float)) for x in spec):
            return [spec[0], spec[1]]
        if isinstance(spec, (list, tuple)):
            if not spec:
                raise ValueError("sweep axes cannot be empty")
            for value in spec:
                if (isinstance(value, (int, float)) and not isinstance(value, bool)
                        and not math.isfinite(float(value))):
                    raise ValueError("axis values must be finite")
            return list(spec)
        if spec is None:
            raise ValueError("sweep axis values cannot be null")
        if (isinstance(spec, (int, float)) and not isinstance(spec, bool)
                and not math.isfinite(float(spec))):
            raise ValueError("axis values must be finite")
        return [spec]

    def build_points(self, param_spec: dict) -> list[dict]:
        # pulse_cycles is the canonical enable-only crowbar duration. ``width``
        # remains a legacy alias for older projects. ``repeats_per_cell`` means
        # independent shots and must not be confused with Husky glitch.repeat.
        axes = param_spec.get("axes") or {k: v for k, v in param_spec.items()
                                           if k in ("pulse_cycles", "width", "offset", "voltage", "ext_offset",
                                                     "mosfet", "fine_offset", "fine_width",
                                                     "x", "y", "z")}
        names = list(axes.keys())
        if not names:
            raise ValueError("a quantified sweep requires at least one explicit axis")
        value_lists = [self._axis_values(axes[n]) for n in names]
        repeats = int(param_spec.get("repeats_per_cell", param_spec.get("repeats", 1)))
        if repeats <= 0:
            raise ValueError("repeats_per_cell must be positive")
        cell_count = math.prod(len(values) for values in value_lists)
        if cell_count * repeats > 1_000_000:
            raise ValueError("sweep expands beyond the 1000000-shot safety limit")
        points = []
        for combo in itertools.product(*value_lists):
            base = dict(zip(names, combo))
            for shot in range(1, repeats + 1):
                points.append({**base, "cell_shot": shot})
        if param_spec.get("shuffle"):
            random.Random(int(param_spec.get("random_seed", 0))).shuffle(points)
        return points

    @staticmethod
    def _point_key(point: dict, axis_names: list[str]) -> str:
        """Stable identity for one independent shot in a persisted sweep."""
        value = {name: point.get(name) for name in axis_names}
        value["cell_shot"] = point.get("cell_shot")
        return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)

    # -- run a sweep -----------------------------------------------------------------
    async def run_sweep(self, sweep_id: str, param_spec: dict | None = None,
                        dry_run: bool = False, max_attempts: int | None = None) -> dict:
        if dry_run:
            return await self._run_sweep_body(
                sweep_id, param_spec, dry_run=True, max_attempts=max_attempts
            )
        try:
            async with self.core.exclusive_rig_operation(f"sweep:{sweep_id}"):
                return await self._run_sweep_body(
                    sweep_id, param_spec, dry_run=False, max_attempts=max_attempts
                )
        except Exception as exc:
            from ..app_core import RigBusyError
            if isinstance(exc, RigBusyError):
                return {"ok": False, "refused": True,
                        "violated_rule": "rig_operation_in_progress",
                        "detail": str(exc), "sweep_id": sweep_id}
            raise

    async def _run_sweep_body(self, sweep_id: str, param_spec: dict | None = None,
                              dry_run: bool = False,
                              max_attempts: int | None = None) -> dict:
        core = self.core
        sweep = core.store.get_sweep(sweep_id)
        if not sweep:
            return {"ok": False, "error": "sweep not found"}
        import json
        stored_spec = json.loads(sweep["param_spec"]) if sweep["param_spec"] else {}
        if param_spec is not None:
            canonical_override = json.dumps(param_spec, sort_keys=True, separators=(",", ":"))
            canonical_stored = json.dumps(stored_spec, sort_keys=True, separators=(",", ":"))
            if canonical_override != canonical_stored:
                return {
                    "ok": False,
                    "refused": True,
                    "violated_rule": "param_spec_override_forbidden",
                    "detail": (
                        "a sweep's persisted parameter plan is immutable; create a new sweep "
                        "instead of overriding it at start time"
                    ),
                }
        spec = stored_spec
        connector_selection = spec.get("connector")
        if connector_selection is not None:
            if not isinstance(connector_selection, dict):
                return {"ok": False, "refused": True,
                        "violated_rule": "connector_selection_invalid",
                        "detail": "sweep connector must be a mapping"}
            try:
                from ..connections import resolve_connector_selection

                resolved_connector = resolve_connector_selection(
                    core.rig.project_profile, connector_selection
                )
            except Exception as exc:
                return {"ok": False, "refused": True,
                        "violated_rule": "connector_selection_invalid",
                        "detail": str(exc)}
            if resolved_connector != connector_selection:
                return {"ok": False, "refused": True,
                        "violated_rule": "connector_source_or_parameter_drift",
                        "detail": "persisted connector selection no longer matches its source/schema",
                        "expected": connector_selection, "current": resolved_connector}
        # Validate/expand the immutable point order before opening any device.
        all_points = self.build_points(spec)
        axes = spec.get("axes") or {k: v for k, v in spec.items()
                                     if k in ("pulse_cycles", "width", "offset", "voltage",
                                              "ext_offset", "mosfet", "fine_offset",
                                              "fine_width", "x", "y", "z")}
        axis_names = list(axes.keys())
        # A stopped infrastructure epoch can be resumed on the same immutable
        # sweep. Skip only rows explicitly persisted as valid deliveries; invalid
        # rows never consume one of the requested independent cell shots.
        completed_keys: set[str] = set()
        existing_successes = 0
        for row in core.store.fetch_all(
            "SELECT a.params,a.outcome_class,e.aux_telemetry FROM attempt a "
            "LEFT JOIN env_sample e ON e.id=(SELECT MAX(e2.id) FROM env_sample e2 "
            "WHERE e2.attempt_id=a.id) WHERE a.sweep_id=? ORDER BY a.id",
            (sweep_id,),
        ):
            try:
                params = json.loads(row.get("params") or "{}")
                aux = json.loads(row.get("aux_telemetry") or "{}")
            except (TypeError, ValueError):
                continue
            if aux.get("attempt_valid") is True and params.get("cell_shot") is not None:
                completed_keys.add(self._point_key(params, axis_names))
                if row.get("outcome_class") == "success":
                    existing_successes += 1
        points = [
            point for point in all_points
            if self._point_key(point, axis_names) not in completed_keys
        ]
        if max_attempts:
            points = points[:max_attempts]
        # A completed immutable plan is a read-only result.  In particular, do
        # not reconnect to a preserved target merely because an operator or
        # agent repeats ``start`` after every valid cell_shot already exists.
        if not dry_run and not points:
            prior_status = str(sweep.get("status") or "done")
            if prior_status not in {"candidate-preserved", "infrastructure-failure"}:
                core.store.set_sweep_status(sweep_id, "done")
                core.store.set_session_status_for_sweep(sweep_id, "done")
                prior_status = "done"
            return {
                "ok": True,
                "sweep_id": sweep_id,
                "status": prior_status,
                "attempts": len(completed_keys),
                "valid_attempts": len(completed_keys),
                "invalid_attempts": 0,
                "planned_valid_attempts": len(all_points),
                "successes": existing_successes,
                "remaining_valid_points": 0,
                "hardware_touched": False,
            }

        # Bind a live sweep to the project and hash-addressed session snapshot
        # that created it.  A database namespace or historical sweep must never
        # select which physical target the currently loaded adapter actuates.
        declared_plugin, _ = core._glitcher_config()
        declared_simulator = declared_plugin == "simulator"
        binding = core.store.fetch_one(
            "SELECT se.rig_config,c.project_id,t.model target_model "
            "FROM sweep sw JOIN session se ON sw.session_id=se.id "
            "JOIN campaign c ON se.campaign_id=c.id "
            "JOIN target t ON c.target_id=t.id WHERE sw.id=?",
            (sweep_id,),
        )
        if not binding:
            return {"ok": False, "refused": True,
                    "violated_rule": "sweep_provenance_missing",
                    "detail": "sweep/session/campaign/target provenance is incomplete"}
        if not declared_simulator:
            if binding.get("project_id") != core.config_project_id:
                return {
                    "ok": False,
                    "refused": True,
                    "violated_rule": "configured_project_mismatch",
                    "detail": (
                        f"sweep belongs to project {binding.get('project_id')!r}, but the loaded "
                        f"hardware/oracle profile is {core.config_project_id!r}"
                    ),
                }
            if str(binding.get("target_model") or "") != str(core.rig.target_model):
                return {
                    "ok": False,
                    "refused": True,
                    "violated_rule": "configured_target_mismatch",
                    "detail": (
                        f"sweep target {binding.get('target_model')!r} does not match active "
                        f"target {core.rig.target_model!r}"
                    ),
                }
            try:
                sealed = json.loads(binding.get("rig_config") or "{}")
            except (TypeError, ValueError):
                sealed = {}
            current = core.run_configuration_snapshot()
            sealed_provenance = sealed.get("provenance") or {}
            current_provenance = current.get("provenance") or {}

            def _hashes(snapshot: dict) -> dict[str, str]:
                rows: dict[str, str] = {}
                for key in ("rig_config", "project_profile"):
                    item = snapshot.get(key) or {}
                    if item.get("path") and item.get("sha256"):
                        rows[str(item["path"])] = str(item["sha256"])
                for group in ("sources", "external_helpers"):
                    for item in snapshot.get(group) or []:
                        if item.get("path") and item.get("sha256"):
                            rows[str(item["path"])] = str(item["sha256"])
                connector = snapshot.get("connector") or {}
                for item in connector.get("files") or []:
                    if item.get("path") and item.get("sha256"):
                        rows[str(item["path"])] = str(item["sha256"])
                return rows

            sealed_hashes = _hashes(sealed_provenance)
            current_hashes = _hashes(current_provenance)
            if (sealed.get("schema_version") != "glitchlab.session-config/v2"
                    or not sealed_hashes
                    or sealed_hashes != current_hashes):
                changed = sorted(
                    path for path in set(sealed_hashes) | set(current_hashes)
                    if sealed_hashes.get(path) != current_hashes.get(path)
                )
                return {
                    "ok": False,
                    "refused": True,
                    "violated_rule": "session_configuration_drift",
                    "detail": (
                        "live start requires a v2 session snapshot whose rig, project profile, "
                        "and critical source hashes still match; open a new session after changes"
                    ),
                    "changed_paths": changed,
                }
        # Construct the adapter without opening any device, then run the static
        # safety gate.  Connection/preflight only happen after an identical plan
        # has passed dry-run in this server process.
        _g = core.ensure_glitcher(connect=False)
        _is_sim = bool(getattr(_g, "simulate", None)) or bool(getattr(_g, "is_simulator", False))
        run_snapshot = core.run_configuration_snapshot()
        validation_fingerprint = hashlib.sha256(
            json.dumps(
                {"sweep_id": sweep_id, "param_spec": spec,
                 "run_configuration": run_snapshot},
                sort_keys=True, separators=(",", ":"), default=str,
            ).encode("utf-8")
        ).hexdigest()
        dec = core.safety.check("control_sweep", {"param_spec": spec}, dry_run=dry_run,
                                context={"glitcher_bound": _g is not None,
                                          "param_spec": spec, "is_simulator": _is_sim,
                                          "dry_run": dry_run,
                                          "target_acknowledged":
                                             core.active.get("acknowledged_target") == core.rig.target_model})
        core.auditor.record_decision("control_sweep", {"sweep_id": sweep_id, "spec": spec}, dec)
        if not dec.allowed and dec.decision == "refused":
            core.bus.publish("sweep_refused", {"sweep_id": sweep_id, "rule": dec.violated_rule,
                                               "detail": dec.detail})
            return {"ok": False, **dec.refusal_dict()}
        if dec.decision == "dry_run":
            self._validated_live_plans[sweep_id] = validation_fingerprint
            return {"ok": True, "dry_run": True, "planned_points": len(all_points),
                    "remaining_valid_points": len(points),
                    "already_valid_points": len(completed_keys),
                    "detail": dec.detail, "sample": points[:3],
                    "validation_fingerprint": validation_fingerprint,
                    "live_start_authorized_for_this_process": True}

        if self._validated_live_plans.get(sweep_id) != validation_fingerprint:
            return {
                "ok": False,
                "refused": True,
                "violated_rule": "matching_dry_run_required",
                "detail": (
                    "call control_sweep(action='start', dry_run=true) for this exact immutable "
                    "plan and run-configuration fingerprint before live start"
                ),
                "validation_fingerprint": validation_fingerprint,
            }

        glitcher = core.ensure_glitcher(connect=True)
        # Sync a real target (reset + drain banner + confirm baseline) before glitching.
        if hasattr(glitcher, "prepare"):
            try:
                prep = await asyncio.to_thread(glitcher.prepare)
                core.bus.publish("target_prepared", prep)
                if not isinstance(prep, dict) or prep.get("ok") is not True:
                    raise RuntimeError(f"target preflight did not pass: {prep!r}")
            except Exception as exc:
                preflight_preserved = bool(getattr(glitcher, "_preserve", False))
                preflight_status = (
                    "candidate-preserved" if preflight_preserved else "preflight-failure"
                )
                try:
                    glitcher.safe_shutdown()
                finally:
                    core.store.set_sweep_status(sweep_id, preflight_status)
                    core.store.set_session_status_for_sweep(
                        sweep_id, preflight_status
                    )
                    core.bus.publish(
                        "sweep_stopped_preflight_failure",
                        {"sweep_id": sweep_id, "detail": repr(exc),
                         "candidate_preserved": preflight_preserved,
                         "status": preflight_status},
                    )
                return {"ok": False, "sweep_id": sweep_id,
                        "error": "preflight-failure", "detail": repr(exc),
                        "candidate_preserved": preflight_preserved,
                        "status": preflight_status}
        self._controls[sweep_id] = {"paused": False, "stop": False}
        self._progress[sweep_id] = {"start": time.time(), "total": len(all_points),
                                    "done": len(completed_keys), "invalid": 0,
                                    "paused_s": 0.0, "end": None}
        core.store.set_sweep_status(sweep_id, "running")
        core.store.set_sweep_measurement_state(sweep_id, "quantified")
        core.set_danger_state(glitch="ARMED (pulsed)")
        core.bus.publish("sweep_started", {"sweep_id": sweep_id,
                                            "points": len(all_points),
                                            "remaining_valid_points": len(points),
                                            "already_valid_points": len(completed_keys)})
        rate_limit = core.rig.limit("rate", "max_attempts_per_second", default=200) or 200
        min_dt = 1.0 / max(rate_limit, 1)

        successes = existing_successes
        done = len(completed_keys)
        invalid = 0
        consec_err = 0        # consecutive adapter/oracle/store errors — resilience guard
        scope_every = spec.get("scope_capture_every", 0)   # 0 = off
        stop_on_infra = bool(spec.get("stop_on_infrastructure_error", True))
        preserved_candidate = False
        infrastructure_failure: dict[str, Any] | None = None
        operator_stopped = False
        stopped_on_success = False
        try:
            for i, p in enumerate(points):
                ctl = self._controls[sweep_id]
                if ctl["stop"]:
                    operator_stopped = True
                    break
                while ctl["paused"] and not ctl["stop"]:
                    await asyncio.sleep(0.1)
                t_start = time.time()
                # Resolve the crowbar transistor path: an explicit "mosfet" (per-cell axis or spec
                # default) wins; otherwise fall back to the hp/lp booleans.
                _mos = p.get("mosfet", spec.get("mosfet"))
                if _mos in ("lp", "hp", "both"):
                    _hp, _lp = (_mos != "lp"), (_mos != "hp")
                else:
                    _hp = bool(p.get("hp", spec.get("hp", False)))
                    _lp = bool(p.get("lp", spec.get("lp", True)))
                _extra = {}
                if p.get("cell_shot") is not None:
                    _extra["cell_shot"] = int(p["cell_shot"])
                if p.get("fine_offset") is not None:
                    _extra["fine_offset"] = float(p["fine_offset"])
                if p.get("fine_width") is not None:
                    _extra["fine_width"] = float(p["fine_width"])
                _pulse_cycles = p.get("pulse_cycles", p.get("width", 1))
                _extra["pulse_cycles"] = int(_pulse_cycles)
                if _mos:
                    _extra["mosfet"] = _mos
                if isinstance(connector_selection, dict):
                    _extra["connector_id"] = connector_selection.get("id")
                    _extra["connector_fingerprint"] = connector_selection.get("fingerprint")
                    _extra["connector_parameters"] = dict(
                        connector_selection.get("parameters") or {}
                    )
                gp = GlitchParams(
                    width=_pulse_cycles, offset=p.get("offset", p.get("ext_offset", 0)),
                    voltage=p.get("voltage", core.rig.limit("target_power", "vcc_nominal_v",
                                                            default=3.3)),
                    repeat=1,
                    ext_offset=p.get("ext_offset"),
                    hp=_hp, lp=_lp, extra=_extra)
                # per-parameter enforcement (agent-adaptive safety, §18.5)
                d2 = core.safety.check("set_next_parameters", {"params": gp.as_dict()},
                                       context={})
                if not d2.allowed and d2.decision == "refused":
                    core.auditor.record_decision("set_next_parameters", gp.as_dict(), d2)
                    infrastructure_failure = {
                        "sweep_id": sweep_id,
                        "attempt": done,
                        "detail": d2.detail,
                        "violated_rule": d2.violated_rule,
                    }
                    core.bus.publish("sweep_stopped_parameter_refusal", infrastructure_failure)
                    break
                # Capture adapter/oracle/store failures with full evidence.  They
                # invalidate this cell_shot and stop the epoch, leaving it
                # retryable on an explicit resume after the infrastructure issue
                # has been corrected.
                _err_kind = None
                try:
                    result = await asyncio.to_thread(glitcher.attempt, gp, None)
                    cls = core.classifier.classify(result.raw_captures, result.oracle_readings,
                                                   expected=result.expected)
                    _meta = getattr(result, "meta", None) or {}
                    _err = bool(
                        _meta.get("attempt_valid") is not True
                        or
                        _meta.get("infrastructure_failure") is True
                        or any(
                            isinstance(reading, dict)
                            and isinstance(reading.get("detail"), dict)
                            and reading["detail"].get("infrastructure_failure") is True
                            for reading in (result.oracle_readings or [])
                        )
                    )
                    if _err:
                        # NOT a Python throw: the adapter explicitly marked an
                        # infrastructure failure. Preserve its staged oracle and
                        # capture evidence instead of reducing it to a blank row.
                        _err_kind = "adapter-infrastructure"
                        self._last_tb = ""
                        try:
                            self._last_err = json.dumps({
                                "oracle_readings": result.oracle_readings,
                                "raw_captures": [{k: str(v)[:300] for k, v in rc.items()}
                                                 for rc in (result.raw_captures or [])],
                            })[:1200]
                        except Exception:
                            self._last_err = "classifier returned 'exception' (detail unserialisable)"
                except Exception as _e:  # noqa
                    _err = True; result = None; _err_kind = "python-exception"
                    self._last_err = str(_e)[:200]
                    # CAPTURE THE FULL TRACEBACK. A bare "exception" row is unusable forensically:
                    # An exception can leave the physical target in a state that changes later
                    # observations. Retain the full traceback so infrastructure faults never become
                    # false candidates and every state-changing failure says why.
                    import traceback as _tb
                    self._last_tb = _tb.format_exc()[-4000:]
                if _err:
                    consec_err += 1
                    core.bus.publish("attempt_error", {"sweep_id": sweep_id, "consecutive": consec_err,
                                                       "detail": getattr(self, "_last_err", "oracle exception")})
                    invalid += 1
                    preserved_now = bool(
                        ((getattr(result, "meta", None) or {}).get("preserve_target")
                         if result is not None else False)
                        or getattr(glitcher, "_preserve", False)
                    )
                    persistence_error: Exception | None = None
                    try:
                        # Persist the full staged result when one exists, plus a
                        # loop-level diagnostic. The valid=false marker means this
                        # row cannot consume the requested cell_shot on resume.
                        _detail = {"kind": _err_kind or "unknown",
                                   "error": getattr(self, "_last_err", "")[:900],
                                   "traceback": getattr(self, "_last_tb", "")[-3500:],
                                   "params": gp.as_dict(),
                                   "preserve_target": preserved_now}
                        captures = list(result.raw_captures or []) if result is not None else []
                        captures.append({"channel": "error", "payload": json.dumps(_detail),
                                         "encoding": "json"})
                        readings = list(result.oracle_readings or []) if result is not None else []
                        if not readings:
                            readings = [{"oracle_name": "loop", "verdict": "exception",
                                         "detail": _detail}]
                        env = dict(result.env_sample or {}) if result is not None else {}
                        env["aux_telemetry"] = {
                            **(env.get("aux_telemetry") or {}),
                            "attempt_valid": False,
                            "infrastructure_failure": True,
                        }
                        _effective = ((getattr(result, "meta", None) or {}).get("effective")
                                      if result is not None else None)
                        if _effective:
                            env["aux_telemetry"]["effective_settings"] = _effective
                        aid = core.store.record_attempt(
                            sweep_id, gp.as_dict(), "exception", 0.3,
                            verdict_source="infrastructure-guard",
                            duration_ms=(result.duration_ms if result is not None else 0.0),
                            verified=False, raw_captures=captures,
                            oracle_readings=readings, env_sample=env,
                            notes=(f"invalid-attempt[{_err_kind or 'unknown'}]: "
                                   + getattr(self, "_last_err", ""))[:250],
                        )
                        if preserved_now:
                            preserved_candidate = True
                            core.bus.publish("candidate_preserved", {
                                "sweep_id": sweep_id, "attempt_id": aid,
                                "outcome": "exception", "verified": False,
                                "candidate_dir": ((getattr(result, "meta", None) or {})
                                                  .get("candidate_dir")
                                                  if result is not None else
                                                  str(getattr(glitcher, "_candidate_dir", "") or "")
                                                  or None),
                            })
                    except Exception as exc:
                        persistence_error = exc
                        try:
                            if preserved_now or getattr(glitcher, "_preserve", False):
                                preserved_candidate = True
                        except Exception:
                            pass
                        core.bus.publish("sweep_stopped_persistence_failure", {
                            "sweep_id": sweep_id,
                            "attempt": done,
                            "detail": (
                                "invalid-attempt evidence persistence failed: "
                                f"{exc!r}"
                            ),
                            "preserve_target": preserved_candidate,
                            "candidate_dir": str(
                                getattr(glitcher, "_candidate_dir", "") or ""
                            ) or None,
                        })
                    self._progress[sweep_id]["invalid"] = invalid
                    if consec_err % 15 == 0:            # escalate: disarm + longer settle to recover
                        try:
                            await asyncio.to_thread(glitcher.safe_shutdown)
                        except Exception:
                            pass
                        await asyncio.sleep(4.0)
                    else:
                        await asyncio.sleep(min(2.0, 0.2 * consec_err))
                    if persistence_error is not None:
                        infrastructure_failure = {
                            "sweep_id": sweep_id,
                            "attempt": done,
                            "detail": (
                                "invalid-attempt evidence persistence failed: "
                                f"{persistence_error!r}"
                            ),
                            "preserve_target": preserved_candidate,
                        }
                        break
                    if stop_on_infra:
                        infrastructure_failure = {
                            "sweep_id": sweep_id,
                            "attempt": done,
                            "detail": getattr(self, "_last_err", "adapter/oracle failure"),
                        }
                        core.bus.publish(
                            "sweep_stopped_infrastructure_failure", infrastructure_failure
                        )
                        break
                    continue
                consec_err = 0
                result_meta = getattr(result, "meta", None) or {}
                # Latch the adapter's preservation state before any database,
                # CSV, or UI bookkeeping.  A storage failure must not make the
                # sweep look safely complete while the physical target is being
                # deliberately held in a candidate state.
                result_preserves_target = bool(
                    result_meta.get("preserve_target")
                    or getattr(glitcher, "_preserve", False)
                )
                if result_preserves_target:
                    preserved_candidate = True
                    core.bus.publish("candidate_preservation_latched", {
                        "sweep_id": sweep_id,
                        "attempt_id": None,
                        "outcome": cls.outcome_class,
                        "verified": bool(result_meta.get("verified")),
                        "candidate_dir": result_meta.get("candidate_dir"),
                    })
                env = result.env_sample or {}
                env = {
                    **env,
                    "aux_telemetry": {
                        **(env.get("aux_telemetry") or {}),
                        "attempt_valid": True,
                    },
                }
                # Record a measured per-attempt signal dip (when supplied) as evidence in the
                # scope_measurements JSON column so it persists + rides the attempt_recorded event.
                _dip = (getattr(result, "meta", None) or {}).get("dip")
                if _dip:
                    env = {**env, "scope_measurements": {**(env.get("scope_measurements") or {}), **_dip}}
                _effective = (getattr(result, "meta", None) or {}).get("effective")
                if _effective:
                    env = {
                        **env,
                        "aux_telemetry": {
                            **(env.get("aux_telemetry") or {}),
                            "effective_settings": _effective,
                        },
                    }
                # attach a scope trace as evidence periodically (§16.4)
                scope_meas = None
                if scope_every and core.scope and core.scope.bound and (i % scope_every == 0):
                    try:
                        meas = await core.scope.measure(1)
                        scope_meas = meas
                        env = {**env, "scope_measurements": meas}
                    except Exception:
                        pass
                try:
                    aid = core.store.record_attempt(
                        sweep_id, gp.as_dict(), cls.outcome_class, cls.confidence,
                        verdict_source=cls.source, duration_ms=result.duration_ms,
                        verified=bool(result_meta.get("verified")),
                        raw_captures=result.raw_captures,
                        oracle_readings=result.oracle_readings or
                            [{"oracle_name": cls.oracle, "verdict": cls.outcome_class,
                              "latency_ms": 2.0}],
                        env_sample=env or None)
                except Exception as exc:
                    invalid += 1
                    # The hardware result was produced but could not be made
                    # durable. Even a completely classified connector non-goal
                    # must not be power-cycled on the assumption that an
                    # in-memory verdict is an adequate forensic record.
                    try:
                        if hasattr(glitcher, "preserve_target"):
                            glitcher.preserve_target("post-shot-persistence-failure")
                            preserved_candidate = True
                            result_preserves_target = True
                    except Exception:
                        # Preserve is best-effort only at this layer; the adapter
                        # still owns its own fail-safe output disarm.
                        preserved_candidate = bool(
                            preserved_candidate or getattr(glitcher, "_preserve", False)
                        )
                    infrastructure_failure = {
                        "sweep_id": sweep_id,
                        "attempt": done,
                        "detail": f"attempt persistence failed after hardware result: {exc!r}",
                        "preserve_target": result_preserves_target,
                    }
                    core.bus.publish(
                        "sweep_stopped_persistence_failure", infrastructure_failure
                    )
                    break
                if cls.outcome_class == "success":
                    successes += 1
                    core.bus.publish("success", {"sweep_id": sweep_id, "attempt_id": aid,
                                                  "params": gp.as_dict()})
                if result_preserves_target:
                    core.bus.publish("candidate_preserved", {
                        "sweep_id": sweep_id,
                        "attempt_id": aid,
                        "outcome": cls.outcome_class,
                        "verified": bool(result_meta.get("verified")),
                        "candidate_dir": result_meta.get("candidate_dir"),
                    })
                done += 1
                self._progress[sweep_id]["done"] = done
                if done % 5 == 0 or cls.outcome_class == "success":
                    core.bus.publish("sweep_progress", {
                        "sweep_id": sweep_id, "done": done, "invalid": invalid,
                        "total": len(all_points),
                        "successes": successes, "last_outcome": cls.outcome_class,
                        "timing": self.timing(sweep_id)})
                # Stop on the first fully verified success.  The project adapter
                # has already disarmed the crowbar and preserved the volatile
                # target state; no later shot may overwrite that evidence.
                if cls.outcome_class == "success" and spec.get("stop_on_success", True):
                    stopped_on_success = True
                    core.bus.publish("halt_on_success",
                                     {"sweep_id": sweep_id, "attempt_id": aid,
                                      "params": gp.as_dict(), "done": done})
                    break
                if preserved_candidate:
                    break
                # rate limit
                dt = time.time() - t_start
                if dt < min_dt:
                    await asyncio.sleep(min_dt - dt)
        finally:
            try:
                glitcher.safe_shutdown()
            except Exception:
                pass
            core.set_danger_state(glitch="DISARMED")
            final_status = (
                "candidate-preserved" if preserved_candidate
                else "infrastructure-failure" if infrastructure_failure
                else "aborted" if operator_stopped
                else "done" if done >= len(all_points)
                else "incomplete"
            )
            core.store.set_sweep_status(sweep_id, final_status)
            core.store.set_session_status_for_sweep(sweep_id, final_status)
            if sweep_id in self._progress:
                self._progress[sweep_id]["end"] = time.time()
            core.bus.publish("sweep_done", {"sweep_id": sweep_id, "done": done,
                                            "invalid": invalid,
                                            "successes": successes,
                                            "candidate_preserved": preserved_candidate,
                                            "infrastructure_failure": infrastructure_failure,
                                            "operator_stopped": operator_stopped,
                                            "stopped_on_success": stopped_on_success,
                                            "status": final_status,
                                            "planned_valid_attempts": len(all_points),
                                            "timing": self.timing(sweep_id)})
        return {"ok": infrastructure_failure is None, "attempts": done,
                "valid_attempts": done, "invalid_attempts": invalid,
                "planned_valid_attempts": len(all_points), "successes": successes,
                "candidate_preserved": preserved_candidate, "sweep_id": sweep_id,
                "status": final_status, "operator_stopped": operator_stopped,
                "remaining_valid_points": max(0, len(all_points) - done)}

    # -- control ---------------------------------------------------------------------
    def start(self, sweep_id: str, spec: dict | None = None, dry_run: bool = False,
              max_attempts: int | None = None) -> dict:
        active = next(
            (sid for sid, task in self._tasks.items() if not task.done()), None
        )
        if active is not None:
            return {"ok": False, "refused": True,
                    "violated_rule": "live_sweep_already_running",
                    "error": f"sweep {active} already owns the rig",
                    "active_sweep_id": active}
        rig_status = self.core.rig_operation_status()
        if rig_status.get("busy"):
            return {"ok": False, "refused": True,
                    "violated_rule": "rig_operation_in_progress",
                    "error": f"rig is busy with {rig_status.get('operation')}"}
        loop = asyncio.get_running_loop()
        task = loop.create_task(self.run_sweep(sweep_id, spec, dry_run, max_attempts))
        self._tasks[sweep_id] = task
        return {"ok": True, "started": sweep_id}

    def pause(self, sweep_id: str) -> dict:
        if sweep_id in self._controls:
            self._controls[sweep_id]["paused"] = True
            self.core.store.set_sweep_status(sweep_id, "paused")
            return {"ok": True, "paused": sweep_id}
        return {"ok": False, "error": "not running"}

    def resume(self, sweep_id: str) -> dict:
        if sweep_id in self._controls:
            self._controls[sweep_id]["paused"] = False
            self.core.store.set_sweep_status(sweep_id, "running")
            return {"ok": True, "resumed": sweep_id}
        return {"ok": False, "error": "not running"}

    def stop(self, sweep_id: str) -> dict:
        if sweep_id in self._controls:
            self._controls[sweep_id]["stop"] = True
            return {"ok": True, "stopping": sweep_id}
        return {"ok": False, "error": "not running"}

    def is_running(self, sweep_id: str) -> bool:
        t = self._tasks.get(sweep_id)
        return bool(t and not t.done())
