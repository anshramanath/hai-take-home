"""Direct tests of the free-form tool runner (execution/runner.py):
approval-state guards, the tamper check, and compensation on a mid-plan
failure. The happy paths and ordering/completeness variability are
covered end to end in test_scenario_b.py; this file is about the runner's
own error handling.
"""

from __future__ import annotations

from dataclasses import replace

import pytest

import harness.execution.catalog as catalog_module
from harness.execution.runner import run_approved_plan
from harness.planning.models import ToolCall
from harness.policy.approvals import create_approval, decide
from harness.world.users import get_user


def _create_and_approve_reallocation(h, *, run_id="run-1"):
    omar = get_user(h.conn, "u-202")
    steps = [
        ToolCall(tool="reallocate_lot", args={
            "prod_order_id": "4820", "part_id": "P-1180",
            "remove": [{"lot_id": "L-2093", "qty": 100}],
            "add": [{"lot_id": "L-2101", "qty": 70}, {"lot_id": "L-2115", "qty": 30}],
        }),
        ToolCall(tool="notify_user", args={
            "to_user": "u-301", "from_user": "u-202", "subject": "Reallocated", "body": "Done.",
        }),
    ]
    approval_id = create_approval(
        h.conn, h.clock, run_id=run_id, requester=omar, steps=steps,
        approver_id="u-202", routed_reason=None, workflow=None,
    )
    decide(h.conn, h.clock, approval_id=approval_id, decided_by="u-202", decision="approved")
    return approval_id


def test_run_approved_plan_raises_if_not_approved(make_harness):
    h = make_harness("scenario_b_covers")
    omar = get_user(h.conn, "u-202")
    steps = [ToolCall(tool="reallocate_lot", args={
        "prod_order_id": "4820", "part_id": "P-1180",
        "remove": [{"lot_id": "L-2093", "qty": 100}],
        "add": [{"lot_id": "L-2101", "qty": 70}, {"lot_id": "L-2115", "qty": 30}],
    })]
    approval_id = create_approval(
        h.conn, h.clock, run_id="run-1", requester=omar, steps=steps,
        approver_id="u-202", routed_reason=None, workflow=None,
    )  # never decided: still pending

    with pytest.raises(ValueError):
        run_approved_plan(h.conn, h.clock, approval_id, requester_id="u-202", run_id="run-1")


def test_run_approved_plan_fails_cleanly_if_plan_json_was_tampered(make_harness):
    h = make_harness("scenario_b_covers")
    approval_id = _create_and_approve_reallocation(h)
    h.conn.execute(
        "UPDATE approvals SET plan_json = REPLACE(plan_json, '100', '999') WHERE approval_id = ?",
        (approval_id,),
    )
    h.conn.commit()

    status = run_approved_plan(h.conn, h.clock, approval_id, requester_id="u-202", run_id="run-1")

    assert status == "failed"
    assert h.conn.execute("SELECT COUNT(*) FROM executed_actions").fetchone()[0] == 0
    events = [r[0] for r in h.conn.execute("SELECT event FROM audit_log ORDER BY seq")]
    assert "plan.halted" in events


def test_run_approved_plan_compensates_in_reverse_on_a_mid_plan_failure(make_harness):
    h = make_harness("scenario_b_covers")
    approval_id = _create_and_approve_reallocation(h)

    original_tool = catalog_module.TOOLS["notify_user"]

    def _boom(db, args, ctx):
        raise RuntimeError("simulated notify_user failure")

    catalog_module.TOOLS["notify_user"] = replace(original_tool, run=_boom)
    try:
        status = run_approved_plan(h.conn, h.clock, approval_id, requester_id="u-202", run_id="run-1")
    finally:
        catalog_module.TOOLS["notify_user"] = original_tool

    assert status == "compensated"
    allocations = dict(h.conn.execute(
        "SELECT lot_id, qty FROM erp_lot_allocations WHERE prod_order_id = '4820'"
    ).fetchall())
    assert allocations == {"L-2093": 100}  # reallocate_lot backed out
    assert h.conn.execute("SELECT COUNT(*) FROM notifications").fetchone()[0] == 0

    events = [r[0] for r in h.conn.execute("SELECT event FROM audit_log ORDER BY seq")]
    assert "plan.step_failed" in events
    assert "action.compensated" in events


def test_reallocation_target_lot_put_on_hold_after_approval_is_refused_at_execution(make_harness):
    """Mirrors test_supplier_unapproved_after_approval_is_refused_at_execution_with_nothing_written
    in test_workflow_engine.py, but for the free-form path: the plan approves
    reallocating onto L-2101, then (as if someone placed it on hold moments
    later) L-2101 goes stale before execution. reallocate_lot's precheck,
    the same one every call to this tool goes through, must catch it.
    """

    h = make_harness("scenario_b_covers")
    approval_id = _create_and_approve_reallocation(h)

    h.conn.execute("UPDATE erp_lots SET status = 'hold' WHERE lot_id = 'L-2101'")
    h.conn.commit()

    status = run_approved_plan(h.conn, h.clock, approval_id, requester_id="u-202", run_id="run-1")

    assert status == "compensated"
    allocations = dict(h.conn.execute(
        "SELECT lot_id, qty FROM erp_lot_allocations WHERE prod_order_id = '4820'"
    ).fetchall())
    assert allocations == {"L-2093": 100}  # the original allocation, untouched
    assert h.conn.execute("SELECT COUNT(*) FROM notifications").fetchone()[0] == 0

    events = [r[0] for r in h.conn.execute("SELECT event FROM audit_log ORDER BY seq")]
    assert "action.precheck_failed" in events
    assert "plan.step_failed" in events
    assert "action.compensated" not in events  # nothing had executed yet to reverse
