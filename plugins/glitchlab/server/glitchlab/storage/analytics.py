"""DuckDB analytical layer + Parquet lake ELT (spec §5.2/§5.3).

DuckDB ATTACHes the live SQLite file for real-time cross-session analytics with no export step,
and reads the partitioned Parquet lake for finalized sessions. `run_query` (spec §11.1) executes
sandboxed SELECT-only SQL through here.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import duckdb

from .. import config


class Analytics:
    def __init__(self, sqlite_path: Path) -> None:
        self.sqlite_path = Path(sqlite_path)

    def _con(self) -> duckdb.DuckDBPyConnection:
        con = duckdb.connect(database=":memory:")
        # ATTACH the live SQLite store read-only (spec §5.2 "everything historical plus now").
        try:
            con.execute("INSTALL sqlite; LOAD sqlite;")
        except Exception:
            pass
        try:
            con.execute(f"ATTACH '{self.sqlite_path.as_posix()}' AS live (TYPE sqlite, READ_ONLY);")
        except Exception:
            pass
        return con

    def query(self, sql: str, max_rows: int = 1000) -> tuple[list[str], list[list[Any]]]:
        """Execute a SELECT against the attached live DB + lake. Rejects non-SELECT."""
        s = sql.strip().rstrip(";")
        low = s.lower()
        if not (low.startswith("select") or low.startswith("with") or low.startswith("pragma")
                or low.startswith("describe") or low.startswith("summarize")):
            raise ValueError("run_query is read-only: only SELECT/WITH/DESCRIBE/SUMMARIZE allowed")
        forbidden = (" insert ", " update ", " delete ", " drop ", " attach ", " create ",
                     " alter ", " copy ", " replace ", " install ", " load ")
        pad = f" {low} "
        if any(tok in pad for tok in forbidden):
            raise ValueError("run_query rejected a non-SELECT keyword")
        con = self._con()
        try:
            rel = con.execute(s)
            cols = [d[0] for d in rel.description] if rel.description else []
            rows = rel.fetchmany(max_rows)
            return cols, [list(r) for r in rows]
        finally:
            con.close()

    def cross_session_rate(self, target_model: str) -> list[dict]:
        """Success rate by (width,offset) bin across every session for a target model."""
        con = self._con()
        try:
            q = """
            SELECT a.width, a.offset,
                   COUNT(*) AS trials,
                   SUM(CASE WHEN a.outcome_class='success' THEN 1 ELSE 0 END) AS successes
            FROM live.attempt a
            JOIN live.sweep sw ON a.sweep_id = sw.id
            JOIN live.session s ON sw.session_id = s.id
            JOIN live.campaign c ON s.campaign_id = c.id
            JOIN live.target t ON c.target_id = t.id
            WHERE t.model = ?
            GROUP BY a.width, a.offset
            ORDER BY successes DESC
            """
            rel = con.execute(q, [target_model])
            cols = [d[0] for d in rel.description]
            return [dict(zip(cols, r)) for r in rel.fetchall()]
        except Exception:
            return []
        finally:
            con.close()

    def flush_session_to_lake(self, session_id: str, target_model: str, campaign_id: str) -> str | None:
        """ELT: write a finalized session's attempts to the partitioned Parquet lake (§5.2)."""
        out_dir = (config.LAKE_DIR / f"target={target_model}" / f"campaign={campaign_id}"
                   / f"session={session_id}")
        out_dir.mkdir(parents=True, exist_ok=True)
        out = out_dir / "attempts.parquet"
        con = self._con()
        try:
            con.execute(
                "COPY (SELECT a.* FROM live.attempt a JOIN live.sweep sw ON a.sweep_id=sw.id "
                "WHERE sw.session_id = ?) TO ? (FORMAT PARQUET)", [session_id, out.as_posix()])
            return str(out)
        except Exception:
            return None
        finally:
            con.close()
