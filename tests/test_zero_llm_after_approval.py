"""Tier 2, T1 of FIXES (1).md: invariant 3 says zero LLM calls happen
between approval and execution. Proven directly here by swapping in a
client that raises on any call at the exact moment `approve()` is
invoked, for both the declared-workflow path (Scenario A) and the
free-form path (Scenario B): if either path ever called the model again,
these tests would fail with that client's exception instead of
completing.
"""

from __future__ import annotations

from harness.app import approve, tick
from harness.execution.workflows.reroute_po import ChooseSupplierResponse, DraftNotificationResponse
from harness.planning.llm import FakeLLMClient
from harness.planning.models import PlannerOutput, ToolCall, ToolPlan, WorkflowRequest


def test_workflow_path_makes_no_llm_calls_between_approval_and_execution(make_harness):
    h = make_harness("scenario_a")
    planning_llm = FakeLLMClient([
        PlannerOutput(proposal=WorkflowRequest(
            kind="workflow", workflow="reroute_po",
            params={
                "part_id": "P-4471", "original_po_id": "PO-77812", "prod_order_id": "4812",
                "qty": 120, "needed_by": "2026-09-07",
            },
            reasoning="Supplier Y slipped per M-001.", summary_for_user="Reroute to Z.",
        )),
        ChooseSupplierResponse(supplier_id="S-Z", justification="Only valid candidate."),
        DraftNotificationResponse(body="Reroute in progress."),
    ])
    tick(h.conn, h.clock, planning_llm)
    # Dana is OOO starting the very next day (E-002), so a single tick
    # (detect, plan, then escalate -- in that order) already escalates
    # this approval to her backup before returning.
    approval = h.conn.execute("SELECT approval_id, approver_id FROM approvals").fetchone()

    # FakeLLMClient([]) raises on its very first call; a client this
    # starved of scripted responses must never actually be asked anything.
    raising_llm = FakeLLMClient([])
    approve(h.conn, h.clock, raising_llm, approval_id=approval["approval_id"], decided_by=approval["approver_id"])

    instance = h.conn.execute("SELECT status FROM workflow_instances").fetchone()
    assert instance["status"] == "completed"
    assert h.conn.execute("SELECT COUNT(*) FROM erp_purchase_orders WHERE supplier_id = 'S-Z'").fetchone()[0] == 1


def test_free_form_path_makes_no_llm_calls_between_approval_and_execution(make_harness):
    h = make_harness("scenario_b_covers")
    planning_llm = FakeLLMClient([PlannerOutput(proposal=ToolPlan(
        kind="plan",
        steps=[
            ToolCall(tool="reallocate_lot", args={
                "prod_order_id": "4820", "part_id": "P-1180",
                "remove": [{"lot_id": "L-2093", "qty": 100}],
                "add": [{"lot_id": "L-2101", "qty": 70}, {"lot_id": "L-2115", "qty": 30}],
            }),
            ToolCall(tool="notify_user", args={
                "to_user": "u-301", "from_user": "u-202", "subject": "Reallocated", "body": "Done.",
            }),
        ],
        reasoning="L-2101 and L-2115 together cover the full 100 units needed.",
        summary_for_user="Reallocate 4820's coverage and notify production.",
    ))])
    tick(h.conn, h.clock, planning_llm)
    approval = h.conn.execute("SELECT approval_id, approver_id FROM approvals").fetchone()

    raising_llm = FakeLLMClient([])
    approve(h.conn, h.clock, raising_llm, approval_id=approval["approval_id"], decided_by=approval["approver_id"])

    allocations = dict(h.conn.execute(
        "SELECT lot_id, qty FROM erp_lot_allocations WHERE prod_order_id = '4820'"
    ).fetchall())
    assert allocations == {"L-2101": 70, "L-2115": 30}
    assert h.conn.execute("SELECT COUNT(*) FROM notifications").fetchone()[0] == 1
