"""Section 15.3: tools and idempotency."""

from __future__ import annotations

from datetime import date

import pytest
from pydantic import ValidationError

from harness.execution.args import (
    AllocationEntry,
    CreatePoArgs,
    NotificationArgs,
    ReallocateLotArgs,
    ReducePoArgs,
    ScheduleCheckArgs,
)
from harness.execution.catalog import all_tools, get_tool
from harness.execution.executor import compensate, execute
from harness.execution.tools import PrecheckFailed, ToolContext


def ctx(run_id: str = "run-1", step: str = "step-1", today: date = date(2026, 9, 2)) -> ToolContext:
    return ToolContext(run_id=run_id, step=step, today=today)


# ---------------------------------------------------------------------------
# Schema validation


@pytest.mark.parametrize("tool", all_tools(), ids=lambda t: t.name)
def test_input_schema_rejects_malformed_args(tool):
    with pytest.raises(ValidationError):
        tool.input_schema.model_validate({"this_field_does_not_exist": True})


# ---------------------------------------------------------------------------
# Idempotency


def test_write_tool_runs_once_for_the_same_idempotency_key(make_harness):
    h = make_harness("scenario_a")
    tool = get_tool("notify_user")
    args = NotificationArgs(to_user="u-301", from_user="u-101", subject="hi", body="hello")
    c = ctx()

    first = execute(h.conn, h.clock, tool, args, c, run_id="run-1", actor="test", requester_id="u-101")
    second = execute(h.conn, h.clock, tool, args, c, run_id="run-1", actor="test", requester_id="u-101")

    assert first == second
    assert h.conn.execute("SELECT COUNT(*) FROM notifications").fetchone()[0] == 1

    events = [row[0] for row in h.conn.execute("SELECT event FROM audit_log ORDER BY seq")]
    assert events.count("action.executed") == 1
    assert events.count("action.skipped_idempotent") == 1


def test_idempotency_key_differs_by_step(make_harness):
    h = make_harness("scenario_a")
    tool = get_tool("notify_user")
    args = NotificationArgs(to_user="u-301", from_user="u-101", subject="hi", body="hello")

    execute(h.conn, h.clock, tool, args, ctx(step="a"), run_id="run-1", actor="test", requester_id="u-101")
    execute(h.conn, h.clock, tool, args, ctx(step="b"), run_id="run-1", actor="test", requester_id="u-101")

    assert h.conn.execute("SELECT COUNT(*) FROM notifications").fetchone()[0] == 2


# ---------------------------------------------------------------------------
# Execution-time scope re-check


def test_execute_refuses_write_if_scope_was_revoked_after_approval(make_harness):
    h = make_harness("scenario_a")
    tool = get_tool("reallocate_lot")
    args = ReallocateLotArgs(
        prod_order_id="4820",
        part_id="P-1180",
        remove=[AllocationEntry(lot_id="L-2093", qty=100)],
        add=[AllocationEntry(lot_id="L-2101", qty=70)],
    )
    # Dana (u-101) has no erp:lot:allocate scope at all.
    with pytest.raises(Exception):
        execute(h.conn, h.clock, tool, args, ctx(), run_id="run-1", actor="test", requester_id="u-101")
    events = [row[0] for row in h.conn.execute("SELECT event FROM audit_log ORDER BY seq")]
    assert "action.scope_denied" in events


# ---------------------------------------------------------------------------
# Prechecks


def test_create_po_precheck_rejects_unapproved_supplier(make_harness):
    h = make_harness("scenario_a")
    tool = get_tool("create_po")
    args = CreatePoArgs(
        po_id="PO-TEST", part_id="P-4471", supplier_id="S-Q", qty=100, unit_price=39.0,
        needed_by="2026-09-05", created_by="u-101",
    )
    with pytest.raises(PrecheckFailed):
        execute(h.conn, h.clock, tool, args, ctx(), run_id="run-1", actor="test", requester_id="u-101")
    assert h.conn.execute(
        "SELECT COUNT(*) FROM erp_purchase_orders WHERE supplier_id = 'S-Q'"
    ).fetchone()[0] == 0


def test_create_po_precheck_rejects_nonexistent_supplier(make_harness):
    h = make_harness("scenario_a")
    tool = get_tool("create_po")
    args = CreatePoArgs(
        po_id="PO-TEST", part_id="P-4471", supplier_id="S-NOPE", qty=10, unit_price=10.0,
        needed_by="2026-09-05", created_by="u-101",
    )
    with pytest.raises(PrecheckFailed):
        execute(h.conn, h.clock, tool, args, ctx(), run_id="run-1", actor="test", requester_id="u-101")


def test_cancel_po_precheck_rejects_nonexistent_po(make_harness):
    from harness.execution.args import CancelPoArgs

    h = make_harness("scenario_a")
    tool = get_tool("cancel_po")
    args = CancelPoArgs(po_id="PO-NOPE")
    with pytest.raises(PrecheckFailed):
        execute(h.conn, h.clock, tool, args, ctx(), run_id="run-1", actor="test", requester_id="u-101")


def test_cancel_po_precheck_rejects_already_cancelled_po(make_harness):
    from harness.execution.args import CancelPoArgs

    h = make_harness("scenario_a")
    h.conn.execute("UPDATE erp_purchase_orders SET status = 'cancelled' WHERE po_id = 'PO-77812'")
    h.conn.commit()
    tool = get_tool("cancel_po")
    args = CancelPoArgs(po_id="PO-77812")
    with pytest.raises(PrecheckFailed):
        execute(h.conn, h.clock, tool, args, ctx(), run_id="run-1", actor="test", requester_id="u-101")


def test_reduce_po_precheck_rejects_nonexistent_po(make_harness):
    h = make_harness("scenario_a")
    tool = get_tool("reduce_po")
    args = ReducePoArgs(po_id="PO-NOPE", new_qty=10)
    with pytest.raises(PrecheckFailed):
        execute(h.conn, h.clock, tool, args, ctx(), run_id="run-1", actor="test", requester_id="u-101")


def test_notify_user_precheck_rejects_nonexistent_recipient(make_harness):
    h = make_harness("scenario_a")
    tool = get_tool("notify_user")
    args = NotificationArgs(to_user="u-999", from_user="u-101", subject="hi", body="hello")
    with pytest.raises(PrecheckFailed):
        execute(h.conn, h.clock, tool, args, ctx(), run_id="run-1", actor="test", requester_id="u-101")


def test_reallocate_lot_precheck_rejects_remove_qty_mismatch(make_harness):
    h = make_harness("scenario_b_covers")
    tool = get_tool("reallocate_lot")
    args = ReallocateLotArgs(
        prod_order_id="4820",
        part_id="P-1180",
        remove=[AllocationEntry(lot_id="L-2093", qty=50)],  # actual allocation is 100
        add=[AllocationEntry(lot_id="L-2101", qty=50)],
    )
    with pytest.raises(PrecheckFailed):
        execute(h.conn, h.clock, tool, args, ctx(), run_id="run-1", actor="test", requester_id="u-202")


def test_reallocate_lot_precheck_rejects_nonexistent_target_lot(make_harness):
    h = make_harness("scenario_b_covers")
    tool = get_tool("reallocate_lot")
    args = ReallocateLotArgs(
        prod_order_id="4820",
        part_id="P-1180",
        remove=[AllocationEntry(lot_id="L-2093", qty=100)],
        add=[AllocationEntry(lot_id="L-NOPE", qty=100)],
    )
    with pytest.raises(PrecheckFailed):
        execute(h.conn, h.clock, tool, args, ctx(), run_id="run-1", actor="test", requester_id="u-202")


def test_compensate_raises_for_a_tool_with_no_compensation(make_harness):
    h = make_harness("scenario_a")
    tool = get_tool("restore_po")  # terminal: no compensate declared
    with pytest.raises(ValueError):
        compensate(h.conn, h.clock, tool, object(), {}, ctx(), run_id="run-1", actor="test", requester_id="u-101")


def test_reduce_po_precheck_rejects_qty_not_less_than_current(make_harness):
    h = make_harness("scenario_a")
    tool = get_tool("reduce_po")
    args = ReducePoArgs(po_id="PO-77812", new_qty=400)
    with pytest.raises(PrecheckFailed):
        execute(h.conn, h.clock, tool, args, ctx(), run_id="run-1", actor="test", requester_id="u-101")


def test_reduce_po_precheck_rejects_non_open_po(make_harness):
    h = make_harness("scenario_a")
    h.conn.execute("UPDATE erp_purchase_orders SET status = 'cancelled' WHERE po_id = 'PO-77812'")
    h.conn.commit()
    tool = get_tool("reduce_po")
    args = ReducePoArgs(po_id="PO-77812", new_qty=100)
    with pytest.raises(PrecheckFailed):
        execute(h.conn, h.clock, tool, args, ctx(), run_id="run-1", actor="test", requester_id="u-101")


def test_reduce_po_args_reject_negative_qty_at_the_schema_level():
    with pytest.raises(ValidationError):
        ReducePoArgs(po_id="PO-77812", new_qty=-1)


def test_reallocate_lot_precheck_rejects_wrong_part(make_harness):
    h = make_harness("scenario_b_covers")
    tool = get_tool("reallocate_lot")
    args = ReallocateLotArgs(
        prod_order_id="4820",
        part_id="P-1180",
        remove=[AllocationEntry(lot_id="L-2093", qty=100)],
        add=[AllocationEntry(lot_id="L-3000", qty=50)],  # L-3000 is part P-5500
    )
    with pytest.raises(PrecheckFailed):
        execute(h.conn, h.clock, tool, args, ctx(), run_id="run-1", actor="test", requester_id="u-202")


def test_reallocate_lot_precheck_rejects_held_target_lot(make_harness):
    h = make_harness("scenario_b_covers")
    tool = get_tool("reallocate_lot")
    args = ReallocateLotArgs(
        prod_order_id="4831",
        part_id="P-1180",
        remove=[AllocationEntry(lot_id="L-2115", qty=50)],
        add=[AllocationEntry(lot_id="L-2093", qty=50)],  # L-2093 is on hold
    )
    with pytest.raises(PrecheckFailed):
        execute(h.conn, h.clock, tool, args, ctx(), run_id="run-1", actor="test", requester_id="u-202")


def test_reallocate_lot_precheck_rejects_insufficient_free_qty(make_harness):
    h = make_harness("scenario_b_covers")
    tool = get_tool("reallocate_lot")
    args = ReallocateLotArgs(
        prod_order_id="4820",
        part_id="P-1180",
        remove=[AllocationEntry(lot_id="L-2093", qty=100)],
        add=[AllocationEntry(lot_id="L-2115", qty=100)],  # only 30 free
    )
    with pytest.raises(PrecheckFailed):
        execute(h.conn, h.clock, tool, args, ctx(), run_id="run-1", actor="test", requester_id="u-202")


def test_reallocate_lot_precheck_rejects_missing_source_allocation(make_harness):
    h = make_harness("scenario_b_covers")
    tool = get_tool("reallocate_lot")
    args = ReallocateLotArgs(
        prod_order_id="4831",  # 4831 is not allocated to L-2093
        part_id="P-1180",
        remove=[AllocationEntry(lot_id="L-2093", qty=100)],
        add=[AllocationEntry(lot_id="L-2101", qty=70)],
    )
    with pytest.raises(PrecheckFailed):
        execute(h.conn, h.clock, tool, args, ctx(), run_id="run-1", actor="test", requester_id="u-202")


# ---------------------------------------------------------------------------
# Compensation


def test_cancel_po_compensates_create_po(make_harness):
    h = make_harness("scenario_a")
    tool = get_tool("create_po")
    args = CreatePoArgs(
        po_id="PO-TEST", part_id="P-4471", supplier_id="S-Z", qty=150, unit_price=46.50,
        needed_by="2026-09-04", created_by="u-101",
    )
    c = ctx(step="create")
    result = execute(h.conn, h.clock, tool, args, c, run_id="run-1", actor="test", requester_id="u-101")
    po_id = result["po_id"]
    assert h.conn.execute(
        "SELECT status FROM erp_purchase_orders WHERE po_id = ?", (po_id,)
    ).fetchone()[0] == "open"

    compensate(h.conn, h.clock, tool, args, result, c, run_id="run-1", actor="test", requester_id="u-101")

    assert h.conn.execute(
        "SELECT status FROM erp_purchase_orders WHERE po_id = ?", (po_id,)
    ).fetchone()[0] == "cancelled"


def test_restore_po_compensates_reduce_po(make_harness):
    h = make_harness("scenario_a")
    tool = get_tool("reduce_po")
    args = ReducePoArgs(po_id="PO-77812", new_qty=100)
    c = ctx(step="reduce")
    result = execute(h.conn, h.clock, tool, args, c, run_id="run-1", actor="test", requester_id="u-101")
    assert h.conn.execute(
        "SELECT qty FROM erp_purchase_orders WHERE po_id = 'PO-77812'"
    ).fetchone()[0] == 100

    compensate(h.conn, h.clock, tool, args, result, c, run_id="run-1", actor="test", requester_id="u-101")

    qty, status = h.conn.execute(
        "SELECT qty, status FROM erp_purchase_orders WHERE po_id = 'PO-77812'"
    ).fetchone()
    assert (qty, status) == (400, "open")


def test_send_correction_compensates_notify_user(make_harness):
    h = make_harness("scenario_a")
    tool = get_tool("notify_user")
    args = NotificationArgs(to_user="u-301", from_user="u-101", subject="Delay", body="PO delayed")
    c = ctx(step="notify")
    result = execute(h.conn, h.clock, tool, args, c, run_id="run-1", actor="test", requester_id="u-101")

    compensate(h.conn, h.clock, tool, args, result, c, run_id="run-1", actor="test", requester_id="u-101")

    subjects = [row[0] for row in h.conn.execute("SELECT subject FROM notifications ORDER BY sent_at, subject")]
    assert len(subjects) == 2
    assert any(s.startswith("Correction:") for s in subjects)


def test_cancel_task_compensates_schedule_check(make_harness):
    h = make_harness("scenario_a")
    tool = get_tool("schedule_check")
    args = ScheduleCheckArgs(
        run_at="2026-09-08", kind="arrival_check", payload={"po_id": "PO-X"}, created_by_run="run-1"
    )
    c = ctx(step="schedule")
    result = execute(h.conn, h.clock, tool, args, c, run_id="run-1", actor="test", requester_id="u-101")
    task_id = result["task_id"]
    assert h.conn.execute(
        "SELECT status FROM scheduled_tasks WHERE task_id = ?", (task_id,)
    ).fetchone()[0] == "pending"

    compensate(h.conn, h.clock, tool, args, result, c, run_id="run-1", actor="test", requester_id="u-101")

    assert h.conn.execute(
        "SELECT status FROM scheduled_tasks WHERE task_id = ?", (task_id,)
    ).fetchone()[0] == "cancelled"


def test_reallocate_lot_compensation_restores_original_allocation_exactly(make_harness):
    h = make_harness("scenario_b_covers")
    tool = get_tool("reallocate_lot")
    args = ReallocateLotArgs(
        prod_order_id="4820",
        part_id="P-1180",
        remove=[AllocationEntry(lot_id="L-2093", qty=100)],
        add=[AllocationEntry(lot_id="L-2101", qty=70), AllocationEntry(lot_id="L-2115", qty=30)],
    )
    c = ctx(step="reallocate")
    result = execute(h.conn, h.clock, tool, args, c, run_id="run-1", actor="test", requester_id="u-202")

    after_forward = dict(h.conn.execute(
        "SELECT lot_id, qty FROM erp_lot_allocations WHERE prod_order_id = '4820'"
    ).fetchall())
    assert after_forward == {"L-2101": 70, "L-2115": 30}

    compensate(h.conn, h.clock, tool, args, result, c, run_id="run-1", actor="test", requester_id="u-202")

    after_back = dict(h.conn.execute(
        "SELECT lot_id, qty FROM erp_lot_allocations WHERE prod_order_id = '4820'"
    ).fetchall())
    assert after_back == {"L-2093": 100}
    # The untouched allocation for the other order must be unaffected.
    assert h.conn.execute(
        "SELECT qty FROM erp_lot_allocations WHERE lot_id = 'L-2115' AND prod_order_id = '4831'"
    ).fetchone()[0] == 50


def test_withdraw_flag_compensates_flag_shortage(make_harness):
    from harness.execution.args import FlagShortageArgs

    h = make_harness("scenario_b_shortage")
    tool = get_tool("flag_shortage")
    args = FlagShortageArgs(
        part_id="P-1180", prod_order_id="4820", qty_short=10, summary="short by 10"
    )
    c = ctx(step="flag")
    result = execute(h.conn, h.clock, tool, args, c, run_id="run-1", actor="test", requester_id="u-202")
    item_id = result["item_id"]
    row = h.conn.execute(
        "SELECT status, owner_id FROM attention_items WHERE item_id = ?", (item_id,)
    ).fetchone()
    assert row[0] == "open"
    assert row[1] == "u-101"  # resolved by role, not supplied by the caller

    compensate(h.conn, h.clock, tool, args, result, c, run_id="run-1", actor="test", requester_id="u-202")

    assert h.conn.execute(
        "SELECT status FROM attention_items WHERE item_id = ?", (item_id,)
    ).fetchone()[0] == "withdrawn"


# ---------------------------------------------------------------------------
# Atomic multi-row writes and rollback on failure


def test_reallocate_lot_split_writes_both_rows_atomically(make_harness):
    h = make_harness("scenario_b_covers")
    tool = get_tool("reallocate_lot")
    args = ReallocateLotArgs(
        prod_order_id="4820",
        part_id="P-1180",
        remove=[AllocationEntry(lot_id="L-2093", qty=100)],
        add=[AllocationEntry(lot_id="L-2101", qty=70), AllocationEntry(lot_id="L-2115", qty=30)],
    )
    execute(h.conn, h.clock, tool, args, ctx(), run_id="run-1", actor="test", requester_id="u-202")

    rows = dict(h.conn.execute(
        "SELECT lot_id, qty FROM erp_lot_allocations WHERE prod_order_id = '4820'"
    ).fetchall())
    assert rows == {"L-2101": 70, "L-2115": 30}
    assert h.conn.execute(
        "SELECT COUNT(*) FROM erp_lot_allocations WHERE lot_id = 'L-2093' AND prod_order_id = '4820'"
    ).fetchone()[0] == 0


def test_executor_rolls_back_all_writes_on_mid_run_failure(make_harness):
    """The mechanism every tool relies on for "a failure partway leaves no
    partial write": execute() wraps precheck, run(), and the
    executed_actions bookkeeping insert in one transaction and rolls back
    on any exception, using a tool built for this test only.
    """

    from pydantic import BaseModel, ConfigDict

    from harness.execution.tools import Tool

    class _NoArgs(BaseModel):
        model_config = ConfigDict(extra="forbid")

    def _run_then_raise(db, args, ctx):
        db.execute("UPDATE erp_parts SET on_hand = on_hand - 1 WHERE part_id = 'P-4471'")
        raise RuntimeError("simulated mid-run failure")

    flaky_tool = Tool(
        name="flaky_test_tool",
        description="test only",
        input_schema=_NoArgs,
        required_scopes=(),
        writes=True,
        run=_run_then_raise,
    )

    h = make_harness("scenario_a")
    before = h.conn.execute("SELECT on_hand FROM erp_parts WHERE part_id = 'P-4471'").fetchone()[0]

    with pytest.raises(RuntimeError):
        execute(
            h.conn, h.clock, flaky_tool, _NoArgs(), ctx(), run_id="run-1", actor="test",
            requester_id="u-101",
        )

    after = h.conn.execute("SELECT on_hand FROM erp_parts WHERE part_id = 'P-4471'").fetchone()[0]
    assert after == before
    assert h.conn.execute(
        "SELECT COUNT(*) FROM executed_actions WHERE tool = 'flaky_test_tool'"
    ).fetchone()[0] == 0
