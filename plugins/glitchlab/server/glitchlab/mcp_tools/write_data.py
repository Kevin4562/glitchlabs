"""Data-plane write tools — SAFE, additive, audited, invisible (spec §11.2).

These mutate the datastore only (append/annotate, never delete-in-place). Notes-mode is supported
(functional pass/fail + free text, no rate). None touch hardware.
"""
from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import Field

from . import anns, meta
from .workflow import get_attempt_evidence_data


def register(srv, core):
    store = core.store

    @srv.tool(name="record_attempt", title="Append attempt data without manufacturing confirmation",
              description="Append one attempt or a batch with requested/effective parameters, raw "
              "captures, staged connector detail, physical measurements, and notes. outcome_class='success' "
              "is stored as a candidate. This manual/import tool never accepts verified=true, even if "
              "the caller supplies contract-shaped JSON; only the live adapter/sweep pipeline may set "
              "the durable verified bit.", annotations=anns(idempotent=False),
              meta=meta("SAFE", "invisible", 600))
    def record_attempt(
        sweep_id: Annotated[str, Field(description="Existing sweep receiving the attempt(s).")],
        params: Annotated[dict | None, Field(description=
            "Complete requested/effective tuple in the sweep's hardware units.")] = None,
        outcome_class: Annotated[str | None, Field(description=
            "Optional legacy outcome key. Omit to classify supplied raw/connection evidence.")] = None,
        raw_captures: Annotated[list[dict] | None, Field(description=
            "Capture objects with channel, payload, encoding, and optional preamble.")] = None,
        oracle_readings: Annotated[list[dict] | None, Field(description=
            "Legacy storage name for connector readings with name, verdict, latency, and staged detail.")] = None,
        connection_readings: Annotated[list[dict] | None, Field(description=
            "Connector readings. Stored in the legacy oracle_reading table for database compatibility.")] = None,
        env_sample: Annotated[dict | None, Field(description=
            "Environment and physical scope measurements captured for this exact shot.")] = None,
        notes: Annotated[str, Field(description="Factual operator/agent note; not confirmation evidence.")] = "",
        confidence: Annotated[float, Field(ge=0, le=1, description=
            "Classifier confidence for the legacy outcome label.")] = 1.0,
        verified: Annotated[bool, Field(description=
            "Must be false. Live confirmation is owned by the hardware acquisition pipeline.")] = False,
        batch: Annotated[list[dict] | None, Field(description=
            "Batch of attempt objects using the same keys. Individual arguments are ignored when set.")] = None,
        notes_mode: Annotated[bool, Field(description=
            "Store qualitative notes without treating them as quantified rate evidence.")] = False,
    ) -> dict:
        if oracle_readings is not None and connection_readings is not None:
            return {"ok": False, "refused": True,
                    "reason": "supply_connection_readings_or_legacy_oracle_readings_not_both"}
        oracle_readings = connection_readings if connection_readings is not None else oracle_readings
        if batch:
            bad = [i for i, attempt in enumerate(batch) if attempt.get("verified")]
            if bad:
                return {"ok": False, "refused": True,
                        "reason": "manual_verified_forbidden",
                        "batch_indexes": bad}
            ids = []
            for a in batch:
                ids.append(_record_one(core, sweep_id, a))
            return {"ok": True, "recorded": len(ids), "attempt_ids": ids,
                    "result_semantics": "success rows remain candidates unless verified=true"}
        if notes_mode:
            # functional_unquantified: record with minimal params, no rate math
            store.set_sweep_measurement_state(sweep_id, "functional_unquantified")
            aid = store.record_attempt(sweep_id, params or {}, outcome_class or "no-data",
                                       confidence, verdict_source="manual", notes=notes,
                                       verified=False, raw_captures=raw_captures,
                                       oracle_readings=oracle_readings, env_sample=env_sample)
            return {"ok": True, "attempt_id": aid, "mode": "functional_unquantified",
                    "classification": "candidate_unconfirmed" if outcome_class == "success"
                    else "non_success"}
        oc = outcome_class
        source = "manual"
        if oc is None and raw_captures is not None:
            cls = core.classifier.classify(raw_captures, oracle_readings)
            oc, confidence, source = cls.outcome_class, cls.confidence, "classifier"
        oc = oc or "no-effect"
        if verified:
            return {"ok": False, "refused": True,
                    "reason": "manual_verified_forbidden",
                    "detail": "Only the live adapter/sweep pipeline can set verified=1."}
        aid = store.record_attempt(sweep_id, params or {}, oc, confidence,
                                   verdict_source=source, notes=notes, verified=verified,
                                   raw_captures=raw_captures, oracle_readings=oracle_readings,
                                   env_sample=env_sample)
        return {"ok": True, "attempt_id": aid, "outcome_class": oc,
                "verified": bool(verified), "classification": (
                    "fully_confirmed" if verified and oc == "success" else
                    "candidate_unconfirmed" if oc == "success" else "non_success"),
                "verdict_source": source}

    @srv.tool(name="open_campaign", title="Open a project-scoped campaign",
              description="Create a campaign under one project and make it active. Projects isolate "
              "target/connector configuration and totals; pass project_id explicitly when more than one is open.",
              meta=meta("SAFE", "invisible", 300))
    def open_campaign(
        name: Annotated[str, Field(min_length=1, description="Stable human-readable campaign name.")],
        objective: Annotated[str, Field(min_length=1, description="Concrete fault outcome being tested.")],
        target_model: Annotated[str, Field(min_length=1, description=
            "Exact target model matching the active project profile.")],
        vendor: Annotated[str, Field(description="Target vendor for provenance.")] = "",
        package: Annotated[str, Field(description="Target package/revision relevant to injection.")] = "",
        injection_types: Annotated[list[str] | None, Field(description=
            "Injection families used, for example ['voltage'].")] = None,
        mode: Annotated[Literal["full", "notes"], Field(description=
            "full records a live/quantified campaign and therefore must match the server-selected project and target; notes is analysis-only.")] = "full",
        project_id: Annotated[str | None, Field(description=
            "Owning project. A full campaign must equal the server-selected config_project_id; omit to use it.")] = None,
    ) -> dict:
        if mode == "full":
            active_project_id = getattr(core, "config_project_id", None)
            requested_project_id = project_id or core.active.get("project_id")
            active_target = core.rig.target_model
            if requested_project_id != active_project_id:
                return {"ok": False, "refused": True,
                        "reason": "live_project_profile_mismatch",
                        "detail": "full campaigns must use the server-selected project profile",
                        "active_project_id": active_project_id}
            if target_model != active_target:
                return {"ok": False, "refused": True,
                        "reason": "live_target_profile_mismatch",
                        "detail": "full campaigns must use the exact active target model",
                        "active_target_model": active_target}
        tid = store.get_or_create_target(target_model, vendor, package,
                                         injection_types or ["voltage"])
        cid = store.create_campaign(name, objective, tid, mode,
                                    project_id=project_id or core.active.get("project_id"))
        core.active.update({"campaign_id": cid, "target_id": tid})
        core.active.pop("ui_validated_sweep_id", None)
        return {"ok": True, "campaign_id": cid, "target_id": tid,
                "project_id": project_id or core.active.get("project_id")}

    @srv.tool(name="open_session", description="Create a session under a campaign (spec §11.2).",
              meta=meta("SAFE", "invisible", 300))
    def open_session(
        campaign_id: Annotated[str, Field(description="Existing campaign receiving the session.")],
        unit_serial: Annotated[str | None, Field(description=
            "Optional physical DUT serial; omit only when unit identity is genuinely unavailable.")] = None,
        operator: Annotated[str, Field(description="Operator or automation identity for provenance.")] = "",
        batch: Annotated[str, Field(description="Optional DUT lot/batch identifier.")] = "",
    ) -> dict:
        camp = store.get_campaign(campaign_id)
        if not camp:
            return {"ok": False, "error": "campaign not found"}
        uid = None
        if unit_serial:
            uid = store.create_unit(camp["target_id"], unit_serial, batch)
        sid = store.create_session(
            campaign_id, uid, operator, rig_config=core.run_configuration_snapshot()
        )
        core.active.update({"session_id": sid, "unit_id": uid})
        return {"ok": True, "session_id": sid, "unit_id": uid}

    @srv.tool(name="define_sweep", title="Persist a deterministic bounded sweep",
              description="Create a sweep with its exact axes, fixed values, repeat count, physical "
              "capture policy, and stop_on_success behavior. parent_sweep_id preserves the discovery/"
              "refinement graph. Creation does not touch hardware.", meta=meta("SAFE", "invisible", 300))
    def define_sweep(
        session_id: Annotated[str, Field(description="Existing session that snapshots rig and unit identity.")],
        kind: Annotated[Literal["grid", "random", "genetic", "bayesian", "fixed-point",
                                "spatial-grid", "config-bruteforce"], Field(description=
            "Search strategy. Use fixed-point for exact reproduction.")] = "grid",
        param_spec: Annotated[dict | None, Field(description=
            "Axes/fixed parameters in hardware units, repeats, capture policy, and stop_on_success.")] = None,
        parent_sweep_id: Annotated[str | None, Field(description=
            "Coarse/parent sweep being refined; omit for a root sweep.")] = None,
        name: Annotated[str, Field(description="Human-readable sweep name.")] = "",
        axis_flags: Annotated[dict | None, Field(description=
            "Flags such as equipment_quantized or confounded for each axis.")] = None,
    ) -> dict:
        from ..connections import resolve_connector_selection

        resolved_spec = dict(param_spec or {})
        connector = resolve_connector_selection(
            core.rig.project_profile,
            resolved_spec.get("connector") if isinstance(resolved_spec.get("connector"), dict) else None,
        )
        resolved_spec["connector"] = connector
        swid = store.create_sweep(session_id, kind, resolved_spec, parent_sweep_id, name,
                                  axis_flags)
        core.active.update({"sweep_id": swid})
        return {"ok": True, "sweep_id": swid, "parent_sweep_id": parent_sweep_id,
                "connector": connector,
                "connector_help": "Use list_connectors/get_connector_schema for dynamic parameters."}

    @srv.tool(name="annotate", description="Attach a causal hypothesis to a 1D range / 2D region, or "
              "flag an axis/region confounded/equipment_quantized (spec §11.2).",
              meta=meta("SAFE", "invisible", 300))
    def annotate(
        sweep_id: Annotated[str, Field(description="Sweep receiving the append-only annotation.")],
        text: Annotated[str, Field(min_length=1, description="Factual observation or explicit hypothesis.")],
        region: Annotated[dict | None, Field(description=
            "Optional parameter range/region to which the note applies.")] = None,
        flag: Annotated[str | None, Field(description=
            "Optional structured flag such as confounded or equipment_quantized.")] = None,
        author: Annotated[str, Field(description="Annotation author identity.")] = "agent",
    ) -> dict:
        aid = store.annotate(sweep_id, region or {}, text, flag, author)
        return {"ok": True, "annotation_id": aid}

    @srv.tool(name="reclassify", description="Re-triage an attempt's outcome from its stored raw "
              "capture — creates a NEW verdict version; the original is retained (spec §4.3/§11.2).",
              meta=meta("SAFE", "invisible", 300))
    def reclassify(
        attempt_id: Annotated[int, Field(ge=1, description="Attempt to re-triage without deleting history.")],
        outcome_class: Annotated[str | None, Field(description=
            "New legacy outcome key. Omit when use_classifier=true.")] = None,
        confidence: Annotated[float, Field(ge=0, le=1, description=
            "Confidence in a manual outcome label; not confirmation evidence.")] = 1.0,
        use_classifier: Annotated[bool, Field(description=
            "Re-run the classifier on stored captures instead of trusting outcome_class.")] = False,
    ) -> dict:
        if use_classifier or outcome_class is None:
            caps = store.fetch_all("SELECT channel,payload,encoding FROM raw_capture WHERE "
                                   "attempt_id=?", (attempt_id,))
            rc = [{"channel": c["channel"], "payload": c["payload"]} for c in caps]
            cls = core.classifier.classify(rc)
            outcome_class, confidence = cls.outcome_class, cls.confidence
            source = "classifier"
        else:
            source = "manual"
        v = store.reclassify(attempt_id, outcome_class, confidence, source)
        return {"ok": True, "attempt_id": attempt_id, "new_version": v,
                "outcome_class": outcome_class, "history": store.verdict_history(attempt_id)}

    @srv.tool(name="save_known_good", title="Save a confirmed reproduction profile",
              description="Persist an exact parameter tuple or bounded range only after linking a "
              "fully-confirmed attempt. Candidate-only and manually asserted provenance is refused.",
              meta=meta("SAFE", "invisible", 300))
    def save_known_good(
        target_model: Annotated[str, Field(min_length=1, description=
            "Exact target model for which the profile was confirmed.")],
        injection_type: Annotated[str, Field(min_length=1, description=
            "Injection family, for example voltage.")],
        known_good: Annotated[dict, Field(description=
            "Exact hardware-unit tuple or bounded range to reproduce; never inferred from labels alone.")],
        confirmed_attempt_id: Annotated[int, Field(ge=1, description=
            "Persisted attempt whose complete project connector contract proves this profile.")],
        provenance: Annotated[dict | None, Field(description=
            "Optional additional immutable provenance; confirmation fields are added by GlitchLab.")] = None,
    ) -> dict:
        evidence = get_attempt_evidence_data(core, confirmed_attempt_id, include_raw=False)
        if not evidence.get("fully_confirmed"):
            return {"ok": False, "refused": True, "reason": "attempt_not_fully_confirmed",
                    "attempt_id": confirmed_attempt_id,
                    "classification": evidence.get("classification"),
                    "missing_confirmation_evidence": evidence.get("missing_confirmation_evidence")}
        source = evidence["attempt"]
        full_provenance = dict(provenance or {})
        connector = evidence.get("connection", {})
        full_provenance.update({"confirmed_attempt_id": confirmed_attempt_id,
                                "campaign_id": source.get("campaign_id"),
                                "sweep_id": source.get("sweep_id"),
                                "classification": "fully_confirmed",
                                "connector_id": connector.get("connector_id"),
                                "connector_fingerprint": connector.get("connector_fingerprint"),
                                # Retained for readers of legacy known-good records.
                                "oracle_plugin": connector.get("connector_id")})
        pid = store.save_known_good(target_model, injection_type, known_good, full_provenance)
        return {"ok": True, "profile_id": pid, "provenance": full_provenance}


def _record_one(core, sweep_id, a: dict) -> int:
    oc = a.get("outcome_class")
    conf = a.get("confidence", 1.0)
    src = "manual"
    readings = a.get("connection_readings", a.get("oracle_readings"))
    if oc is None and a.get("raw_captures"):
        cls = core.classifier.classify(a["raw_captures"], readings)
        oc, conf, src = cls.outcome_class, cls.confidence, "classifier"
    return core.store.record_attempt(
        sweep_id, a.get("params", {}), oc or "no-effect", conf, verdict_source=src,
        verified=False, notes=a.get("notes", ""), raw_captures=a.get("raw_captures"),
        oracle_readings=readings, env_sample=a.get("env_sample"))
