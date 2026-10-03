"""The append-only audit log. Every component writes here; nothing reads
business meaning back out except the (later) `explain` renderer. The table
itself enforces append-only via SQL triggers (schema.sql); this module just
gives every writer the same shape.

`log()` only INSERTs; it does not commit. A caller that is also writing
business data (the executor) should commit once after both are staged, so
the audit entry and the write it describes land together or not at all. A
caller with nothing else pending should commit right after calling this.
"""

from __future__ import annotations

import json
import sqlite3

from harness.scheduling.clock import Clock


def log(
    conn: sqlite3.Connection,
    clock: Clock,
    *,
    run_id: str | None,
    actor: str,
    event: str,
    detail: dict,
) -> int:
    cur = conn.execute(
        "INSERT INTO audit_log (ts, run_id, actor, event, detail) VALUES (?, ?, ?, ?, ?)",
        (clock.today().isoformat(), run_id, actor, event, json.dumps(detail, sort_keys=True)),
    )
    return cur.lastrowid


def events_for_run(conn: sqlite3.Connection, run_id: str) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM audit_log WHERE run_id = ? ORDER BY seq", (run_id,)
    ).fetchall()


def all_events(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM audit_log ORDER BY seq").fetchall()
