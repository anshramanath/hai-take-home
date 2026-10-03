"""Deferred task execution (section 13): tick() runs every task whose
run_at has arrived, dispatching by `kind` to a registered handler. A task
is marked fired before its handler runs, so it fires exactly once even if
tick is called more than once on the same day.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any, Callable

from harness.audit.log import log as audit_log
from harness.scheduling.clock import Clock

TaskHandler = Callable[[sqlite3.Connection, Clock, dict[str, Any]], None]

_HANDLERS: dict[str, TaskHandler] = {}


def register_handler(kind: str, handler: TaskHandler) -> None:
    _HANDLERS[kind] = handler


def run_due_tasks(conn: sqlite3.Connection, clock: Clock) -> list[str]:
    today = clock.today().isoformat()
    due = conn.execute(
        "SELECT task_id, run_at, kind, payload, created_by_run FROM scheduled_tasks "
        "WHERE status = 'pending' AND run_at <= ?",
        (today,),
    ).fetchall()

    fired: list[str] = []
    for task_id, run_at, kind, payload_json, created_by_run in due:
        conn.execute("UPDATE scheduled_tasks SET status = 'fired' WHERE task_id = ?", (task_id,))
        audit_log(
            conn, clock, run_id=created_by_run, actor="scheduler", event="schedule.fired",
            detail={"task_id": task_id, "kind": kind, "run_at": run_at},
        )
        conn.commit()

        handler = _HANDLERS.get(kind)
        if handler is not None:
            handler(conn, clock, {
                "task_id": task_id, "payload": json.loads(payload_json), "created_by_run": created_by_run,
            })
        fired.append(task_id)

    return fired
