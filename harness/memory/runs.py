"""Run bookkeeping: what the current run knows (`runs.state`), as distinct
from what persists across runs (`memory_facts`, phase 5). A run is created
once an attention item is handed to the planner, and its status tracks
that one attempt at resolving it, through to completion, rejection, or
failure.
"""

from __future__ import annotations

import json
import sqlite3
import uuid

from harness.scheduling.clock import Clock


class UnknownRun(Exception):
    pass


def create_run(conn: sqlite3.Connection, clock: Clock, *, item_id: str, user_id: str) -> str:
    run_id = f"RUN-{uuid.uuid4().hex[:8].upper()}"
    conn.execute(
        "INSERT INTO runs (run_id, item_id, user_id, status, state, created_at) "
        "VALUES (?, ?, ?, 'running', '{}', ?)",
        (run_id, item_id, user_id, clock.today().isoformat()),
    )
    conn.commit()
    return run_id


def get_run(conn: sqlite3.Connection, run_id: str) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()
    if row is None:
        raise UnknownRun(run_id)
    return row


def set_run_status(conn: sqlite3.Connection, run_id: str, status: str) -> None:
    conn.execute("UPDATE runs SET status = ? WHERE run_id = ?", (status, run_id))
    conn.commit()


def update_run_state(conn: sqlite3.Connection, run_id: str, updates: dict) -> None:
    row = get_run(conn, run_id)
    state = json.loads(row["state"])
    state.update(updates)
    conn.execute("UPDATE runs SET state = ? WHERE run_id = ?", (json.dumps(state), run_id))
    conn.commit()
