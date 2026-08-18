"""AI-facing campaign workflow and evidence inspection tools.

The operational tables intentionally retain the historical ``success`` outcome key for compatibility.
That key is a *candidate* until the project connector's complete confirmation contract has been persisted
and the attempt is marked ``verified``.  This module is the canonical read-side translation of those
low-level rows into the states an operator or agent should act on.
"""
from __future__ import annotations

import hashlib
import json
from typing import Annotated, Any, Literal

from pydantic import Field

from . import anns, meta
from .rig_state import target_state_interlock


def _json(value: Any, default: Any = None) -> Any:
    if value is None:
        return default
    if isinstance(value, (dict, list, int, float, bool)):
        return value
    if isinstance(value, (bytes, bytearray)):
        value = bytes(value).decode("utf-8", "replace")
    if isinstance(value, str):
        try:
            return json.loads(value)
        except (TypeError, ValueError):
            return default if default is not None else value
    return default


def _target_profile(core) -> tuple[str | None, dict]:
    """Return the active project profile in both current and legacy config layouts."""
    rig = getattr(core, "rig", None)
    if rig is None:
        return None, {}
    try:
        profile = rig.project_profile
    except Exception:
        profile = {}
    if isinstance(profile, dict) and profile:
        path = getattr(rig, "project_profile_path", None)
        name = str(profile.get("id") or (getattr(path, "stem", None) if path else "") or "")
        return name or None, dict(profile)

    # Compatibility with pre-project-profile rig files.
    raw = getattr(rig, "raw", {}) or {}
    name = (raw.get("rig") or {}).get("target_profile")
    profiles = raw.get("target_profile") or {}
    return name, (profiles.get(name) or {}) if name else {}


def _flatten_checks(value: Any, out: dict[str, bool] | None = None) -> dict[str, bool]:
    if out is None:
        out = {}
    if isinstance(value, dict):
        for key, child in value.items():
            if isinstance(child, bool):
                out[key] = child
            else:
                _flatten_checks(child, out)
    elif isinstance(value, list):
        for child in value:
            _flatten_checks(child, out)
    return out


def _oracle_detail(captures: list[dict]) -> dict:
    """Return the richest persisted oracle JSON capture, if present."""
    best: dict = {}
    best_score = -1
    for capture in captures:
        decoded = _json(capture.get("payload"), {})
        if not isinstance(decoded, dict):
            continue
        channel = str(capture.get("channel") or "").lower()
        score = len(_flatten_checks(decoded))
        if channel in {"connection", "oracle", "oracle_detail", "oracle_evidence"}:
            score += 100
        if decoded.get("schema_version"):
            score += 20
        if score > best_score:
            best = decoded
            best_score = score
    # New candidate bundles wrap the raw oracle as evidence.oracle.detail.  Unwrap only for the
    # historical raw-capture fallback; oracle_reading.detail remains canonical when present.
    nested = best.get("oracle") if isinstance(best, dict) else None
    if isinstance(nested, dict) and isinstance(nested.get("detail"), dict):
        return nested["detail"]
    underlying = best.get("underlying_oracle") if isinstance(best, dict) else None
    if isinstance(underlying, dict) and isinstance(underlying.get("detail"), dict):
        return underlying["detail"]
    return best


def confirmation_contract_status(
    outcome_class: str | None,
    persisted_verified: bool,
    oracle_readings: list[dict] | None,
    raw_captures: list[dict] | None = None,
) -> dict:
    """Validate the persisted project-composite and underlying connection contracts.

    This is deliberately reusable by the read path and manual-ingestion guard so
    there is only one definition of ``fully_confirmed``.
    """
    details = []
    for reading in oracle_readings or []:
        if not isinstance(reading, dict):
            continue
        detail = _json(reading.get("detail"), {})
        details.append(detail if isinstance(detail, dict) else {})
    composite = details[0] if details else {}
    fallback = _oracle_detail(raw_captures or [])
    underlying = composite.get("underlying_connection") or composite.get("underlying_oracle") or {}
    raw_detail = (underlying.get("detail") or {}) if isinstance(underlying, dict) else {}
    if not raw_detail and len(details) > 1:
        raw_detail = details[1]
    if not raw_detail:
        raw_detail = fallback
    if not isinstance(raw_detail, dict):
        raw_detail = {}

    checks = _flatten_checks(raw_detail)
    connector_contract = composite.get("schema_version") == "glitchlab.project-connection/v1"
    raw_composite_required = composite.get("required_checks") or {}
    composite_required = (
        raw_composite_required if isinstance(raw_composite_required, dict) else {}
    )
    connector_parameters = composite.get("connector_parameters") or {}
    composite_checks_complete = (
        isinstance(composite_required, dict)
        and bool(composite_required)
        and all(value is True for value in composite_required.values())
    )
    composite_complete = (
        connector_contract
        and composite.get("verified") is True
        and composite.get("confirmed") is True
        and composite.get("evidence_complete") is True
        and composite.get("attempt_valid", True) is not False
        and composite.get("infrastructure_failure", False) is not True
        and composite_checks_complete
    )
    configured_gates = raw_detail.get("required_gates")
    required_gates = list(configured_gates or checks.keys())
    gates_complete = (
        all(checks.get(gate) is True for gate in required_gates)
        if required_gates else True
    )
    raw_complete = gates_complete
    complete = (
        str(outcome_class or "") == "success"
        and bool(persisted_verified)
        and composite_complete
        and raw_complete
    )
    return {
        "complete": complete,
        "composite": composite,
        "raw_detail": raw_detail,
        "checks": checks,
        "required_gates": required_gates,
        "connector_contract": connector_contract,
        "connector_id": composite.get("connector_id"),
        "connector_fingerprint": composite.get("connector_fingerprint"),
        "connector_parameters": connector_parameters,
        "composite_required": composite_required,
        "project_gate_schema_complete": connector_contract,
        "required_project_gates": sorted(composite_required),
        "initial_raw_contract": {"complete": raw_complete},
        "late_runtime_contract": {"complete": True, "connector_owned": True},
        "composite_checks_complete": composite_checks_complete,
        "composite_complete": composite_complete,
        "raw_complete": raw_complete,
    }


def _physical_summary(scope_measurements: Any) -> dict:
    measurements = _json(scope_measurements, {})
    if not isinstance(measurements, dict):
        measurements = {}
    aliases = {
        "pulse_width_ns": ("pulse_width_ns", "glitch_low_ns"),
        "trigger_to_injection_us": ("trigger_to_injection_us", "trigger_to_glitch_us"),
        "observed_signal_min_v": ("observed_signal_min_v", "pin_dip_min_V", "dip_min_V"),
        "observed_signal_idle_v": ("observed_signal_idle_v", "pin_idle_V", "idle_V"),
        "ext_offset_readback": ("ext_offset_readback",),
        "ext_offset_requested": ("ext_offset_requested",),
    }
    summary: dict[str, Any] = {}
    for output, keys in aliases.items():
        for key in keys:
            if measurements.get(key) is not None:
                summary[output] = measurements[key]
                break
    for source, output, factor in (
        ("pulse_width_s", "pulse_width_ns", 1e9),
        ("trigger_to_injection_s", "trigger_to_injection_us", 1e6),
    ):
        if output not in summary and measurements.get(source) is not None:
            try:
                summary[output] = round(float(measurements[source]) * factor, 3)
            except (TypeError, ValueError):
                pass
    compact = {}
    for key, value in measurements.items():
        if isinstance(value, list):
            compact[key] = {"samples": len(value)}
        elif isinstance(value, (str, int, float, bool, dict)) or value is None:
            compact[key] = value
    return {
        "stored": bool(measurements),
        "summary": summary,
        "available_fields": sorted(measurements),
        "measurements": compact,
    }


def _profile_summary(core, name: str | None, profile: dict) -> dict:
    glitcher = dict(profile.get("glitcher") or {})
    glitcher_cfg = dict(glitcher.get("config") or {})
    connector = dict(profile.get("connector") or {})
    recipes = dict(profile.get("recipes") or {})
    path = getattr(getattr(core, "rig", None), "project_profile_path", None)
    return {
        "id": name,
        "title": profile.get("title"),
        "path": str(path or ""),
        "target": dict(profile.get("target") or {}),
        "glitcher_plugin": glitcher.get("plugin"),
        "connector_id": connector.get("id") or connector.get("plugin"),
        "connector_defaults": dict(connector.get("parameters") or {}),
        "fixed_phase": dict(glitcher_cfg.get("phase") or {}),
        "reset_profile": glitcher_cfg.get("reset_profile"),
        "boot_delay_s": (glitcher_cfg.get("timing") or {}).get("boot_delay_s"),
        "required_evidence": list((profile.get("evidence") or {}).get("required_for_success") or []),
        "recipes": recipes,
        "default_recipe": (
            recipes.get("reproduce")
            or recipes.get("local_refine")
            or recipes.get("discovery")
            or {}
        ),
        "proven_result": dict(profile.get("proven_result") or {}),
    }


def get_project_reproduction_recipe_data(core, verify_startup_snapshot: bool = False) -> dict:
    """Return the active operator-owned YAML recipe with content-addressed provenance."""
    profile_name, profile = _target_profile(core)
    recipe = dict((profile.get("recipes") or {}).get("reproduce") or {})
    if not profile_name or not recipe:
        return {"ok": False, "refused": True, "reason": "project_reproduce_recipe_missing",
                "project_profile": profile_name}
    canonical = json.dumps(recipe, sort_keys=True, separators=(",", ":"), default=str)
    rig = getattr(core, "rig", None)
    profile_sha256 = getattr(rig, "project_profile_source_sha256", None)
    snapshot_verified = None
    snapshot_profile_sha256 = None
    if verify_startup_snapshot:
        try:
            snapshot = core.run_configuration_snapshot()
            snapshot_profile_sha256 = (((snapshot.get("provenance") or {})
                                        .get("project_profile") or {}).get("sha256"))
            snapshot_verified = bool(
                snapshot_profile_sha256
                and (not profile_sha256 or snapshot_profile_sha256 == profile_sha256)
            )
        except Exception as exc:
            return {"ok": False, "refused": True,
                    "reason": "run_configuration_or_profile_drift",
                    "detail": str(exc), "project_profile": profile_name}
        if snapshot_verified is not True:
            return {"ok": False, "refused": True,
                    "reason": "project_profile_hash_not_sealed_in_startup_snapshot",
                    "project_profile": profile_name,
                    "profile_sha256": profile_sha256,
                    "snapshot_profile_sha256": snapshot_profile_sha256}
    param_spec = {
        "axes": json.loads(json.dumps(recipe.get("axes") or {})),
        "repeats_per_cell": int(recipe.get("repeats_per_cell") or 1),
        "random_seed": int(recipe.get("random_seed") or 0),
        "shuffle": bool(recipe.get("shuffle", False)),
        "scope_capture_every": 0,
        "stop_on_success": bool(recipe.get("stop_on_success", True)),
        "stop_on_infrastructure_error": True,
        "recipe_name": "reproduce",
        "recipe_profile_id": profile_name,
    }
    return {
        "ok": True,
        "source": "active_operator_owned_project_yaml",
        "project_profile": profile_name,
        "profile_path": str(getattr(rig, "project_profile_path", "") or ""),
        "profile_sha256": profile_sha256 or snapshot_profile_sha256,
        "recipe_sha256": hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
        "startup_snapshot_verified": snapshot_verified,
        "recipe": recipe,
        "param_spec": param_spec,
        "prior_result": dict(profile.get("proven_result") or {}),
        "prior_result_semantics": (
            "published/documented prior used to seed first acceptance; not a fully-confirmed "
            "attempt in this GlitchLab store"
        ),
        "target_state": target_state_interlock(core),
        "next": "define this exact fixed-point sweep, dry-run it, acknowledge limits, then start live",
    }


def _rig_limit(core, *path: str) -> Any:
    rig = getattr(core, "rig", None)
    if rig is not None and hasattr(rig, "limit"):
        return rig.limit(*path)
    value: Any = (getattr(rig, "raw", {}) or {}).get("limits", {})
    for key in path:
        value = value.get(key) if isinstance(value, dict) else None
    return value


def get_attempt_evidence_data(core, attempt_id: int, include_raw: bool = False,
                              max_raw_chars: int = 1200) -> dict:
    store = core.store
    row = store.fetch_one(
        "SELECT a.*,sw.name sweep_name,sw.status sweep_status,se.campaign_id,c.name campaign_name "
        "FROM attempt a JOIN sweep sw ON a.sweep_id=sw.id JOIN session se ON sw.session_id=se.id "
        "JOIN campaign c ON se.campaign_id=c.id WHERE a.id=?", (attempt_id,))
    if not row:
        return {"ok": False, "error": "attempt_not_found", "attempt_id": attempt_id}

    captures = store.fetch_all(
        "SELECT id,channel,payload,encoding,preamble FROM raw_capture WHERE attempt_id=? ORDER BY id",
        (attempt_id,))
    oracle = store.fetch_all(
        "SELECT oracle_name,verdict,latency_ms,detail FROM oracle_reading WHERE attempt_id=? ORDER BY id",
        (attempt_id,))
    env = store.fetch_one(
        "SELECT ambient_temp_c,board_temp_c,concurrent_bus_activity,aux_telemetry,scope_measurements "
        "FROM env_sample WHERE attempt_id=? ORDER BY id DESC LIMIT 1", (attempt_id,)) or {}
    for reading in oracle:
        reading["detail"] = _json(reading.get("detail"), {})
    outcome = str(row.get("outcome_class") or "")
    persisted_verified = bool(row.get("verified"))
    # The first reading is the connector-owned composite decision. A raw capture is
    # historical fallback only and cannot self-promote an unverified row.
    contract = confirmation_contract_status(outcome, persisted_verified, oracle, captures)
    composite = contract["composite"]
    raw_detail = contract["raw_detail"]
    checks = contract["checks"]
    explicit_confirmed = composite.get("confirmed")
    evidence_complete = composite.get("evidence_complete")
    composite_required = contract["composite_required"]
    composite_checks_complete = contract["composite_checks_complete"]
    project_gate_schema_complete = contract["project_gate_schema_complete"]
    initial_raw_contract = contract["initial_raw_contract"]
    late_runtime_contract = contract["late_runtime_contract"]
    required_gates = contract["required_gates"]
    contract_complete = contract["complete"]
    partial_candidate = (raw_detail.get("partial_candidate_observed") is True
                         or composite.get("partial_candidate_observed") is True)
    if contract_complete:
        classification = "fully_confirmed"
    elif partial_candidate:
        classification = "partial_candidate_unconfirmed"
    elif outcome == "success" or composite.get("candidate_credible") is True:
        classification = "candidate_unconfirmed"
    elif outcome in {"exception", "false-positive"}:
        classification = "infrastructure_or_ambiguous"
    else:
        classification = "non_success"

    missing = []
    failed = [name for name, passed in checks.items() if passed is False]
    failed.extend(f"project:{name}" for name, passed in composite_required.items()
                  if passed is not True)
    if classification in {"candidate_unconfirmed", "partial_candidate_unconfirmed"}:
        if not persisted_verified:
            missing.append("persisted verified flag from the connector confirmation path")
        if not composite:
            missing.append("persisted first project-composite connection reading")
        if composite.get("verified") is not True:
            missing.append("composite detail verified=true")
        if explicit_confirmed is not True:
            missing.append("composite detail confirmed=true")
        if evidence_complete is not True:
            missing.append("composite detail evidence_complete=true")
        if not composite_checks_complete:
            missing.append("every composite required_checks value=true")
        missing.extend(f"connection gate {gate}=true" for gate in required_gates
                       if checks.get(gate) is not True)
    physical = _physical_summary(env.get("scope_measurements"))

    raw_index = []
    for capture in captures:
        payload = capture.get("payload")
        if isinstance(payload, (bytes, bytearray)):
            encoding = str(capture.get("encoding") or "utf-8").lower()
            if encoding in {"json", "text", "plain"}:
                encoding = "utf-8"
            try:
                payload = bytes(payload).decode(encoding, "replace")
            except LookupError:
                payload = bytes(payload).decode("utf-8", "replace")
        entry = {"id": capture.get("id"), "channel": capture.get("channel"),
                 "encoding": capture.get("encoding"), "bytes": len(str(payload or ""))}
        if include_raw:
            entry["preview"] = str(payload or "")[:max_raw_chars]
            entry["truncated"] = len(str(payload or "")) > max_raw_chars
        raw_index.append(entry)

    params = _json(row.get("params"), {})
    connector_block = composite.get("connector") if isinstance(composite.get("connector"), dict) else {}
    connector_id = (
        composite.get("connector_id")
        or connector_block.get("id")
        or raw_detail.get("plugin")
        or composite.get("plugin")
    )
    connector_fingerprint = (
        composite.get("connector_fingerprint")
        or connector_block.get("fingerprint")
    )
    connector_parameters = (
        composite.get("connector_parameters")
        or connector_block.get("parameters")
        or {}
    )
    connection_evidence = {
        "readings": oracle,
        "schema_version": composite.get("schema_version"),
        "connector_id": connector_id,
        "connector_fingerprint": connector_fingerprint,
        "connector_parameters": connector_parameters,
        # Compatibility for clients created before the connection-module rename.
        "plugin": connector_id,
        "outcome": composite.get("outcome"),
        "confirmed": explicit_confirmed,
        "evidence_complete": evidence_complete,
        "verified": composite.get("verified"),
        "required_checks": composite_required,
        "composite_checks_complete": composite_checks_complete,
        "initial_raw_contract": initial_raw_contract,
        "late_runtime_contract": late_runtime_contract,
        "failure_stage": raw_detail.get("failure_stage") or composite.get("failure_stage"),
        "highest_passed_stage": raw_detail.get("highest_passed_stage"),
        "partial_candidate_observed": partial_candidate,
        "partial_stage_evidence": raw_detail.get("partial_stage_evidence") or {},
        "stages": raw_detail.get("stages") or [],
        "checks": composite.get("checks") or {},
        "flattened_confirmation_gates": checks,
        "required_confirmation_gates": required_gates,
        "failed_gates": failed,
        "detail": composite,
        "underlying_detail": raw_detail,
        "source": "oracle_reading.detail" if composite else "raw_capture_fallback",
        "storage_compatibility": "oracle_reading is the legacy database table name",
    }
    return {
        "ok": True,
        "attempt_id": attempt_id,
        "classification": classification,
        "fully_confirmed": classification == "fully_confirmed",
        "candidate": classification in {"candidate_unconfirmed", "partial_candidate_unconfirmed"},
        "interpretation": (
            "All persisted connector confirmation gates passed on a verified success attempt."
            if classification == "fully_confirmed" else
            "This incomplete target state must remain preserved; do not power-cycle/discard it, "
            "hand it off, or claim success until it is fully triaged."
            if classification == "partial_candidate_unconfirmed" else
            "The historical 'success' outcome is a candidate only; do not claim confirmation or perform "
            "a post-success handoff unless the complete confirmation contract is present."
            if classification == "candidate_unconfirmed" else
            "This attempt is not a confirmed glitch."
        ),
        "attempt": {
            "sweep_id": row.get("sweep_id"), "sweep_name": row.get("sweep_name"),
            "campaign_id": row.get("campaign_id"), "campaign_name": row.get("campaign_name"),
            "seq": row.get("seq"), "timestamp": row.get("ts"), "outcome": outcome,
            "confidence": row.get("outcome_confidence"), "verified": persisted_verified,
            "verdict_source": row.get("verdict_source"), "duration_ms": row.get("duration_ms"),
            "params": params, "notes": row.get("notes") or "",
        },
        "connection": connection_evidence,
        "oracle": connection_evidence,
        "physical_timing": physical,
        "environment": {
            "ambient_temp_c": env.get("ambient_temp_c"),
            "board_temp_c": env.get("board_temp_c"),
            "aux_telemetry": _json(env.get("aux_telemetry"), {}),
        },
        "missing_confirmation_evidence": missing,
        "raw_captures": raw_index,
    }


def _recent_stage_events(core) -> dict:
    wanted = {
        "preflight", "preflight_result", "glitcher_health", "husky_health",
        "oracle_health", "oracle_stage", "timing_result", "physical_timing",
        "sweep_started", "sweep_done", "sweep_refused", "campaign_error",
    }
    events = getattr(core, "bus", None)
    if events is None or not hasattr(events, "since"):
        return {}
    latest: dict[str, dict] = {}
    for event in events.since(0):
        if event.kind in wanted:
            latest[event.kind] = {"seq": event.seq, "ts": event.ts, "data": event.data}
    return latest


def get_workflow_state_data(core, campaign_id: str | None = None, sweep_id: str | None = None,
                            recent_attempts: int = 5) -> dict:
    store = core.store
    sweep_id = sweep_id or core.active.get("sweep_id")
    campaign_id = campaign_id or core.active.get("campaign_id")
    if sweep_id and not campaign_id:
        linked = store.fetch_one(
            "SELECT se.campaign_id FROM sweep sw JOIN session se ON sw.session_id=se.id WHERE sw.id=?",
            (sweep_id,))
        campaign_id = (linked or {}).get("campaign_id")
    if campaign_id and not sweep_id:
        linked = store.fetch_one(
            "SELECT sw.id FROM sweep sw JOIN session se ON sw.session_id=se.id "
            "WHERE se.campaign_id=? ORDER BY sw.created_at DESC LIMIT 1", (campaign_id,))
        sweep_id = (linked or {}).get("id")

    where, args = "", []
    if sweep_id:
        where, args = "WHERE a.sweep_id=?", [sweep_id]
    elif campaign_id:
        where = ("JOIN sweep sw ON a.sweep_id=sw.id JOIN session se ON sw.session_id=se.id "
                 "WHERE se.campaign_id=?")
        args = [campaign_id]
    aggregate = (store.fetch_one(
        "SELECT COUNT(*) attempts,"
        "SUM(CASE WHEN a.outcome_class='success' THEN 1 ELSE 0 END) success_rows,"
        "SUM(CASE WHEN EXISTS (SELECT 1 FROM oracle_reading oq WHERE oq.attempt_id=a.id "
        "AND json_valid(oq.detail) AND ("
        "json_extract(oq.detail,'$.partial_candidate_observed')=1 OR "
        "json_extract(oq.detail,'$.underlying_oracle.detail.partial_candidate_observed')=1)) "
        "OR EXISTS (SELECT 1 FROM raw_capture rc WHERE rc.attempt_id=a.id "
        "AND json_valid(CAST(rc.payload AS TEXT)) AND ("
        "json_extract(CAST(rc.payload AS TEXT),'$.partial_candidate_observed')=1 OR "
        "json_extract(CAST(rc.payload AS TEXT),'$.oracle.detail.partial_candidate_observed')=1)) "
        "THEN 1 ELSE 0 END) partial_candidates "
        f"FROM attempt a {where}", tuple(args)) or {}) if (sweep_id or campaign_id) else {}
    recent = store.fetch_all(
        "SELECT a.id FROM attempt a " + where + " ORDER BY a.id DESC LIMIT ?",
        tuple(args + [max(1, min(recent_attempts, 25))])) if (sweep_id or campaign_id) else []
    evidence = [get_attempt_evidence_data(core, int(row["id"]), include_raw=False)
                for row in recent]
    latest_candidate = next((item for item in evidence if item.get("candidate")), None)
    verified_rows = store.fetch_all(
        "SELECT a.id FROM attempt a " + where
        + (" AND" if "WHERE" in where else " WHERE")
        + " a.outcome_class='success' AND COALESCE(a.verified,0)=1 ORDER BY a.id DESC",
        tuple(args)) if (sweep_id or campaign_id) else []
    confirmed_evidence = []
    for verified_row in verified_rows:
        item = get_attempt_evidence_data(core, int(verified_row["id"]), include_raw=False)
        if item.get("fully_confirmed"):
            confirmed_evidence.append(item)
    latest_confirmed = confirmed_evidence[0] if confirmed_evidence else None

    manifest = core.capability_manifest()
    glitcher = dict(manifest.get("glitcher") or {})
    gl_obj = getattr(core, "glitcher", None)
    for attr in ("connection_health", "last_health", "serial_number", "firmware_version"):
        value = getattr(gl_obj, attr, None) if gl_obj is not None else None
        if isinstance(value, (str, int, float, bool, dict, list)):
            glitcher[attr] = value
    profile_name, profile = _target_profile(core)
    profile_detail = _profile_summary(core, profile_name, profile)
    project_recipe = get_project_reproduction_recipe_data(core, verify_startup_snapshot=False)
    connector_cfg = dict(profile.get("connector") or {})
    events = _recent_stage_events(core)
    preflight = (events.get("preflight_result") or events.get("preflight") or {}).get("data")
    timing_event = (events.get("timing_result") or events.get("physical_timing") or {}).get("data")
    measured_timing = bool(
        (isinstance(timing_event, dict) and timing_event.get("ok") is True)
        or any(item.get("physical_timing", {}).get("stored") for item in evidence)
    )
    project_evidence = profile.get("evidence") or {}
    project_rigol_owned = bool(project_evidence.get("rigol")) and (
        not project_evidence.get("required_for_success")
        or "rigol" in project_evidence.get("required_for_success", []))
    physical_acceptance = ((profile.get("proven_result") or {}).get("physical_acceptance") or {})
    project_managed_timing = bool(
        project_rigol_owned and physical_acceptance and preflight and preflight.get("ok") is True
    )
    timing_captured = measured_timing or project_managed_timing
    timing_detail = timing_event or ({
        "source": "profile-managed known envelope",
        "captured_this_session": False,
        "project_evidence_owned": True,
        "physical_acceptance": physical_acceptance,
        "preflight_validated": bool(preflight and preflight.get("ok") is True),
    } if project_rigol_owned else {})
    target_model = getattr(core.rig, "target_model", None) or (
        profile.get("target") or {}).get("chip") or "UNKNOWN"
    acknowledged = core.active.get("acknowledged_target") == target_model
    profile_ready = bool(profile_name and connector_cfg)
    target_state = target_state_interlock(core)

    counts = {key: int(aggregate.get(key) or 0)
              for key in ("attempts", "success_rows", "partial_candidates")}
    counts["confirmed"] = len(confirmed_evidence)
    counts["candidates"] = max(0, counts["success_rows"] - counts["confirmed"])
    if latest_confirmed:
        next_action = {"tool": "get_attempt_evidence",
                       "arguments": {"attempt_id": latest_confirmed["attempt_id"]},
                       "reason": (
                           "fully confirmed state is preserved; inspect/export persisted evidence "
                           "while a connector-owned handoff is unavailable"
                           if target_state["blocking"] else
                           "fully confirmed evidence is stored; the volatile target state is no "
                           "longer preserved, so inspect the record or begin a new epoch"
                       ),
                       "terminal": True}
    elif latest_candidate:
        next_action = {"tool": "get_attempt_evidence", "arguments": {
            "attempt_id": latest_candidate["attempt_id"]},
            "reason": "candidate must satisfy the complete persisted connector contract before it can be called fully confirmed"}
    elif target_state["blocking"]:
        next_action = {
            "tool": None,
            "reason": (
                "target-state interlock is latched; review/export persisted evidence. "
                "Only the explicit MCP discard operation may intentionally destroy this state"
            ),
            "terminal": True,
        }
    elif not profile_ready:
        next_action = {"tool": None,
                       "reason": "operator must select a valid project profile with a connector"}
    elif preflight is None or not preflight.get("ok", False):
        next_action = {"tool": "preflight_check",
                       "reason": "no passing preflight is present in this server session"}
    elif not timing_captured:
        next_action = {"tool": "discover_timing",
                       "reason": "physical trigger/injection timing has not been captured this session"}
    elif not acknowledged:
        next_action = {"tool": "acknowledge_target",
                       "arguments": {"target_model": target_model},
                       "reason": "review and echo the active project safety limits before live actuation"}
    elif sweep_id:
        next_action = {"tool": "control_sweep", "arguments": {"action": "status",
                                                                 "sweep_id": sweep_id},
                       "reason": "inspect or continue the defined sweep"}
    else:
        next_action = {"tool": "get_glitch_workflow", "arguments": {"mode": "discover"},
                       "reason": "create a deterministic discovery campaign"}

    stages = [
        {"name": "target_state", "status": target_state["state"],
         "detail": target_state},
        {"name": "project_profile", "status": "ready" if profile_ready else "blocked",
         "detail": profile_detail},
        {"name": "target_acknowledgment", "status": "ready" if acknowledged else "required",
         "detail": {"target_model": target_model,
                    "required_limits": {
                        "pulse_cycles_max": _rig_limit(core, "glitch", "pulse_cycles_max"),
                        "ext_offset_max": _rig_limit(core, "glitch", "ext_offset_max"),
                        "num_glitches_max": _rig_limit(core, "glitch", "num_glitches_max"),
                        "vcc_max_v": _rig_limit(core, "target_power", "vcc_max_v"),
                    }}},
        {"name": "husky_connection", "status": (
            "ready" if glitcher.get("bound") and (
                ((glitcher.get("connect_result") or {}).get("health") or {}).get("ok") is True
                or glitcher.get("simulator") is True)
            else "health_unknown" if glitcher.get("bound") else "not_connected"),
         "detail": glitcher},
        {"name": "preflight", "status": ("passed" if preflight and preflight.get("ok") else
                                             "failed" if preflight else "not_run"),
         "detail": preflight or {}},
        {"name": "physical_timing", "status": (
            "captured_this_session" if measured_timing
            else "profile_managed_known_envelope" if project_managed_timing
            else "profile_managed_pending_preflight" if project_rigol_owned
            else "not_captured"),
         "detail": timing_detail},
        {"name": "candidate", "status": "present" if counts["candidates"] else "none",
         "count": counts["candidates"]},
        {"name": "partial_candidate", "status": "present" if counts["partial_candidates"] else "none",
         "count": counts["partial_candidates"]},
        {"name": "fully_confirmed", "status": "present" if counts["confirmed"] else "none",
         "count": counts["confirmed"]},
    ]
    return {
        "ok": True,
        "campaign_id": campaign_id,
        "sweep_id": sweep_id,
        "active": dict(core.active),
        "counts": counts,
        "stages": stages,
        "latest_candidate": latest_candidate,
        "latest_confirmed": latest_confirmed,
        "recent_attempts": evidence,
        "recent_stage_events": events,
        "project_profile": profile_detail,
        "project_reproduction_recipe": project_recipe,
        "target_state": target_state,
        "controls_blocked": target_state["blocking"],
        "connector_profile": {"project_profile": profile_name,
                              "id": connector_cfg.get("id") or connector_cfg.get("plugin"),
                              "config": connector_cfg.get("config") or {},
                              "parameters": connector_cfg.get("parameters") or {}},
        "result_semantics": {
            "success": "legacy outcome key; candidate unless verified with complete connector evidence",
            "fully_confirmed": "verified success + persisted connector confirmation/evidence contract",
        },
        "next_action": next_action,
    }


def workflow_plan(mode: str = "discover") -> dict:
    common = [
        {"step": 1, "tool": "get_workflow_state", "purpose": "read active IDs, health, and evidence state"},
        {"step": 2, "tool": "preflight_check", "purpose":
         "fail closed on Husky identity/readback, target power/reset state, and connection baseline"},
        {"step": 3, "tool": "get_workflow_state", "conditional_tool": "discover_timing", "purpose":
         "require physical_timing=captured_this_session, or profile_managed_known_envelope after project preflight; call discover_timing only when the project does not own the Rigol"},
    ]
    if mode == "reproduce":
        steps = common + [
            {"step": 4, "tool": "get_project_reproduction_recipe|get_reproduction_recipe", "purpose":
             "for the first acceptance load the startup-sealed active YAML reproduce recipe; after a local fully-confirmed attempt, prefer that attempt's persisted tuple"},
            {"step": 5, "tool": "define_sweep", "purpose": "define a fixed-point reproduction sweep"},
            {"step": 6, "tool": "control_sweep", "arguments": {"action": "start", "dry_run": True},
             "purpose": "validate effective limits and readbacks without pulsing"},
            {"step": 7, "tool": "acknowledge_target", "purpose": "echo the enforced target limits"},
            {"step": 8, "tool": "control_sweep", "arguments": {"action": "start", "dry_run": False},
             "purpose": "run; preserve/stop on full confirmation or incomplete connector evidence"},
        ]
    else:
        steps = common + [
            {"step": 4, "tool": "open_campaign/open_session/define_sweep", "purpose":
             "persist campaign identity, unit, rig snapshot, and bounded coarse search"},
            {"step": 5, "tool": "control_sweep", "arguments": {"action": "start", "dry_run": True},
             "purpose": "validate the complete search and effective hardware bounds"},
            {"step": 6, "tool": "acknowledge_target", "purpose": "echo the enforced target limits"},
            {"step": 7, "tool": "control_sweep", "arguments": {"action": "start", "dry_run": False},
             "purpose": "run the bounded coarse search"},
            {"step": 8, "tool": "get_parameter_map/analyze_clusters", "purpose":
             "refine around disruptions and candidates while preserving negative coverage"},
        ]
    steps.extend([
        {"step": len(steps) + 1, "tool": "get_attempt_evidence", "purpose":
         "require every project connector gate, runtime liveness, and physical evidence; preserve/export the record"},
    ])
    return {
        "mode": mode,
        "steps": steps,
        "stop_conditions": [
            "Preserve/disarm on fully confirmed or incomplete partial target evidence.",
            "Continue only after the connector classifies a complete false-positive.",
            "Never continue glitching after a fully_confirmed result.",
            "Stop on connection, timeout, or other infrastructure failure; never count it as a hit.",
        ],
        "required_distinction": "outcome_class=success is candidate; only fully_confirmed is actionable",
        "handoff": {"available": False,
                    "reason": "a preservation-safe handoff must be implemented by the target connector"},
    }


def register(srv, core):
    @srv.tool(
        name="get_workflow_state",
        title="Inspect campaign readiness and confirmation state",
        description=(
            "Read the active campaign's AI workflow state without connecting to hardware. Returns "
            "project/connector profile, cached Husky state, most recent preflight stages, physical-timing "
            "evidence, candidate count, fully-confirmed count, and the safest deterministic next tool. "
            "Call this first and after every candidate."
        ),
        annotations=anns(read_only=True, idempotent=True),
        meta=meta("SAFE", "invisible", 2400, semantic_states=["candidate_unconfirmed", "fully_confirmed"]),
    )
    def get_workflow_state(
        campaign_id: Annotated[str | None, Field(description=
            "Campaign to inspect. Omit to use the active campaign or the campaign linked to sweep_id.")] = None,
        sweep_id: Annotated[str | None, Field(description=
            "Sweep to inspect. Omit to use the active/latest sweep in the selected campaign.")] = None,
        recent_attempts: Annotated[int, Field(ge=1, le=25, description=
            "Number of recent attempts to normalize into candidate/confirmed evidence summaries.")] = 5,
    ) -> dict:
        return get_workflow_state_data(core, campaign_id, sweep_id, recent_attempts)

    @srv.tool(
        name="get_attempt_evidence",
        title="Verify one glitch candidate",
        description=(
            "Normalize one persisted attempt into non_success, candidate_unconfirmed, or fully_confirmed. "
            "Inspects the project connector's staged JSON evidence, runtime gates, verified flag, raw-capture "
            "index, and oscilloscope timing. A legacy outcome_class='success' is never sufficient alone."
        ),
        annotations=anns(read_only=True, idempotent=True),
        meta=meta("SAFE", "invisible", 3000, confirmation_authority=True),
    )
    def get_attempt_evidence(
        attempt_id: Annotated[int, Field(ge=1, description="Database attempt ID to verify.")],
        include_raw: Annotated[bool, Field(description=
            "Include bounded text previews of raw captures. Leave false for normal triage.")] = False,
        max_raw_chars: Annotated[int, Field(ge=100, le=8000, description=
            "Maximum characters returned per raw capture when include_raw is true.")] = 1200,
    ) -> dict:
        return get_attempt_evidence_data(core, attempt_id, include_raw, max_raw_chars)

    @srv.tool(
        name="get_project_reproduction_recipe",
        title="Load the startup-sealed project reproduction recipe",
        description=(
            "Return the active operator-owned YAML recipes.reproduce entry, exact immutable sweep "
            "specification, profile/source hashes, and clearly labelled published-prior provenance. "
            "This is the deterministic bootstrap path before GlitchLab has its first locally "
            "fully-confirmed attempt. It verifies that the loaded project profile still matches the "
            "startup snapshot and never presents published prior counts as local confirmation."
        ),
        annotations=anns(read_only=True, idempotent=True),
        meta=meta("SAFE", "invisible", 1800, provenance="startup-sealed project YAML"),
    )
    def get_project_reproduction_recipe() -> dict:
        return get_project_reproduction_recipe_data(core, verify_startup_snapshot=True)

    @srv.tool(
        name="get_reproduction_recipe",
        title="Load an exact confirmed reproduction recipe",
        description=(
            "Return the exact parameter tuple and evidence provenance for a fully-confirmed attempt. "
            "By default refuses candidate-only rows so an AI cannot turn a false positive into a recipe."
        ),
        annotations=anns(read_only=True, idempotent=True),
        meta=meta("SAFE", "invisible", 1800, requires="fully_confirmed by default"),
    )
    def get_reproduction_recipe(
        attempt_id: Annotated[int, Field(ge=1, description=
            "Attempt whose exact requested parameters and measured evidence should be reproduced.")],
        allow_candidate: Annotated[bool, Field(description=
            "Allow an unverified candidate for diagnostic work. False is the safe reproduction default.")] = False,
    ) -> dict:
        evidence = get_attempt_evidence_data(core, attempt_id, include_raw=False)
        if not evidence.get("ok"):
            return evidence
        if not evidence.get("fully_confirmed") and not allow_candidate:
            return {"ok": False, "refused": True, "reason": "attempt_not_fully_confirmed",
                    "attempt_id": attempt_id, "classification": evidence.get("classification"),
                    "missing_confirmation_evidence": evidence.get("missing_confirmation_evidence")}
        attempt = evidence["attempt"]
        return {"ok": True, "attempt_id": attempt_id,
                "classification": evidence["classification"],
                "params": attempt["params"],
                "physical_timing": evidence["physical_timing"],
                "connector_id": evidence["connection"].get("connector_id"),
                "connector_fingerprint": evidence["connection"].get("connector_fingerprint"),
                "provenance": {"campaign_id": attempt["campaign_id"],
                               "sweep_id": attempt["sweep_id"], "sequence": attempt["seq"]},
                "next": "define a fixed-point sweep, dry-run it, then acknowledge and start live"}

    @srv.tool(
        name="get_glitch_workflow",
        title="Get the deterministic discovery or reproduction workflow",
        description=(
            "Return the ordered GlitchLab tool sequence, decision points, and stop conditions for "
            "end-to-end discovery or exact reproduction. This tool is read-only; it does not actuate the rig."
        ),
        annotations=anns(read_only=True, idempotent=True),
        meta=meta("SAFE", "invisible", 1600),
    )
    def get_glitch_workflow(
        mode: Annotated[Literal["discover", "reproduce"], Field(description=
            "discover explores and refines a bounded space; reproduce starts from a confirmed recipe.")] = "discover",
    ) -> dict:
        return workflow_plan(mode)
