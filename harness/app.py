"""Wires the fake company, the clock, and (in later phases) the registries
into one object the CLI and tests can hold onto.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from harness.scheduling.clock import Clock
from harness.world.seed import seed

DEFAULT_DB_PATH = Path("harness.db")

STATUS_TABLES = (
    "erp_parts",
    "erp_suppliers",
    "erp_purchase_orders",
    "erp_production_orders",
    "erp_receipts",
    "erp_lots",
    "erp_lot_allocations",
    "mail_messages",
    "cal_events",
    "users",
    "notifications",
    "attention_items",
    "runs",
    "approvals",
    "workflow_instances",
    "scheduled_tasks",
    "executed_actions",
    "memory_facts",
    "audit_log",
)


class Harness:
    """Owns the SQLite connection and the clock for one database file."""

    def __init__(self, db_path: Path = DEFAULT_DB_PATH):
        self.db_path = Path(db_path)
        self.conn = self._connect()
        self.clock = Clock(self.conn)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def reset(self, fixture: str) -> None:
        self.conn.close()
        if self.db_path.exists():
            self.db_path.unlink()
        self.conn = self._connect()
        seed(self.conn, fixture)
        self.clock = Clock(self.conn)

    def status(self) -> dict[str, int | str]:
        counts: dict[str, int | str] = {
            table: self.conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in STATUS_TABLES
        }
        counts["today"] = self.clock.today().isoformat()
        return counts
