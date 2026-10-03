"""Section 15.1: world and clock."""

from __future__ import annotations

import re
import sqlite3
from datetime import date
from pathlib import Path

import pytest

from harness.scheduling.clock import Clock
from harness.world.seed import FIXTURES, create_schema, seed

HARNESS_ROOT = Path(__file__).resolve().parent.parent / "harness"
CLOCK_FILE = HARNESS_ROOT / "scheduling" / "clock.py"

# Tables common to every fixture (the full user roster is always seeded).
COMMON_COUNTS = {"users": 5}

SCENARIO_A_COUNTS = {
    **COMMON_COUNTS,
    "erp_parts": 3,
    "erp_suppliers": 5,
    "erp_purchase_orders": 3,
    "erp_production_orders": 2,
    "erp_receipts": 0,
    "erp_lots": 0,
    "erp_lot_allocations": 0,
    "mail_messages": 5,
    "cal_events": 3,
    "notifications": 0,
}

SCENARIO_B_COUNTS = {
    **COMMON_COUNTS,
    "erp_parts": 2,
    "erp_suppliers": 0,
    "erp_purchase_orders": 0,
    "erp_production_orders": 2,
    "erp_receipts": 0,
    "erp_lots": 4,
    "erp_lot_allocations": 2,
    "mail_messages": 0,
    "cal_events": 0,
    "notifications": 0,
}

EXPECTED_COUNTS = {
    "scenario_a": SCENARIO_A_COUNTS,
    "scenario_a_no_supplier": SCENARIO_A_COUNTS,
    "scenario_a_over_limit": SCENARIO_A_COUNTS,
    "scenario_a_backup_low_limit": SCENARIO_A_COUNTS,
    "scenario_a_no_arrival": SCENARIO_A_COUNTS,
    "scenario_b_covers": SCENARIO_B_COUNTS,
    "scenario_b_shortage": SCENARIO_B_COUNTS,
}


def _row_count(conn: sqlite3.Connection, table: str) -> int:
    return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


@pytest.mark.parametrize("fixture", FIXTURES)
def test_fixture_seeds_without_error(make_harness, fixture):
    harness = make_harness(fixture)
    for table, expected in EXPECTED_COUNTS[fixture].items():
        assert _row_count(harness.conn, table) == expected, f"{fixture}: {table}"


def test_all_fixtures_covered_by_expectations():
    assert set(EXPECTED_COUNTS) == set(FIXTURES)


def test_clock_today_returns_seeded_date(make_harness):
    harness = make_harness("scenario_a")
    assert harness.clock.today() == date(2026, 9, 2)


def test_clock_advance_moves_the_date(make_harness):
    harness = make_harness("scenario_a")
    new_today = harness.clock.advance(3)
    assert new_today == date(2026, 9, 5)
    assert harness.clock.today() == date(2026, 9, 5)


def test_clock_persists_across_a_new_connection(make_harness, tmp_path):
    harness = make_harness("scenario_a")
    harness.clock.advance(2)
    harness.conn.commit()

    db_path = Path(harness.conn.execute("PRAGMA database_list").fetchone()[2])
    reopened = sqlite3.connect(db_path)
    row = reopened.execute("SELECT today FROM clock WHERE id = 1").fetchone()
    reopened.close()

    assert row[0] == "2026-09-04"


def test_clock_set_overwrites_existing_row(make_harness):
    harness = make_harness("scenario_a")
    harness.clock.set(date(2030, 1, 1))
    assert harness.clock.today() == date(2030, 1, 1)


def test_seed_rejects_unknown_fixture(tmp_path):
    conn = sqlite3.connect(tmp_path / "bogus.db")
    with pytest.raises(ValueError):
        seed(conn, "no-such-fixture")
    conn.close()


def test_clock_raises_before_database_is_seeded(tmp_path):
    conn = sqlite3.connect(tmp_path / "unseeded.db")
    create_schema(conn)
    with pytest.raises(RuntimeError):
        Clock(conn).today()
    conn.close()


def test_audit_log_rejects_update_and_delete(make_harness):
    harness = make_harness("scenario_a")
    harness.conn.execute(
        "INSERT INTO audit_log (ts, run_id, actor, event, detail) VALUES (?, ?, ?, ?, ?)",
        ("2026-09-02", None, "test", "test.event", "{}"),
    )
    harness.conn.commit()

    with pytest.raises(sqlite3.IntegrityError):
        harness.conn.execute("UPDATE audit_log SET event = 'changed' WHERE seq = 1")

    with pytest.raises(sqlite3.IntegrityError):
        harness.conn.execute("DELETE FROM audit_log WHERE seq = 1")


NO_WALL_CLOCK_PATTERN = re.compile(r"\b(datetime\.now|date\.today|time\.time)\s*\(")


def test_no_wall_clock_calls_outside_clock_module():
    offenders = []
    for path in HARNESS_ROOT.rglob("*.py"):
        if path == CLOCK_FILE:
            continue
        text = path.read_text()
        if NO_WALL_CLOCK_PATTERN.search(text):
            offenders.append(str(path.relative_to(HARNESS_ROOT.parent)))
    assert not offenders, f"wall-clock calls found outside clock.py: {offenders}"
