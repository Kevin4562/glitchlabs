"""Read-only retrieval & analysis tools — all SAFE, invisible (spec §11.1).

These gather/read data without moving the UI. Cursor-paginated, server-side filtered, projected.
The three-state model (§4.4) is preserved in every result.
"""
from __future__ import annotations

import json
from typing import Annotated, Any, Literal

from pydantic import Field

from . import anns, meta
from ..render import budget, descriptor, textart
from ..render.grid import build_grid
from ..domain import stats as stats_mod
from ..domain import refinement, prediction
from .workflow import get_attempt_evidence_data


def _strict_classification(core, row: dict) -> str:
    """Translate a legacy success row without trusting its durable flag by itself."""
    if row.get("outcome_class") != "success":
        return "non_success"
    if not bool(row.get("verified")):
        return "candidate_unconfirmed"
    evidence = get_attempt_evidence_data(core, int(row["id"]), include_raw=False)
    return str(evidence.get("classification") or "candidate_unconfirmed")


def register(srv, core):
    store = core.store

    @srv.tool(name="list_campaigns", title="List campaigns and confirmation totals",
              description="Enumerate campaigns with target, sweep state, attempt count, legacy "
              "success rows, unconfirmed candidates, and fully contract-validated confirmations. Defaults to "
              "the active project so unrelated targets are not mixed.",
              annotations=anns(read_only=True), meta=meta("SAFE", "invisible", 800))
    def list_campaigns(
        filter: Annotated[str | None, Field(description=
            "Case-insensitive text matched against campaign metadata.")] = None,
        project_id: Annotated[str | None, Field(description=
            "Project to enumerate. Omit to use the active project.")] = None,
        all_projects: Annotated[bool, Field(description=
            "Include every project. Use only for an intentional cross-project inventory.")] = False,
        cursor: Annotated[int, Field(ge=0, description="Zero-based page offset.")] = 0,
        limit: Annotated[int, Field(ge=1, le=100, description="Maximum campaigns returned.")] = 25,
    ) -> dict:
        pid = project_id or core.active.get("project_id")
        project_where = "" if all_projects or not pid else " WHERE c.project_id=?"
        project_args = () if not project_where else (pid,)
        rows = store.fetch_all(
            "SELECT c.id,c.name,c.objective,c.mode,c.created_at,c.project_id,t.model target,"
            "(SELECT COUNT(*) FROM session s WHERE s.campaign_id=c.id) sessions "
            "FROM campaign c LEFT JOIN target t ON c.target_id=t.id" + project_where +
            " ORDER BY c.created_at DESC", project_args)
        if filter:
            rows = [r for r in rows if filter.lower() in json.dumps(r).lower()]
        page = rows[cursor:cursor + limit]
        for r in page:
            att = store.fetch_one(
                "SELECT COUNT(*) n, "
                "SUM(CASE WHEN a.outcome_class='success' THEN 1 ELSE 0 END) s, "
                "SUM(CASE WHEN a.outcome_class='success' AND COALESCE(a.verified,0)=0 THEN 1 ELSE 0 END) candidates, "
                "SUM(CASE WHEN a.outcome_class='success' AND COALESCE(a.verified,0)=1 THEN 1 ELSE 0 END) confirmed "
                "FROM attempt a JOIN sweep sw ON a.sweep_id=sw.id JOIN session se "
                "ON sw.session_id=se.id WHERE se.campaign_id=?", (r["id"],))
            r["attempts"] = (att or {}).get("n", 0) or 0
            r["successes"] = (att or {}).get("s", 0) or 0
            verified_rows = store.fetch_all(
                "SELECT a.id,a.outcome_class,a.verified FROM attempt a JOIN sweep sw ON a.sweep_id=sw.id "
                "JOIN session se ON sw.session_id=se.id WHERE se.campaign_id=? "
                "AND a.outcome_class='success' AND COALESCE(a.verified,0)=1",
                (r["id"],))
            confirmed = sum(1 for row in verified_rows
                            if _strict_classification(core, row) == "fully_confirmed")
            r["verified_flag_successes"] = (att or {}).get("confirmed", 0) or 0
            r["confirmed_successes"] = confirmed
            r["candidate_successes"] = max(0, int(r["successes"]) - confirmed)
            latest = store.fetch_one(
                "SELECT sw.id,sw.name,sw.status,sw.confidence FROM sweep sw JOIN session se "
                "ON sw.session_id=se.id WHERE se.campaign_id=? ORDER BY sw.created_at DESC LIMIT 1",
                (r["id"],))
            r["latest_sweep"] = latest
        return {"campaigns": page, "total": len(rows),
                "next_cursor": (cursor + limit) if cursor + limit < len(rows) else None,
                "project_id": None if all_projects else pid, "all_projects": all_projects}

    @srv.tool(name="query_attempts", title="Query attempts without conflating candidates and hits",
              description="Filter or aggregate persisted attempts. Raw rows include verified and "
              "verdict_source and a contract-derived classification by default. confirmation='confirmed' "
              "requires get_attempt_evidence to pass the complete project-composite and raw-connection "
              "contract; the persisted verified flag alone is never sufficient.",
              annotations=anns(read_only=True),
              meta=meta("SAFE", "invisible", 2000))
    def query_attempts(
        sweep_id: Annotated[str | None, Field(description=
            "Restrict to one sweep. Omit only for an intentional broader query.")] = None,
        campaign_id: Annotated[str | None, Field(description=
            "Restrict to all sweeps in one campaign. Ignored when sweep_id is supplied.")] = None,
        outcome: Annotated[str | None, Field(description=
            "Exact outcome key such as no-effect, reset, exception, false-positive, or success.")] = None,
        confirmation: Annotated[Literal["all", "candidate", "confirmed"], Field(description=
            "For success rows: all, candidate (complete contract fails), or confirmed (complete contract passes).")] = "all",
        aggregate: Annotated[Literal["raw", "by-cell", "by-class"], Field(description=
            "raw returns attempts; by-cell and by-class split success rows by contract-derived classification.")] = "raw",
        cursor: Annotated[int, Field(ge=0, description=
            "Return rows with database id greater than this cursor; 0 starts at the beginning.")] = 0,
        limit: Annotated[int, Field(ge=1, le=500, description="Maximum raw rows returned.")] = 50,
        fields: Annotated[list[str] | None, Field(description=
            "Optional projection from documented attempt columns; id is always included for paging.")] = None,
        since_cursor: Annotated[int | None, Field(ge=0, description=
            "Deprecated alias for cursor, retained for compatibility. The larger value wins.")] = None,
        width_min: Annotated[float | None, Field(description="Inclusive requested width lower bound.")] = None,
        width_max: Annotated[float | None, Field(description="Inclusive requested width upper bound.")] = None,
        offset_min: Annotated[float | None, Field(description="Inclusive requested offset lower bound.")] = None,
        offset_max: Annotated[float | None, Field(description="Inclusive requested offset upper bound.")] = None,
    ) -> dict:
        where, args = [], []
        if sweep_id:
            where.append("sweep_id=?"); args.append(sweep_id)
        elif campaign_id:
            where.append("sweep_id IN (SELECT sw.id FROM sweep sw JOIN session se ON "
                         "sw.session_id=se.id WHERE se.campaign_id=?)")
            args.append(campaign_id)
        if outcome:
            where.append("outcome_class=?"); args.append(outcome)
        if confirmation in {"candidate", "confirmed"}:
            where.append("outcome_class='success'")
        if confirmation == "confirmed":
            # A verified flag is necessary but not sufficient; strict evidence is checked below.
            where.append("COALESCE(verified,0)=1")
        if width_min is not None:
            where.append("width>=?"); args.append(width_min)
        if width_max is not None:
            where.append("width<=?"); args.append(width_max)
        if offset_min is not None:
            where.append("offset>=?"); args.append(offset_min)
        if offset_max is not None:
            where.append("offset<=?"); args.append(offset_max)
        effective_cursor = max(cursor, since_cursor or 0)
        if effective_cursor:
            where.append("id>?"); args.append(effective_cursor)
        w = (" WHERE " + " AND ".join(where)) if where else ""
        if aggregate == "by-class":
            totals = store.fetch_all(
                f"SELECT outcome_class,COUNT(*) n FROM attempt{w} GROUP BY outcome_class", tuple(args))
            strict = store.fetch_all(
                f"SELECT id,outcome_class,COALESCE(verified,0) verified FROM attempt{w} "
                "AND outcome_class='success' AND COALESCE(verified,0)=1"
                if w else
                "SELECT id,outcome_class,COALESCE(verified,0) verified FROM attempt "
                "WHERE outcome_class='success' AND COALESCE(verified,0)=1",
                tuple(args))
            confirmed_n = sum(1 for row in strict
                              if _strict_classification(core, row) == "fully_confirmed")
            rows = []
            for row in totals:
                if row["outcome_class"] != "success":
                    if confirmation == "all":
                        rows.append({**row, "classification": "non_success"})
                    continue
                candidate_n = max(0, int(row["n"]) - confirmed_n)
                if confirmation in {"all", "candidate"} and candidate_n:
                    rows.append({"outcome_class": "success",
                                 "classification": "candidate_unconfirmed", "n": candidate_n})
                if confirmation in {"all", "confirmed"} and confirmed_n:
                    rows.append({"outcome_class": "success",
                                 "classification": "fully_confirmed", "n": confirmed_n})
            return {"aggregate": "by-class", "rows": rows,
                    "result_semantics": "fully_confirmed requires the complete persisted evidence contract"}
        if aggregate == "by-cell":
            totals = store.fetch_all(
                f"SELECT width,offset,outcome_class,COUNT(*) n FROM attempt{w} "
                "GROUP BY width,offset,outcome_class ORDER BY width,offset", tuple(args))
            strict_sql = (f"SELECT id,width,offset,outcome_class,COALESCE(verified,0) verified "
                          f"FROM attempt{w} " + ("AND " if w else " WHERE ") +
                          "outcome_class='success' AND COALESCE(verified,0)=1")
            confirmed_cells: dict[tuple[Any, Any], int] = {}
            for row in store.fetch_all(strict_sql, tuple(args)):
                if _strict_classification(core, row) == "fully_confirmed":
                    key = (row.get("width"), row.get("offset"))
                    confirmed_cells[key] = confirmed_cells.get(key, 0) + 1
            rows = []
            for row in totals:
                if row["outcome_class"] != "success":
                    if confirmation == "all":
                        rows.append({**row, "classification": "non_success"})
                    continue
                confirmed_n = confirmed_cells.get((row.get("width"), row.get("offset")), 0)
                candidate_n = max(0, int(row["n"]) - confirmed_n)
                if confirmation in {"all", "candidate"} and candidate_n:
                    rows.append({**row, "classification": "candidate_unconfirmed", "n": candidate_n})
                if confirmation in {"all", "confirmed"} and confirmed_n:
                    rows.append({**row, "classification": "fully_confirmed", "n": confirmed_n})
            return {"aggregate": "by-cell", "rows": rows[:500],
                    "truncated": len(rows) > 500,
                    "result_semantics": "fully_confirmed requires the complete persisted evidence contract"}
        default_cols = ("id,seq,ts,width,offset,voltage,repeat,outcome_class,outcome_confidence,"
                        "duration_ms,verified,verdict_source,notes")
        requested = default_cols.split(",")
        if fields:
            allowed = {"id", "seq", "ts", "width", "offset", "voltage", "repeat", "outcome_class",
                       "outcome_confidence", "duration_ms", "verified", "verdict_source", "notes"}
            requested = [f for f in fields if f in allowed]
            if "id" not in requested:
                requested.insert(0, "id")
        internal = list(dict.fromkeys(requested + ["id", "outcome_class", "verified"]))
        batch_size = max(200, limit * 4)
        scan_cursor = effective_cursor
        matched: list[dict] = []
        # Contract filtering can reject a row whose verified bit was stale. Scan until a complete
        # page of actual classifications is found instead of returning a misleading short page.
        while len(matched) < limit + 1:
            scan_where = [part for part in where if part != "id>?"]
            scan_args = list(args[:-1] if effective_cursor else args)
            scan_where.append("id>?")
            scan_args.append(scan_cursor)
            scan_w = " WHERE " + " AND ".join(scan_where)
            batch = store.fetch_all(
                f"SELECT {','.join(internal)} FROM attempt{scan_w} ORDER BY id LIMIT ?",
                tuple(scan_args) + (batch_size,))
            if not batch:
                break
            scan_cursor = int(batch[-1]["id"])
            for row in batch:
                classification = _strict_classification(core, row)
                if confirmation == "candidate" and classification == "fully_confirmed":
                    continue
                if confirmation == "confirmed" and classification != "fully_confirmed":
                    continue
                projected = {key: row.get(key) for key in requested}
                projected["classification"] = classification
                matched.append(projected)
                if len(matched) >= limit + 1:
                    break
            if len(batch) < batch_size:
                break
        page = matched[:limit]
        nxt = page[-1]["id"] if len(matched) > limit and page else None
        return {"aggregate": "raw", "attempts": page, "next_cursor": nxt,
                "result_semantics": "fully_confirmed requires verified=1 plus the complete persisted evidence contract"}

    @srv.tool(name="get_parameter_map", description="The heatmap tool. view ∈ {categorical, "
              "success_rate, spatial}; detail ∈ {summary, textmap, cells, image}. summary is default "
              "(~150-400 tokens). Auto-degrades beyond max_tokens with a full_data_uri.",
              annotations=anns(read_only=True), meta=meta("SAFE", "invisible", 1500))
    def get_parameter_map(
        sweep_id: Annotated[str, Field(description="Sweep whose persisted attempts form the map.")],
        view: Annotated[Literal["categorical", "success_rate", "spatial"], Field(description=
            "Map statistic. success_rate is the legacy success-class candidate rate.")] = "success_rate",
        detail: Annotated[Literal["summary", "textmap", "cells", "image"], Field(description=
            "Response representation; summary is compact and cells is the exact numeric grid.")] = "summary",
        x_axis: Annotated[str, Field(description="Attempt parameter on the horizontal axis.")] = "width",
        y_axis: Annotated[str, Field(description="Attempt parameter on the vertical axis.")] = "offset",
        max_cols: Annotated[int, Field(ge=1, le=120, description=
            "Maximum text-map columns before downsampling.")] = 24,
        max_rows: Annotated[int, Field(ge=1, le=120, description=
            "Maximum text-map rows before downsampling.")] = 24,
    ) -> dict:
        if detail == "summary":
            summ = descriptor.build_summary(store, sweep_id, view, x_axis, y_axis)
            b = budget.enforce("summary", summ, summ.get("render_uri"))
            summ["estimated_tokens"] = b["tokens"]
            summ["downsampled"] = b["downsampled"]
            return summ
        if detail == "textmap":
            g = build_grid(store, sweep_id, x_axis, y_axis)
            if view == "categorical":
                text = textart.categorical_map(g, max_cols, max_rows)
            else:
                text = textart.success_rate_map(g, max_cols, max_rows)
            text += "\n" + textart.marginals_sparkline(g)
            trimmed, dropped = budget.truncate_text(text, "textmap")
            return {"view": view, "detail": "textmap", "textmap": trimmed, "downsampled": dropped,
                    "full_data_uri": f"glitchlab://sweep/{sweep_id}/map.png?view={view}",
                    "estimated_tokens": budget.estimate_tokens(trimmed)}
        if detail == "cells":
            rows = store.fetch_all(
                "SELECT width,offset,outcome_class,COUNT(*) n FROM attempt WHERE sweep_id=? "
                "GROUP BY width,offset,outcome_class ORDER BY n DESC LIMIT 200", (sweep_id,))
            return {"view": view, "detail": "cells", "cells": rows,
                    "full_data_uri": f"glitchlab://sweep/{sweep_id}/map.csv"}
        # image
        return {"view": view, "detail": "image",
                "render_uri": f"glitchlab://sweep/{sweep_id}/map.png?view={view}",
                "alt_text": f"Parameter-space {view} heatmap for sweep {sweep_id}"}

    @srv.tool(name="analyze_clusters", description="Success clusters, bounding ranges, and a "
              "suggested narrowed child-sweep box (spec §6.3, §11.1).",
              annotations=anns(read_only=True), meta=meta("SAFE", "invisible", 800))
    def analyze_clusters(
        sweep_id: Annotated[str, Field(description="Sweep to cluster without changing its verdicts.")],
        min_trials: Annotated[int, Field(ge=1, description=
            "Minimum trials required before a cell can influence a suggested refinement.")] = 1,
        x_axis: Annotated[str, Field(description="Horizontal parameter name.")] = "width",
        y_axis: Annotated[str, Field(description="Vertical parameter name.")] = "offset",
    ) -> dict:
        summ = descriptor.build_summary(store, sweep_id, "success_rate", x_axis, y_axis)
        refine = refinement.suggest_refine_box(store, sweep_id, x_axis, y_axis)
        return {"clusters": summ["clusters"], "suggested_refine": refine, "hotspot": summ["hotspot"]}

    @srv.tool(name="get_statistics", description="Consolidated stats engine. metric ∈ {rolling_rate,"
              " timing_histogram, time_between_success, funnel, throughput, cumulative, "
              "per_unit_variance, drift, confusion_matrix}.", annotations=anns(read_only=True),
              meta=meta("SAFE", "invisible", 1200))
    def get_statistics(
        metric: Annotated[str, Field(description=
            "Metric key listed in this tool description.")],
        sweep_id: Annotated[str | None, Field(description=
            "Optional sweep scope for within-sweep metrics.")] = None,
        session_id: Annotated[str | None, Field(description=
            "Optional session scope when no sweep is supplied.")] = None,
        target_model: Annotated[str | None, Field(description=
            "Target model required by cross-unit variance and drift metrics.")] = None,
        detail: Annotated[Literal["summary", "full"], Field(description=
            "Reserved output-detail preference; metrics currently return their compact structured result.")] = "summary",
    ) -> dict:
        fn = stats_mod.METRICS.get(metric)
        if fn is None:
            return {"error": f"unknown metric {metric}", "available": list(stats_mod.METRICS)}
        if metric in ("per_unit_variance", "drift"):
            return fn(store, target_model or core.rig.target_model)
        return fn(store, sweep_id, session_id)

    @srv.tool(name="bootstrap_confidence", description="Resample a logged trial pool to compute "
              "confidence-vs-N (spec §7.2/§11.1).", annotations=anns(read_only=True),
              meta=meta("SAFE", "invisible", 800))
    def bootstrap_confidence(
        sweep_id: Annotated[str, Field(description="Sweep providing the persisted trial pool.")],
        n_settings: Annotated[int, Field(ge=1, le=10000, description=
            "Largest resampled trial count represented in the confidence curve.")] = 50,
        iterations: Annotated[int, Field(ge=10, le=10000, description=
            "Requested bootstrap resamples; the implementation applies its documented cap.")] = 500,
    ) -> dict:
        return stats_mod.bootstrap_confidence(store, sweep_id, n_settings, iterations)

    @srv.tool(name="predict_parameters", description="Warm-start + success-probability model; "
              "suggested starting bbox + predicted hotspot + provenance/transfer flag (spec §8).",
              annotations=anns(read_only=True), meta=meta("SAFE", "invisible", 800))
    def predict_parameters(
        target_model: Annotated[str, Field(description="Exact target model for prior lookup.")],
        injection_type: Annotated[str, Field(description="Injection family to model.")] = "voltage",
        context_sweep_id: Annotated[str | None, Field(description=
            "Optional current sweep whose observations condition the prediction.")] = None,
    ) -> dict:
        return prediction.predict_parameters(store, target_model, injection_type, context_sweep_id)

    @srv.tool(name="get_known_good", title="Read stored reproduction profiles",
              description="Cross-engagement parameter profile lookup. Entries may be exact tuples or "
              "bounded ranges; treat one as confirmed only when provenance links to an attempt that "
              "get_attempt_evidence still classifies fully_confirmed.",
              annotations=anns(read_only=True), meta=meta("SAFE", "invisible", 600))
    def get_known_good(
        target_model: Annotated[str, Field(description="Exact target model to match.")],
        injection_type: Annotated[str | None, Field(description=
            "Optional injection family filter, for example voltage.")] = None,
    ) -> dict:
        entries = store.get_known_good(target_model, injection_type)
        for entry in entries:
            provenance = entry.get("provenance") or {}
            attempt_id = provenance.get("confirmed_attempt_id")
            evidence = (get_attempt_evidence_data(core, int(attempt_id), include_raw=False)
                        if attempt_id else {})
            entry["status"] = ("fully_confirmed" if evidence.get("fully_confirmed")
                               else "unverified_provenance")
            entry["confirmed_attempt_id"] = attempt_id
        return {"target_model": target_model, "entries": entries,
                "result_semantics": "only status=fully_confirmed is an actionable reproduction profile"}

    @srv.tool(name="get_raw_capture", description="Verbatim evidence for an attempt; returns the raw "
              "payload, or a resource link if large (spec §11.1).", annotations=anns(read_only=True),
              meta=meta("SAFE", "invisible", 1000))
    def get_raw_capture(
        attempt_id: Annotated[int, Field(ge=1, description="Attempt owning the immutable capture.")],
        channel: Annotated[str | None, Field(description=
            "Optional exact capture-channel filter, such as oracle_evidence.")] = None,
        head: Annotated[int | None, Field(ge=0, description=
            "Return at most this many leading characters per payload.")] = None,
        tail: Annotated[int | None, Field(ge=0, description=
            "Return at most this many trailing characters per payload.")] = None,
    ) -> dict:
        where = "attempt_id=?"
        args: tuple = (attempt_id,)
        if channel:
            where += " AND channel=?"; args = (attempt_id, channel)
        rows = store.fetch_all(f"SELECT id,channel,payload,encoding,preamble FROM raw_capture "
                               f"WHERE {where}", args)
        out = []
        for r in rows:
            payload = r["payload"]
            if isinstance(payload, (bytes, bytearray)):
                encoding = str(r.get("encoding") or "utf-8").lower()
                if encoding in {"json", "text", "plain"}:
                    encoding = "utf-8"
                try:
                    payload = bytes(payload).decode(encoding, "replace")
                except LookupError:
                    payload = bytes(payload).decode("utf-8", "replace")
            if head:
                payload = payload[:head]
            elif tail:
                payload = payload[-tail:]
            large = len(payload) > 4000
            out.append({"id": r["id"], "channel": r["channel"],
                        "payload": payload[:4000] if large else payload,
                        "truncated": large,
                        "resource_uri": f"glitchlab://attempt/{attempt_id}/raw/{r['channel']}"
                        if large else None})
        return {"attempt_id": attempt_id, "captures": out}

    @srv.tool(name="run_query", description="Sandboxed READ-ONLY DuckDB SQL over the live store + "
              "lake; rejects any non-SELECT (spec §11.1). Tables under schema 'live.*'.",
              annotations=anns(read_only=True), meta=meta("SAFE", "invisible", 2000))
    def run_query(
        sql: Annotated[str, Field(min_length=1, description=
            "One read-only SELECT statement over documented live/lake tables.")],
        max_rows: Annotated[int, Field(ge=1, le=5000, description=
            "Hard maximum rows returned even if the SELECT has no LIMIT.")] = 200,
    ) -> dict:
        try:
            cols, rows = core.analytics.query(sql, max_rows)
            return {"columns": cols, "rows": rows, "row_count": len(rows)}
        except ValueError as e:
            return {"error": str(e), "rejected": True}
        except Exception as e:
            return {"error": str(e)}

    @srv.tool(name="describe_schema", description="Self-describing data dictionary + capability "
              "manifest: tables, legacy outcome taxonomy, candidate-versus-confirmed semantics, "
              "enabled capabilities, active project/connector profile, and enforced limits.",
              annotations=anns(read_only=True), meta=meta("SAFE", "invisible", 1500))
    def describe_schema() -> dict:
        return {
            "tables": ["target", "unit", "campaign", "session", "sweep", "attempt",
                       "outcome_class", "raw_capture", "oracle_reading", "fault_detail",
                       "env_sample", "annotation", "parameter_profile", "instrument",
                       "audit_record"],
            "outcome_taxonomy": [c["key"] for c in store.outcome_classes()],
            "measurement_states": ["not_attempted", "functional_unquantified", "quantified"],
            "confirmation_states": {
                "candidate_unconfirmed": "outcome_class=success and verified=0",
                "fully_confirmed": "outcome_class=success, verified=1, complete persisted project-connector contract",
            },
            "canonical_evidence_tool": "get_attempt_evidence",
            "capability_manifest": core.capability_manifest(),
        }
