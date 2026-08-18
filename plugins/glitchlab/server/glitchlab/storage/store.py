"""Live store: crash-safe SQLite (WAL) ingestion + per-sweep CSV mirror + rollups (spec §5.1/§5.3).

The sweep engine is the single writer; the viewer and MCP server are concurrent readers (WAL).
Every write emits an event on the bus so subscriptions and the viewer stay live (§6.2, §12), and
appends to a per-sweep CSV (§5.1). Verdicts are versioned, never overwritten (§4.3).
"""
from __future__ import annotations

import csv
import hashlib
import json
import sqlite3
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from .. import config
from ..bus import EventBus
from . import taxonomy
from .schema import DDL


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _uid() -> str:
    return uuid.uuid4().hex[:12]


def _j(v: Any) -> str | None:
    return None if v is None else json.dumps(v, separators=(",", ":"), default=str)


class Store:
    """Single-writer WAL store. Thread-safe via a write lock; reads use short-lived connections."""

    def __init__(self, db_path: Path | None = None, bus: EventBus | None = None) -> None:
        self.db_path = Path(db_path or (config.DATA_DIR / "glitchlab.db"))
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.bus = bus or EventBus()
        self._wlock = threading.RLock()
        self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(DDL)
        self._conn.commit()
        self._csv_writers: dict[str, Path] = {}
        self._migrate()
        self.default_project_id = self._seed_default_project()
        self._seed_global_taxonomy()

    # -- migration / projects --------------------------------------------------------
    def _migrate(self) -> None:
        with self._wlock:
            cols = [r[1] for r in self._conn.execute("PRAGMA table_info(campaign)").fetchall()]
            if "project_id" not in cols:
                self._conn.execute("ALTER TABLE campaign ADD COLUMN project_id TEXT")
            oracle_cols = [
                r[1] for r in self._conn.execute("PRAGMA table_info(oracle_reading)").fetchall()
            ]
            if "detail" not in oracle_cols:
                self._conn.execute("ALTER TABLE oracle_reading ADD COLUMN detail TEXT")
            self._conn.commit()

    def _seed_default_project(self) -> str:
        with self._wlock:
            row = self._conn.execute("SELECT id FROM project ORDER BY created_at LIMIT 1").fetchone()
            pid = row["id"] if row else _uid()
            if not row:
                self._conn.execute("INSERT INTO project(id,name,notes,created_at) VALUES(?,?,?,?)",
                                   (pid, "Default project", "", _now()))
            # orphan campaigns join the default project
            self._conn.execute("UPDATE campaign SET project_id=? WHERE project_id IS NULL", (pid,))
            self._conn.commit()
            return pid

    def create_project(self, name: str, notes: str = "") -> str:
        pid = _uid()
        with self._wlock:
            self._conn.execute("INSERT INTO project(id,name,notes,created_at) VALUES(?,?,?,?)",
                               (pid, name, notes, _now()))
            self._conn.commit()
        self.bus.publish("project_created", {"project_id": pid, "name": name})
        return pid

    def get_or_create_project(self, name: str, notes: str = "") -> str:
        """Get the project with this exact name, or create it. Used so every target model gets
        its OWN project (accurate per-target attempt counts) instead of a shared Default project."""
        row = self.fetch_one("SELECT id FROM project WHERE name=? ORDER BY created_at LIMIT 1", (name,))
        if row and row.get("id"):
            return row["id"]
        return self.create_project(name, notes)

    def list_projects(self) -> list[dict]:
        return self.fetch_all("SELECT * FROM project ORDER BY created_at")

    def set_campaign_project(self, campaign_id: str, project_id: str) -> None:
        with self._wlock:
            self._conn.execute("UPDATE campaign SET project_id=? WHERE id=?",
                               (project_id, campaign_id))
            self._conn.commit()

    def projects_with_totals(self) -> list[dict]:
        rows = self.fetch_all(
            "SELECT p.id, p.name, p.notes, p.created_at, "
            "(SELECT COUNT(*) FROM campaign c WHERE c.project_id=p.id) campaigns FROM project p "
            "ORDER BY p.created_at")
        for r in rows:
            agg = self.fetch_one(
                "SELECT COUNT(a.id) n, SUM(CASE WHEN a.outcome_class='success' THEN 1 ELSE 0 END) s "
                "FROM attempt a JOIN sweep sw ON a.sweep_id=sw.id JOIN session se "
                "ON sw.session_id=se.id JOIN campaign c ON se.campaign_id=c.id WHERE c.project_id=?",
                (r["id"],))
            r["attempts"] = (agg or {}).get("n", 0) or 0
            r["successes"] = (agg or {}).get("s", 0) or 0
        return rows

    # -- connections -----------------------------------------------------------------
    def _reader(self) -> sqlite3.Connection:
        c = sqlite3.connect(self.db_path, check_same_thread=False)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA query_only=ON")
        return c

    def close(self) -> None:
        try:
            self._conn.close()
        except Exception:
            pass

    # -- taxonomy --------------------------------------------------------------------
    def _seed_global_taxonomy(self) -> None:
        with self._wlock:
            cur = self._conn.execute("SELECT COUNT(*) FROM outcome_class WHERE campaign_id IS NULL")
            if cur.fetchone()[0] == 0:
                for c in taxonomy.DEFAULT_TAXONOMY:
                    self._conn.execute(
                        "INSERT OR IGNORE INTO outcome_class"
                        "(campaign_id,key,label,color,marker,glyph,is_success,is_collateral,sort_order)"
                        " VALUES (NULL,?,?,?,?,?,?,?,?)",
                        (c.key, c.label, c.color, c.marker, c.glyph,
                         int(c.is_success), int(c.is_collateral), c.sort_order),
                    )
                self._conn.commit()

    def outcome_classes(self, campaign_id: str | None = None) -> list[dict]:
        c = self._reader()
        rows = c.execute(
            "SELECT * FROM outcome_class WHERE campaign_id IS NULL OR campaign_id=? ORDER BY sort_order",
            (campaign_id,),
        ).fetchall()
        c.close()
        # dedupe by key preferring campaign-specific
        out: dict[str, dict] = {}
        for r in rows:
            out[r["key"]] = dict(r)
        return sorted(out.values(), key=lambda d: d["sort_order"])

    def add_outcome_class(self, campaign_id: str | None, key: str, label: str, color: str,
                          marker: str = "o", glyph: str = "?", is_success: bool = False,
                          is_collateral: bool = False, sort_order: int = 10) -> None:
        with self._wlock:
            self._conn.execute(
                "INSERT OR IGNORE INTO outcome_class"
                "(campaign_id,key,label,color,marker,glyph,is_success,is_collateral,sort_order)"
                " VALUES (?,?,?,?,?,?,?,?,?)",
                (campaign_id, key, label, color, marker, glyph, int(is_success),
                 int(is_collateral), sort_order),
            )
            self._conn.commit()

    # -- entity creation -------------------------------------------------------------
    def create_target(self, vendor: str, model: str, package: str = "", revision: str = "",
                       injection_types: list[str] | None = None, notes: str = "") -> str:
        tid = _uid()
        with self._wlock:
            self._conn.execute(
                "INSERT INTO target(id,vendor,model,package,revision,injection_types,notes)"
                " VALUES (?,?,?,?,?,?,?)",
                (tid, vendor, model, package, revision, _j(injection_types or []), notes),
            )
            self._conn.commit()
        return tid

    def get_or_create_target(self, model: str, vendor: str = "", package: str = "",
                             injection_types: list[str] | None = None) -> str:
        c = self._reader()
        row = c.execute("SELECT id FROM target WHERE model=?", (model,)).fetchone()
        c.close()
        if row:
            return row["id"]
        return self.create_target(vendor, model, package, injection_types=injection_types)

    def create_unit(self, target_id: str, serial: str, batch: str = "") -> str:
        uid = _uid()
        now = _now()
        with self._wlock:
            self._conn.execute(
                "INSERT INTO unit(id,target_id,serial,batch,first_seen,last_seen)"
                " VALUES (?,?,?,?,?,?)", (uid, target_id, serial, batch, now, now))
            self._conn.commit()
        return uid

    def create_campaign(self, name: str, objective: str, target_id: str, mode: str = "full",
                        project_id: str | None = None) -> str:
        cid = _uid()
        project_id = project_id or getattr(self, "default_project_id", None)
        with self._wlock:
            self._conn.execute(
                "INSERT INTO campaign(id,name,objective,target_id,created_at,mode,project_id)"
                " VALUES (?,?,?,?,?,?,?)", (cid, name, objective, target_id, _now(), mode, project_id))
            self._conn.commit()
        self.bus.publish("campaign_opened", {"campaign_id": cid, "name": name, "project_id": project_id})
        return cid

    def create_session(self, campaign_id: str, unit_id: str | None = None, operator: str = "",
                       rig_config: dict | None = None) -> str:
        sid = _uid()
        with self._wlock:
            self._conn.execute(
                "INSERT INTO session(id,campaign_id,unit_id,started_at,operator,rig_config,"
                "resumable_state,status) VALUES (?,?,?,?,?,?,?,?)",
                (sid, campaign_id, unit_id, _now(), operator, _j(rig_config or {}), _j({}), "active"))
            self._conn.commit()
        self.bus.publish("session_opened", {"session_id": sid, "campaign_id": campaign_id})
        return sid

    def create_sweep(self, session_id: str, kind: str = "grid", param_spec: dict | None = None,
                     parent_sweep_id: str | None = None, name: str = "",
                     axis_flags: dict | None = None) -> str:
        swid = _uid()
        with self._wlock:
            self._conn.execute(
                "INSERT INTO sweep(id,session_id,parent_sweep_id,kind,param_spec,axis_flags,"
                "optimizer_state,confidence,measurement_state,name,created_at,status)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (swid, session_id, parent_sweep_id, kind, _j(param_spec or {}), _j(axis_flags or {}),
                 _j({}), "provisional", "not_attempted", name or f"sweep-{swid}", _now(), "defined"))
            self._conn.commit()
        self.bus.publish("sweep_defined", {"sweep_id": swid, "session_id": session_id, "kind": kind})
        return swid

    def set_sweep_status(self, sweep_id: str, status: str) -> None:
        with self._wlock:
            self._conn.execute("UPDATE sweep SET status=? WHERE id=?", (status, sweep_id))
            self._conn.commit()
        self.bus.publish("sweep_status", {"sweep_id": sweep_id, "status": status})

    def set_session_status_for_sweep(self, sweep_id: str, status: str) -> None:
        """Mirror an epoch's terminal/resumable state onto its owning session."""
        ended = _now() if status in {"done", "aborted"} else None
        with self._wlock:
            self._conn.execute(
                "UPDATE session SET status=?,ended_at=? WHERE id=("
                "SELECT session_id FROM sweep WHERE id=?)",
                (status, ended, sweep_id),
            )
            self._conn.commit()
        self.bus.publish("session_status", {
            "sweep_id": sweep_id, "status": status, "ended_at": ended
        })

    def set_sweep_measurement_state(self, sweep_id: str, state: str) -> None:
        with self._wlock:
            self._conn.execute("UPDATE sweep SET measurement_state=? WHERE id=?", (state, sweep_id))
            self._conn.commit()

    def set_sweep_confidence(self, sweep_id: str, confidence: str) -> None:
        with self._wlock:
            self._conn.execute("UPDATE sweep SET confidence=? WHERE id=?", (confidence, sweep_id))
            self._conn.commit()

    def set_resumable_state(self, session_id: str, state: dict) -> None:
        with self._wlock:
            self._conn.execute("UPDATE session SET resumable_state=? WHERE id=?",
                               (_j(state), session_id))
            self._conn.commit()

    # -- attempts (hot path) ---------------------------------------------------------
    def record_attempt(self, sweep_id: str, params: dict, outcome_class: str,
                       outcome_confidence: float = 1.0, verdict_source: str = "classifier",
                       stage_reached: int = 0, duration_ms: float = 0.0, verified: bool = False,
                       notes: str = "", raw_captures: list[dict] | None = None,
                       oracle_readings: list[dict] | None = None, env_sample: dict | None = None,
                       fault_detail: dict | None = None, seq: int | None = None) -> int:
        """Insert one attempt with its evidence. Emits `attempt_recorded`. Returns attempt id."""
        with self._wlock:
            if seq is None:
                r = self._conn.execute("SELECT COALESCE(MAX(seq),0)+1 FROM attempt WHERE sweep_id=?",
                                       (sweep_id,)).fetchone()
                seq = r[0]
            cur = self._conn.execute(
                "INSERT INTO attempt(sweep_id,seq,ts,offset,width,voltage,x,y,z,repeat,params,"
                "outcome_class,outcome_confidence,verdict_version,verdict_source,stage_reached,"
                "duration_ms,verified,notes) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (sweep_id, seq, _now(),
                 params.get("offset"), params.get("width"), params.get("voltage"),
                 params.get("x"), params.get("y"), params.get("z"), params.get("repeat"),
                 _j(params), outcome_class, outcome_confidence, 1, verdict_source,
                 stage_reached, duration_ms, int(verified), notes))
            aid = cur.lastrowid
            self._conn.execute(
                "INSERT INTO attempt_verdict(attempt_id,verdict_version,outcome_class,"
                "outcome_confidence,verdict_source,ts) VALUES (?,?,?,?,?,?)",
                (aid, 1, outcome_class, outcome_confidence, verdict_source, _now()))
            for rc in (raw_captures or []):
                payload = rc.get("payload", b"")
                if isinstance(payload, str):
                    payload = payload.encode("utf-8", "replace")
                self._conn.execute(
                    "INSERT INTO raw_capture(attempt_id,channel,payload,encoding,preamble,"
                    "is_sidecar,sidecar_path) VALUES (?,?,?,?,?,?,?)",
                    (aid, rc.get("channel", "stdout"), payload, rc.get("encoding", "utf-8"),
                     _j(rc.get("preamble")), 0, None))
            for orr in (oracle_readings or []):
                self._conn.execute(
                    "INSERT INTO oracle_reading(attempt_id,oracle_name,verdict,latency_ms,detail)"
                    " VALUES (?,?,?,?,?)", (aid, orr.get("oracle_name", "oracle"),
                                            orr.get("verdict", ""),
                                            orr.get("latency_ms", 0.0),
                                            _j(orr.get("detail"))))
            if env_sample:
                self._conn.execute(
                    "INSERT INTO env_sample(attempt_id,ambient_temp_c,board_temp_c,"
                    "concurrent_bus_activity,aux_telemetry,scope_measurements)"
                    " VALUES (?,?,?,?,?,?)",
                    (aid, env_sample.get("ambient_temp_c"), env_sample.get("board_temp_c"),
                     env_sample.get("concurrent_bus_activity"), _j(env_sample.get("aux_telemetry")),
                     _j(env_sample.get("scope_measurements"))))
            if fault_detail:
                self._conn.execute(
                    "INSERT INTO fault_detail(attempt_id,corrupted_bitmask,affected_round,"
                    "affected_op,instruction_type,fault_model) VALUES (?,?,?,?,?,?)",
                    (aid, fault_detail.get("corrupted_bitmask"), fault_detail.get("affected_round"),
                     fault_detail.get("affected_op"), fault_detail.get("instruction_type"),
                     fault_detail.get("fault_model")))
            # incremental rollup
            w, off = params.get("width"), params.get("offset")
            if w is not None and off is not None:
                self._conn.execute(
                    "INSERT INTO cell_rollup(sweep_id,width,offset,outcome_class,n) VALUES(?,?,?,?,1)"
                    " ON CONFLICT(sweep_id,width,offset,outcome_class) DO UPDATE SET n=n+1",
                    (sweep_id, w, off, outcome_class))
            self._conn.commit()
        self._csv_append(
            aid, sweep_id, seq, params, outcome_class, outcome_confidence,
            verified, notes, env_sample, oracle_readings
        )
        _sm = (env_sample or {}).get("scope_measurements") or {}
        _aux = (env_sample or {}).get("aux_telemetry") or {}
        _effective = _aux.get("effective_settings") or {}
        _composite_detail = {}
        for _reading in oracle_readings or []:
            _detail = (_reading or {}).get("detail") or {}
            if _detail.get("schema_version") == "glitchlab.project-oracle/v2":
                _composite_detail = _detail
                break
        self.bus.publish("attempt_recorded", {
            "attempt_id": aid, "sweep_id": sweep_id, "seq": seq, "outcome": outcome_class,
            "params": {k: params.get(k) for k in
                       ("pulse_cycles", "width", "ext_offset", "offset", "mosfet",
                        "voltage", "repeat")},
            "confidence": outcome_confidence, "verified": bool(verified),
            "attempt_valid": _aux.get("attempt_valid") is True,
            "infrastructure_failure": _aux.get("infrastructure_failure") is True,
            "effective_settings": _effective,
            "candidate_dir": _composite_detail.get("candidate_dir"),
            "required_checks": _composite_detail.get("required_checks") or {},
            "oracle_summary": {
                "confirmed": _composite_detail.get("confirmed"),
                "evidence_complete": _composite_detail.get("evidence_complete"),
                "preserve_target": _composite_detail.get("preserve_target"),
                "preserve_reason": _composite_detail.get("preserve_reason"),
                "underlying_failure_stage": (
                    ((_composite_detail.get("underlying_oracle") or {})
                     .get("detail") or {}).get("failure_stage")
                ),
            },
            "duration_ms": duration_ms,
            "dip_min_V": _sm.get("dip_min_V"), "dip_depth_V": _sm.get("dip_depth_V"),
            # Decimated per-attempt scope traces (~120 pts each) let the UI compare
            # the injection signal with a connector-selected observation signal.
            "wave": _sm.get("wave"), "pin_wave": _sm.get("pin_wave"),
            "pin_dip_min_V": _sm.get("pin_dip_min_V"), "pin_dip_depth_V": _sm.get("pin_dip_depth_V")})
        return aid

    def record_batch(self, sweep_id: str, attempts: Iterable[dict]) -> list[int]:
        return [self.record_attempt(sweep_id=sweep_id, **a) for a in attempts]

    def reclassify(self, attempt_id: int, outcome_class: str, confidence: float = 1.0,
                   verdict_source: str = "manual") -> int:
        """Create a NEW verdict version; the original is retained (§4.3)."""
        with self._wlock:
            row = self._conn.execute("SELECT verdict_version,sweep_id,width,offset,outcome_class "
                                     "FROM attempt WHERE id=?", (attempt_id,)).fetchone()
            if not row:
                raise KeyError(f"attempt {attempt_id} not found")
            new_v = (row["verdict_version"] or 1) + 1
            old_outcome = row["outcome_class"]
            self._conn.execute(
                "UPDATE attempt SET outcome_class=?,outcome_confidence=?,verdict_version=?,"
                "verdict_source=? WHERE id=?",
                (outcome_class, confidence, new_v, verdict_source, attempt_id))
            self._conn.execute(
                "INSERT INTO attempt_verdict(attempt_id,verdict_version,outcome_class,"
                "outcome_confidence,verdict_source,ts) VALUES (?,?,?,?,?,?)",
                (attempt_id, new_v, outcome_class, confidence, verdict_source, _now()))
            # fix rollup
            w, off = row["width"], row["offset"]
            if w is not None and off is not None:
                self._conn.execute("UPDATE cell_rollup SET n=MAX(n-1,0) WHERE sweep_id=? AND width=? "
                                   "AND offset=? AND outcome_class=?",
                                   (row["sweep_id"], w, off, old_outcome))
                self._conn.execute(
                    "INSERT INTO cell_rollup(sweep_id,width,offset,outcome_class,n) VALUES(?,?,?,?,1)"
                    " ON CONFLICT(sweep_id,width,offset,outcome_class) DO UPDATE SET n=n+1",
                    (row["sweep_id"], w, off, outcome_class))
            self._conn.commit()
        self.bus.publish("attempt_reclassified", {"attempt_id": attempt_id,
                         "outcome": outcome_class, "version": new_v})
        return new_v

    def verdict_history(self, attempt_id: int) -> list[dict]:
        c = self._reader()
        rows = c.execute("SELECT verdict_version,outcome_class,outcome_confidence,verdict_source,ts "
                         "FROM attempt_verdict WHERE attempt_id=? ORDER BY verdict_version",
                         (attempt_id,)).fetchall()
        c.close()
        return [dict(r) for r in rows]

    # -- annotations & known-good ----------------------------------------------------
    def annotate(self, sweep_id: str, region: dict, text: str, flag: str | None = None,
                 author: str = "agent") -> int:
        with self._wlock:
            cur = self._conn.execute(
                "INSERT INTO annotation(sweep_id,region,text,flag,author,created_at)"
                " VALUES (?,?,?,?,?,?)", (sweep_id, _j(region), text, flag, author, _now()))
            self._conn.commit()
        self.bus.publish("annotation_added", {"sweep_id": sweep_id, "flag": flag, "text": text})
        return cur.lastrowid

    def save_known_good(self, target_model: str, injection_type: str, known_good: dict,
                        provenance: dict | None = None) -> int:
        with self._wlock:
            cur = self._conn.execute(
                "INSERT INTO parameter_profile(target_model,injection_type,known_good,provenance,"
                "updated_at) VALUES (?,?,?,?,?)",
                (target_model, injection_type, _j(known_good), _j(provenance or {}), _now()))
            self._conn.commit()
        return cur.lastrowid

    def get_known_good(self, target_model: str, injection_type: str | None = None) -> list[dict]:
        c = self._reader()
        if injection_type:
            rows = c.execute("SELECT * FROM parameter_profile WHERE target_model=? AND "
                             "injection_type=? ORDER BY updated_at DESC",
                             (target_model, injection_type)).fetchall()
        else:
            rows = c.execute("SELECT * FROM parameter_profile WHERE target_model=? ORDER BY "
                             "updated_at DESC", (target_model,)).fetchall()
        c.close()
        out = []
        for r in rows:
            d = dict(r)
            d["known_good"] = json.loads(d["known_good"]) if d["known_good"] else {}
            d["provenance"] = json.loads(d["provenance"]) if d["provenance"] else {}
            out.append(d)
        return out

    # -- instrument ------------------------------------------------------------------
    def upsert_instrument(self, kind: str, idn: str, model: str, serial: str, firmware: str,
                          resource_string: str, capabilities: dict, source_syntax: dict,
                          safety_limits: dict, iid: str | None = None) -> str:
        iid = iid or _uid()
        with self._wlock:
            self._conn.execute(
                "INSERT INTO instrument(id,kind,idn,model,serial,firmware,resource_string,"
                "capabilities,source_syntax,safety_limits,bound_at,last_seen)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)"
                " ON CONFLICT(id) DO UPDATE SET idn=excluded.idn,capabilities=excluded.capabilities,"
                "resource_string=excluded.resource_string,last_seen=excluded.last_seen",
                (iid, kind, idn, model, serial, firmware, resource_string, _j(capabilities),
                 _j(source_syntax), _j(safety_limits), _now(), _now()))
            self._conn.commit()
        return iid

    def get_instruments(self, kind: str | None = None) -> list[dict]:
        c = self._reader()
        if kind:
            rows = c.execute("SELECT * FROM instrument WHERE kind=?", (kind,)).fetchall()
        else:
            rows = c.execute("SELECT * FROM instrument").fetchall()
        c.close()
        out = []
        for r in rows:
            d = dict(r)
            for k in ("capabilities", "source_syntax", "safety_limits"):
                d[k] = json.loads(d[k]) if d[k] else {}
            out.append(d)
        return out

    # -- audit -----------------------------------------------------------------------
    def audit(self, tool: str, danger: str, params: dict, decision: str,
              violated_rule: str | None = None, result: dict | None = None) -> int:
        with self._wlock:
            cur = self._conn.execute(
                "INSERT INTO audit_record(ts,tool,danger,params,decision,violated_rule,result)"
                " VALUES (?,?,?,?,?,?,?)",
                (_now(), tool, danger, _j(params), decision, violated_rule, _j(result or {})))
            self._conn.commit()
        self.bus.publish("audit", {"tool": tool, "danger": danger, "decision": decision,
                                   "violated_rule": violated_rule})
        return cur.lastrowid

    def recent_audit(self, limit: int = 50) -> list[dict]:
        c = self._reader()
        rows = c.execute("SELECT * FROM audit_record ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        c.close()
        return [dict(r) for r in rows]

    # -- CSV mirror (spec §5.1) ------------------------------------------------------
    def _csv_path(self, sweep_id: str) -> Path:
        if sweep_id not in self._csv_writers:
            c = self._reader()
            row = c.execute("SELECT s.campaign_id, sw.name FROM sweep sw JOIN session s "
                            "ON sw.session_id=s.id WHERE sw.id=?", (sweep_id,)).fetchone()
            c.close()
            camp = (row["campaign_id"] if row else "unknown")
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
            path = config.CSV_DIR / f"{camp}_{sweep_id}_{stamp}.csv"
            if not path.exists():
                with open(path, "w", newline="", encoding="utf-8") as f:
                    csv.writer(f).writerow([
                        "ts", "attempt_id", "seq", "pulse_cycles", "ext_offset", "mosfet",
                        "voltage", "event_count", "outcome_class", "verified", "confidence",
                        "attempt_valid", "ext_offset_readback", "pulse_cycles_readback",
                        "phase_width_steps", "phase_offset_steps", "trigger_module",
                        "trigger_edge", "trigger_level_v", "candidate_dir",
                        "session_config_sha256", "aux_telemetry_json",
                        "scope_measurements_json", "oracle_readings_json", "notes",
                    ])
            self._csv_writers[sweep_id] = path
        return self._csv_writers[sweep_id]

    def _csv_append(
        self, attempt_id, sweep_id, seq, params, outcome, conf, verified, notes,
        env_sample=None, oracle_readings=None,
    ) -> None:
        try:
            env = dict(env_sample or {})
            aux = dict(env.get("aux_telemetry") or {})
            effective = dict(aux.get("effective_settings") or {})
            frozen = dict(effective.get("frozen_readback") or {})
            composite = {}
            for reading in oracle_readings or []:
                detail = dict((reading or {}).get("detail") or {})
                if detail.get("schema_version") == "glitchlab.project-oracle/v2":
                    composite = detail
                    break
            candidate_dir = (
                composite.get("candidate_dir")
                or aux.get("candidate_dir")
                or effective.get("candidate_dir")
            )
            c = self._reader()
            session_row = c.execute(
                "SELECT se.rig_config FROM sweep sw "
                "JOIN session se ON se.id=sw.session_id WHERE sw.id=?",
                (sweep_id,),
            ).fetchone()
            c.close()
            config_text = str(session_row["rig_config"] or "") if session_row else ""
            config_sha256 = hashlib.sha256(config_text.encode("utf-8")).hexdigest()
            phase = dict(frozen or {})
            with open(self._csv_path(sweep_id), "a", newline="", encoding="utf-8") as f:
                extra = params.get("extra") or {}
                csv.writer(f).writerow([
                    _now(), attempt_id, seq,
                    params.get("pulse_cycles", extra.get("pulse_cycles", params.get("width"))),
                    params.get("ext_offset", params.get("offset")),
                    params.get("mosfet", extra.get("mosfet")), params.get("voltage"),
                    params.get("repeat"), outcome, int(bool(verified)), conf,
                    aux.get("attempt_valid"), effective.get("ext_offset_readback"),
                    effective.get("pulse_cycles_readback"), phase.get("phase_width_steps"),
                    phase.get("phase_offset_steps"), phase.get("trigger_module"),
                    phase.get("trigger_edge"), phase.get("trigger_level_v"), candidate_dir,
                    config_sha256, _j(aux), _j(env.get("scope_measurements") or {}),
                    _j(oracle_readings or []), notes,
                ])
        except Exception as exc:
            self.bus.publish("csv_export_failed", {
                "attempt_id": attempt_id, "sweep_id": sweep_id, "error": repr(exc)
            })
            raise RuntimeError(
                f"attempt {attempt_id} committed to SQLite but CSV mirror failed: {exc!r}"
            ) from exc

    # -- read helpers (used by tools & viewer) ---------------------------------------
    def fetch_one(self, sql: str, args: tuple = ()) -> dict | None:
        c = self._reader()
        r = c.execute(sql, args).fetchone()
        c.close()
        return dict(r) if r else None

    def fetch_all(self, sql: str, args: tuple = ()) -> list[dict]:
        c = self._reader()
        rows = c.execute(sql, args).fetchall()
        c.close()
        return [dict(r) for r in rows]

    def get_campaign(self, cid: str) -> dict | None:
        return self.fetch_one("SELECT * FROM campaign WHERE id=?", (cid,))

    def get_sweep(self, swid: str) -> dict | None:
        return self.fetch_one("SELECT * FROM sweep WHERE id=?", (swid,))

    def list_campaigns(self, project_id: str | None = None) -> list[dict]:
        if project_id:
            return self.fetch_all("SELECT * FROM campaign WHERE project_id=? ORDER BY created_at DESC",
                                  (project_id,))
        return self.fetch_all("SELECT * FROM campaign ORDER BY created_at DESC")

    def sweep_totals(self, sweep_id: str) -> dict[str, int]:
        rows = self.fetch_all("SELECT outcome_class, COUNT(*) n FROM attempt WHERE sweep_id=? "
                              "GROUP BY outcome_class", (sweep_id,))
        return {r["outcome_class"]: r["n"] for r in rows}
