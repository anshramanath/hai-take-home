"""Part of section 15.12: Scenario B end to end (covers, shortage), plus
the free-form runner's variability cases.
"""

from __future__ import annotations

import json

from harness.app import approve, tick
from harness.planning.llm import FakeLLMClient
from harness.planning.models import NoAction, PlannerOutput, ToolPlan


def _covers_plan(order="forward"):
    reallocate = {
        "tool": "reallocate_lot",
        "args": {
            "prod_order_id": "4820", "part_id": "P-1180",
            "remove": [{"lot_id": "L-2093", "qty": 100}],
            "add": [{"lot_id": "L-2101", "qty": 70}, {"lot_id": "L-2115", "qty": 30}],
        },
    }
    notify = {
        "tool": "notify_user",
        "args": {
            "to_user": "u-301", "from_user": "u-202",
            "subject": "Lot reallocated for production order 4820",
            "body": "L-2093 was on hold; reallocated coverage from L-2101 and L-2115.",
        },
    }
    steps = [notify, reallocate] if order == "notify_first" else [reallocate, notify]
    return ToolPlan(
        kind="plan", steps=steps,
        reasoning="L-2093 is on hold and 4820 starts within 3 days; L-2101+L-2115 cover the need.",
        summary_for_user="Reallocate 4820's coverage away from the held lot.",
    )


def test_scenario_b_covers_reallocates_and_notifies_supervisor(make_harness):
    h = make_harness("scenario_b_covers")
    llm = FakeLLMClient([PlannerOutput(proposal=_covers_plan())])

    tick(h.conn, h.clock, llm)
    approval = h.conn.execute("SELECT approval_id FROM approvals").fetchone()
    assert h.conn.execute("SELECT approver_id FROM approvals").fetchone()[0] == "u-202"

    approve(h.conn, h.clock, llm, approval_id=approval["approval_id"], decided_by="u-202")

    allocations = dict(h.conn.execute(
        "SELECT lot_id, qty FROM erp_lot_allocations WHERE prod_order_id = '4820'"
    ).fetchall())
    assert allocations == {"L-2101": 70, "L-2115": 30}
    assert h.conn.execute(
        "SELECT COUNT(*) FROM erp_lot_allocations WHERE lot_id = 'L-2093'"
    ).fetchone()[0] == 0

    notification = h.conn.execute("SELECT to_user FROM notifications").fetchone()
    assert notification["to_user"] == "u-301"
    assert h.conn.execute("SELECT status FROM runs").fetchone()[0] == "completed"


def test_scenario_b_shortage_flags_purchasing_and_dana_recommends_only(make_harness):
    h = make_harness("scenario_b_shortage")
    llm = FakeLLMClient([
        PlannerOutput(proposal=ToolPlan(
            kind="plan",
            steps=[{"tool": "flag_shortage", "args": {
                "part_id": "P-1180", "prod_order_id": "4820", "qty_short": 10,
                "summary": "Released lots cover only 90 of the 100 units 4820 needs.",
            }}],
            reasoning="No combination of released lots covers the full 100 units needed.",
            summary_for_user="Flag a 10-unit shortage of P-1180 to purchasing.",
        )),
        PlannerOutput(proposal=NoAction(
            kind="none",
            reasoning="No declared workflow exists for buying lot-tracked stock and PO tools "
                       "are workflow-only; recommend purchasing manually source 10 more units.",
        )),
    ])

    tick(h.conn, h.clock, llm)  # Omar's run: flags the shortage
    approval = h.conn.execute("SELECT approval_id FROM approvals").fetchone()
    approve(h.conn, h.clock, llm, approval_id=approval["approval_id"], decided_by="u-202")

    shortage_item = h.conn.execute(
        "SELECT owner_id, status FROM attention_items WHERE dedupe_key = 'shortage:P-1180:4820'"
    ).fetchone()
    assert shortage_item["owner_id"] == "u-101"  # Dana, by role
    assert h.conn.execute(
        "SELECT COUNT(*) FROM executed_actions WHERE tool IN ('create_po', 'cancel_po', 'reduce_po')"
    ).fetchone()[0] == 0

    tick(h.conn, h.clock, llm)  # Dana's run: handles the handoff, recommends only

    runs = h.conn.execute("SELECT user_id, status FROM runs ORDER BY created_at").fetchall()
    assert runs[0]["user_id"] == "u-202" and runs[0]["status"] == "completed"
    assert runs[1]["user_id"] == "u-101" and runs[1]["status"] == "closed"
    # Still no PO tools executed anywhere in this story.
    assert h.conn.execute(
        "SELECT COUNT(*) FROM executed_actions WHERE tool IN ('create_po', 'cancel_po', 'reduce_po')"
    ).fetchone()[0] == 0


# ---------------------------------------------------------------------------
# Free-form variability (the runner does not enforce an order or completeness)


def test_free_form_runner_executes_steps_in_whatever_order_was_approved(make_harness):
    h = make_harness("scenario_b_covers")
    llm = FakeLLMClient([PlannerOutput(proposal=_covers_plan(order="notify_first"))])

    tick(h.conn, h.clock, llm)
    approval = h.conn.execute("SELECT approval_id, plan_json FROM approvals").fetchone()
    steps = json.loads(approval["plan_json"])["steps"]
    assert steps[0]["tool"] == "notify_user"  # the approved plan really is notify-first

    approve(h.conn, h.clock, llm, approval_id=approval["approval_id"], decided_by="u-202")

    allocations = dict(h.conn.execute(
        "SELECT lot_id, qty FROM erp_lot_allocations WHERE prod_order_id = '4820'"
    ).fetchall())
    assert allocations == {"L-2101": 70, "L-2115": 30}
    assert h.conn.execute("SELECT COUNT(*) FROM notifications").fetchone()[0] == 1


def test_free_form_plan_missing_a_step_still_executes_as_approved(make_harness):
    """Documents that free-form does not guarantee completeness: a plan
    that never notifies anyone is still exactly what was approved, and the
    runner has no business second-guessing it.
    """

    h = make_harness("scenario_b_covers")
    reallocate_only = ToolPlan(
        kind="plan",
        steps=[{"tool": "reallocate_lot", "args": {
            "prod_order_id": "4820", "part_id": "P-1180",
            "remove": [{"lot_id": "L-2093", "qty": 100}],
            "add": [{"lot_id": "L-2101", "qty": 70}, {"lot_id": "L-2115", "qty": 30}],
        }}],
        reasoning="Reallocating coverage; notification omitted for this test.",
        summary_for_user="Reallocate 4820's coverage.",
    )
    llm = FakeLLMClient([PlannerOutput(proposal=reallocate_only)])

    tick(h.conn, h.clock, llm)
    approval = h.conn.execute("SELECT approval_id FROM approvals").fetchone()
    approve(h.conn, h.clock, llm, approval_id=approval["approval_id"], decided_by="u-202")

    allocations = dict(h.conn.execute(
        "SELECT lot_id, qty FROM erp_lot_allocations WHERE prod_order_id = '4820'"
    ).fetchall())
    assert allocations == {"L-2101": 70, "L-2115": 30}
    assert h.conn.execute("SELECT COUNT(*) FROM notifications").fetchone()[0] == 0
    assert h.conn.execute("SELECT status FROM runs").fetchone()[0] == "completed"
