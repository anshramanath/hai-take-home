"""Section 15.10: scheduler and arrival check."""

from __future__ import annotations

import json
import sqlite3

from harness.execution.args import ScheduleCheckArgs
from harness.execution.catalog import get_tool
from harness.execution.executor import execute
from harness.execution.tools import ToolContext
from harness.memory.runs import create_run
from harness.scheduling.tasks import run_due_tasks
from harness.world.receipts import record_receipt


def _schedule_arrival_check(h, *, po_id: str, part_id: str, prod_order_id: str, run_at: str, run_id: str | None = None):
    if run_id is None:
        # A real run, as in production: schedule_check only ever fires
        # from within a workflow tied to one, and the success path updates
        # its state.
        run_id = create_run(h.conn, h.clock, item_id="AI-TEST", user_id="u-101")
    tool = get_tool("schedule_check")
    args = ScheduleCheckArgs(
        run_at=run_at, kind="arrival_check",
        payload={"po_id": po_id, "part_id": part_id, "prod_order_id": prod_order_id},
        created_by_run=run_id,
    )
    ctx = ToolContext(run_id=run_id, step="schedule_arrival_check", today=h.clock.today())
    result = execute(h.conn, h.clock, tool, args, ctx, run_id=run_id, actor="test", requester_id="u-101")
    return result["task_id"]


def test_task_does_not_fire_before_its_run_at(make_harness):
    h = make_harness("scenario_a")
    _schedule_arrival_check(h, po_id="PO-77812", part_id="P-4471", prod_order_id="4812", run_at="2026-09-10")

    fired = run_due_tasks(h.conn, h.clock)  # today is still 2026-09-02

    assert fired == []
    assert h.conn.execute("SELECT status FROM scheduled_tasks").fetchone()[0] == "pending"


def test_task_fires_exactly_once_even_if_run_due_tasks_runs_twice_same_day(make_harness):
    h = make_harness("scenario_a")
    h.clock.advance(2)  # today = 2026-09-04
    _schedule_arrival_check(h, po_id="PO-77812", part_id="P-4471", prod_order_id="4812", run_at="2026-09-04")

    first = run_due_tasks(h.conn, h.clock)
    second = run_due_tasks(h.conn, h.clock)

    assert len(first) == 1
    assert second == []
    events = [r[0] for r in h.conn.execute("SELECT event FROM audit_log WHERE event = 'schedule.fired'")]
    assert len(events) == 1


def test_task_survives_restart(make_harness, tmp_path):
    h = make_harness("scenario_a")
    h.clock.advance(2)  # today = 2026-09-04
    task_id = _schedule_arrival_check(h, po_id="PO-77812", part_id="P-4471", prod_order_id="4812", run_at="2026-09-04")
    h.conn.commit()

    # Copy to a real file via the backup API, close the original connection,
    # and reopen fresh: simulates a new process picking up where a killed
    # one left off.
    db_path = tmp_path / "restart.db"
    file_conn = sqlite3.connect(db_path)
    h.conn.backup(file_conn)
    file_conn.close()
    h.conn.close()

    from harness.scheduling.clock import Clock

    new_conn = sqlite3.connect(db_path)
    new_conn.row_factory = sqlite3.Row
    new_clock = Clock(new_conn)

    fired = run_due_tasks(new_conn, new_clock)

    assert task_id in fired
    assert new_conn.execute(
        "SELECT status FROM scheduled_tasks WHERE task_id = ?", (task_id,)
    ).fetchone()[0] == "fired"
    new_conn.close()


def test_arrival_received_in_full_closes_out_and_writes_memory_fact(make_harness):
    h = make_harness("scenario_a")
    h.clock.advance(2)  # today = 2026-09-04
    _schedule_arrival_check(h, po_id="PO-77812", part_id="P-4471", prod_order_id="4812", run_at="2026-09-04")
    record_receipt(h.conn, h.clock, po_id="PO-77812", qty=400)

    run_due_tasks(h.conn, h.clock)

    events = [r[0] for r in h.conn.execute("SELECT event FROM audit_log ORDER BY seq")]
    assert "arrival_check.confirmed" in events
    assert "memory.fact_written" in events
    fact = h.conn.execute("SELECT subject, fact, source_ids FROM memory_facts").fetchone()
    assert fact["subject"] == "S-Y"
    assert json.loads(fact["source_ids"]) == ["PO-77812"]
    # No re-entry: no new attention item for this PO.
    assert h.conn.execute(
        "SELECT COUNT(*) FROM attention_items WHERE dedupe_key = 'stockout:P-4471:4812:PO-77812'"
    ).fetchone()[0] == 0


def test_arrival_not_received_creates_a_new_item_and_re_enters(make_harness):
    h = make_harness("scenario_a_no_arrival")
    h.clock.advance(2)  # today = 2026-09-04
    _schedule_arrival_check(h, po_id="PO-77812", part_id="P-4471", prod_order_id="4812", run_at="2026-09-04")
    # No receipt recorded.

    run_due_tasks(h.conn, h.clock)

    events = [r[0] for r in h.conn.execute("SELECT event FROM audit_log ORDER BY seq")]
    assert "arrival_check.missed" in events
    row = h.conn.execute(
        "SELECT owner_id, facts FROM attention_items WHERE dedupe_key = 'stockout:P-4471:4812:PO-77812'"
    ).fetchone()
    assert row is not None
    facts = json.loads(row["facts"])
    assert facts["condition"] == "missed_arrival"
    assert facts["inbound_po_id"] == "PO-77812"


def test_arrival_not_received_falls_back_to_role_when_po_has_no_created_by(make_harness):
    h = make_harness("scenario_a_no_arrival")
    h.conn.execute("UPDATE erp_purchase_orders SET created_by = NULL WHERE po_id = 'PO-77812'")
    h.conn.commit()
    h.clock.advance(2)
    _schedule_arrival_check(h, po_id="PO-77812", part_id="P-4471", prod_order_id="4812", run_at="2026-09-04")

    run_due_tasks(h.conn, h.clock)

    owner = h.conn.execute(
        "SELECT owner_id FROM attention_items WHERE dedupe_key = 'stockout:P-4471:4812:PO-77812'"
    ).fetchone()[0]
    assert owner == "u-101"  # falls back to the user with role 'Purchasing Manager'


def test_arrival_not_received_partial_receipt_still_re_enters(make_harness):
    h = make_harness("scenario_a_no_arrival")
    h.clock.advance(2)
    _schedule_arrival_check(h, po_id="PO-77812", part_id="P-4471", prod_order_id="4812", run_at="2026-09-04")
    record_receipt(h.conn, h.clock, po_id="PO-77812", qty=100)  # short of the full 400

    run_due_tasks(h.conn, h.clock)

    events = [r[0] for r in h.conn.execute("SELECT event FROM audit_log ORDER BY seq")]
    assert "arrival_check.missed" in events
