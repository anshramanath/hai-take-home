"""Persistent memory (section 13): a handful of structured facts, written
only on confirmed outcomes (a completed workflow, a confirmed arrival),
never on predictions. Every fact carries its source record ids. Facts are
handed to the planner as hints only — section 9's distinction between what
the model sees and what actually gates a decision applies here too:
nothing in the gate or the workflow engine ever reads memory_facts.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import timedelta

from harness.audit.log import log as audit_log
from harness.scheduling.clock import Clock


def write_fact(
    conn: sqlite3.Connection,
    clock: Clock,
    *,
    subject: str,
    fact: str,
    source_ids: list[str],
    visible_to_scope: str | None = None,
    expires_in_days: int | None = None,
) -> str:
    fact_id = f"MF-{uuid.uuid4().hex[:8].upper()}"
    today = clock.today()
    expires_at = (today + timedelta(days=expires_in_days)).isoformat() if expires_in_days else None

    conn.execute(
        "INSERT INTO memory_facts (fact_id, subject, fact, source_ids, visible_to_scope, "
        "created_at, expires_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (fact_id, subject, fact, json.dumps(source_ids), visible_to_scope, today.isoformat(), expires_at),
    )
    audit_log(
        conn, clock, run_id=None, actor="memory", event="memory.fact_written",
        detail={"fact_id": fact_id, "subject": subject, "fact": fact, "source_ids": source_ids},
    )
    conn.commit()
    return fact_id


def facts_for_prompt(conn: sqlite3.Connection, clock: Clock) -> list[dict]:
    """Every non-expired fact, shaped for the planner's prompt. Small
    enough in this harness that it isn't worth narrowing by subject; a
    real deployment would scope this to the item's part/supplier and to
    what the requesting user may see.
    """

    today = clock.today().isoformat()
    rows = conn.execute(
        "SELECT subject, fact, source_ids FROM memory_facts "
        "WHERE expires_at IS NULL OR expires_at >= ?",
        (today,),
    ).fetchall()
    return [
        {"subject": row[0], "fact": row[1], "source_ids": json.loads(row[2])}
        for row in rows
    ]
