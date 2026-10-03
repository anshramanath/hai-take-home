"""The detector registry: every registered detector runs on every tick, and
also right after a tool writes to an ERP table (see
execution.catalog.ERP_WRITING_TOOLS). A UNIQUE violation on dedupe_key
means "already known"; it is logged, not raised.
"""

from __future__ import annotations

import json
import sqlite3
import uuid

from harness.audit.log import log as audit_log
from harness.detection.base import AttentionItem, DetectionContext
from harness.detection.stockout import StockoutDetector
from harness.scheduling.clock import Clock

DETECTORS: list = [StockoutDetector()]


def run_detectors(conn: sqlite3.Connection, clock: Clock) -> list[str]:
    """Returns the item_ids of items newly created (not duplicates)."""

    ctx = DetectionContext(conn=conn, clock=clock)
    created: list[str] = []

    for detector in DETECTORS:
        for item in detector.detect(ctx):
            created_id = _insert_or_log_duplicate(conn, clock, detector.name, item)
            if created_id is not None:
                created.append(created_id)

    return created


def _insert_or_log_duplicate(
    conn: sqlite3.Connection, clock: Clock, detector_name: str, item: AttentionItem
) -> str | None:
    item_id = f"AI-{uuid.uuid4().hex[:8].upper()}"
    try:
        conn.execute(
            "INSERT INTO attention_items (item_id, dedupe_key, detector, owner_id, summary, facts, "
            "status, created_at) VALUES (?, ?, ?, ?, ?, ?, 'open', ?)",
            (
                item_id, item.dedupe_key, item.detector, item.owner_id, item.summary,
                json.dumps(item.facts), clock.today().isoformat(),
            ),
        )
    except sqlite3.IntegrityError:
        conn.rollback()
        audit_log(
            conn, clock, run_id=None, actor=detector_name, event="detection.duplicate_ignored",
            detail={"dedupe_key": item.dedupe_key},
        )
        conn.commit()
        return None

    audit_log(
        conn, clock, run_id=None, actor=detector_name, event="detection.raised",
        detail={"item_id": item_id, "dedupe_key": item.dedupe_key, "owner_id": item.owner_id, "summary": item.summary},
    )
    conn.commit()
    return item_id
