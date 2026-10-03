"""The simulated clock. Every component that needs "today" reads it from here.

Real wall time (datetime.now, date.today, time.time) must never drive business
logic; a static test greps harness/ to enforce that this is the only file
allowed to be exempt, and even here we never call those functions, since
"today" always comes from the seeded or advanced value in the clock table.
"""

from __future__ import annotations

import sqlite3
from datetime import date, timedelta


class Clock:
    def __init__(self, conn: sqlite3.Connection):
        self._conn = conn

    def today(self) -> date:
        row = self._conn.execute("SELECT today FROM clock WHERE id = 1").fetchone()
        if row is None:
            raise RuntimeError("clock not initialized; seed the database first")
        return date.fromisoformat(row[0])

    def advance(self, days: int = 1) -> date:
        new_today = self.today() + timedelta(days=days)
        self._conn.execute("UPDATE clock SET today = ? WHERE id = 1", (new_today.isoformat(),))
        self._conn.commit()
        return new_today

    def set(self, value: date) -> None:
        self._conn.execute(
            "INSERT INTO clock (id, today) VALUES (1, ?) "
            "ON CONFLICT(id) DO UPDATE SET today = excluded.today",
            (value.isoformat(),),
        )
        self._conn.commit()
