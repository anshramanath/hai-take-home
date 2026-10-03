from __future__ import annotations

import sqlite3
from dataclasses import dataclass

import pytest

from harness.execution import workflows as _workflows  # noqa: F401  (registers every workflow definition)
from harness.scheduling import arrival_check as _arrival_check  # noqa: F401  (registers the arrival_check task handler)
from harness.scheduling.clock import Clock
from harness.world.seed import seed


@dataclass
class HarnessDB:
    conn: sqlite3.Connection
    clock: Clock


@pytest.fixture
def make_harness(tmp_path):
    """Factory fixture: make_harness("scenario_a") returns a fresh, seeded
    temp-file SQLite connection plus its Clock. Every call gets its own file,
    so tests never share state.
    """

    created: list[sqlite3.Connection] = []

    def _make(fixture: str = "scenario_a") -> HarnessDB:
        db_path = tmp_path / f"{fixture}-{len(created)}.db"
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        seed(conn, fixture)
        created.append(conn)
        return HarnessDB(conn=conn, clock=Clock(conn))

    yield _make

    for conn in created:
        conn.close()
