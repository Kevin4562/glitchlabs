"""SQLite operational schema (spec §4.2). Typed columns promoted for hot filters (§4.2 storage note).

A single WAL database holds all entities. This is a pragmatic consolidation of the spec's
"one file per session" (§5.1): it preserves crash-safe single-writer/many-reader WAL semantics and
makes DuckDB ATTACH + cross-session queries trivial, while CSV mirror (§5.1) and the Parquet lake
(§5.2) provide the portable + analytical stores. Every guarantee in §5 is retained.
"""
from __future__ import annotations

DDL = r"""
PRAGMA journal_mode=WAL;
PRAGMA synchronous=NORMAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS target (
    id TEXT PRIMARY KEY, vendor TEXT, model TEXT, package TEXT, revision TEXT,
    injection_types TEXT, notes TEXT
);

CREATE TABLE IF NOT EXISTS unit (
    id TEXT PRIMARY KEY, target_id TEXT REFERENCES target(id),
    serial TEXT, batch TEXT, first_seen TEXT, last_seen TEXT
);

CREATE TABLE IF NOT EXISTS project (
    id TEXT PRIMARY KEY, name TEXT, notes TEXT, created_at TEXT
);

CREATE TABLE IF NOT EXISTS campaign (
    id TEXT PRIMARY KEY, name TEXT, objective TEXT, target_id TEXT REFERENCES target(id),
    created_at TEXT, mode TEXT,  -- full | notes
    project_id TEXT REFERENCES project(id)   -- separates unrelated projects
);
CREATE INDEX IF NOT EXISTS ix_campaign_project ON campaign(project_id);

CREATE TABLE IF NOT EXISTS session (
    id TEXT PRIMARY KEY, campaign_id TEXT REFERENCES campaign(id),
    unit_id TEXT REFERENCES unit(id), started_at TEXT, ended_at TEXT, operator TEXT,
    rig_config TEXT, resumable_state TEXT, status TEXT   -- active|paused|done|aborted
);
CREATE INDEX IF NOT EXISTS ix_session_campaign ON session(campaign_id);

CREATE TABLE IF NOT EXISTS sweep (
    id TEXT PRIMARY KEY, session_id TEXT REFERENCES session(id),
    parent_sweep_id TEXT REFERENCES sweep(id),
    kind TEXT, param_spec TEXT, axis_flags TEXT, optimizer_state TEXT,
    confidence TEXT,           -- provisional|needs-reverification|confirmed
    measurement_state TEXT,    -- not_attempted|functional_unquantified|quantified (§4.4)
    name TEXT, created_at TEXT, status TEXT   -- defined|running|paused|done|aborted
);
CREATE INDEX IF NOT EXISTS ix_sweep_session ON sweep(session_id);

CREATE TABLE IF NOT EXISTS outcome_class (
    id INTEGER PRIMARY KEY AUTOINCREMENT, campaign_id TEXT,
    key TEXT, label TEXT, color TEXT, marker TEXT, glyph TEXT,
    is_success INTEGER, is_collateral INTEGER, sort_order INTEGER,
    UNIQUE(campaign_id, key)
);

CREATE TABLE IF NOT EXISTS attempt (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    sweep_id TEXT REFERENCES sweep(id), seq INTEGER, ts TEXT,
    -- promoted typed columns (fast filters / aggregates)
    offset REAL, width REAL, voltage REAL, x REAL, y REAL, z REAL, repeat INTEGER,
    params TEXT,                -- full parameter tuple JSON
    outcome_class TEXT, outcome_confidence REAL,
    verdict_version INTEGER, verdict_source TEXT,   -- classifier|sampling|manual
    stage_reached INTEGER, duration_ms REAL, verified INTEGER, notes TEXT
);
CREATE INDEX IF NOT EXISTS ix_attempt_sweep ON attempt(sweep_id);
CREATE INDEX IF NOT EXISTS ix_attempt_outcome ON attempt(sweep_id, outcome_class);
CREATE INDEX IF NOT EXISTS ix_attempt_cell ON attempt(sweep_id, width, offset);

CREATE TABLE IF NOT EXISTS attempt_verdict (   -- verdict version history (§4.3)
    id INTEGER PRIMARY KEY AUTOINCREMENT, attempt_id INTEGER REFERENCES attempt(id),
    verdict_version INTEGER, outcome_class TEXT, outcome_confidence REAL,
    verdict_source TEXT, ts TEXT
);

CREATE TABLE IF NOT EXISTS raw_capture (
    id INTEGER PRIMARY KEY AUTOINCREMENT, attempt_id INTEGER REFERENCES attempt(id),
    channel TEXT, payload BLOB, encoding TEXT, preamble TEXT, is_sidecar INTEGER, sidecar_path TEXT
);
CREATE INDEX IF NOT EXISTS ix_raw_attempt ON raw_capture(attempt_id);
CREATE INDEX IF NOT EXISTS ix_attempt_verdict_attempt ON attempt_verdict(attempt_id);

CREATE TABLE IF NOT EXISTS oracle_reading (
    id INTEGER PRIMARY KEY AUTOINCREMENT, attempt_id INTEGER REFERENCES attempt(id),
    oracle_name TEXT, verdict TEXT, latency_ms REAL,
    detail TEXT                 -- complete project-specific oracle evidence JSON
);
CREATE INDEX IF NOT EXISTS ix_oracle_attempt ON oracle_reading(attempt_id);

CREATE TABLE IF NOT EXISTS fault_detail (
    id INTEGER PRIMARY KEY AUTOINCREMENT, attempt_id INTEGER REFERENCES attempt(id),
    corrupted_bitmask BLOB, affected_round TEXT, affected_op TEXT,
    instruction_type TEXT, fault_model TEXT
);
CREATE INDEX IF NOT EXISTS ix_fault_attempt ON fault_detail(attempt_id);

CREATE TABLE IF NOT EXISTS env_sample (
    id INTEGER PRIMARY KEY AUTOINCREMENT, attempt_id INTEGER REFERENCES attempt(id),
    ambient_temp_c REAL, board_temp_c REAL, concurrent_bus_activity TEXT,
    aux_telemetry TEXT, scope_measurements TEXT
);
CREATE INDEX IF NOT EXISTS ix_env_attempt ON env_sample(attempt_id, id);

CREATE TABLE IF NOT EXISTS annotation (
    id INTEGER PRIMARY KEY AUTOINCREMENT, sweep_id TEXT REFERENCES sweep(id),
    region TEXT, text TEXT, flag TEXT, author TEXT, created_at TEXT
);
CREATE INDEX IF NOT EXISTS ix_annotation_sweep ON annotation(sweep_id);

CREATE TABLE IF NOT EXISTS parameter_profile (
    id INTEGER PRIMARY KEY AUTOINCREMENT, target_model TEXT, injection_type TEXT,
    known_good TEXT, provenance TEXT, updated_at TEXT
);

CREATE TABLE IF NOT EXISTS instrument (
    id TEXT PRIMARY KEY, kind TEXT, idn TEXT, model TEXT, serial TEXT, firmware TEXT,
    resource_string TEXT, capabilities TEXT, source_syntax TEXT, safety_limits TEXT,
    bound_at TEXT, last_seen TEXT
);

CREATE TABLE IF NOT EXISTS audit_record (
    id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, tool TEXT, danger TEXT,
    params TEXT, decision TEXT, violated_rule TEXT, result TEXT
);

-- Materialized rollup (spec §5.3) — per-cell per-class counts, refreshed incrementally.
CREATE TABLE IF NOT EXISTS cell_rollup (
    sweep_id TEXT, width REAL, offset REAL, outcome_class TEXT, n INTEGER,
    PRIMARY KEY (sweep_id, width, offset, outcome_class)
);
"""
