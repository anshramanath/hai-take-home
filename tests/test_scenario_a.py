"""Part of section 15.12: Scenario A end to end, through approval and
execution. The Tuesday arrival-check follow-up depends on the scheduler
(phase 5) and is covered there.
"""

from __future__ import annotations

import json

from harness.app import approve, reject, tick
from harness.execution.workflows.reroute_po import ChooseSupplierResponse, DraftNotificationResponse
from harness.planning.llm import FakeLLMClient
from harness.planning.models import PlannerOutput, WorkflowRequest


def _reroute_llm() -> FakeLLMClient:
    return FakeLLMClient([
        PlannerOutput(proposal=WorkflowRequest(
            kind="workflow", workflow="reroute_po",
            params={
                "part_id": "P-4471", "original_po_id": "PO-77812", "prod_order_id": "4812",
                "qty": 120, "needed_by": "2026-09-07",
            },
            reasoning="Supplier Y (PO-77812) slipped to 9/8 per M-001; 4812 starts 9/7.",
            summary_for_user="Reroute part of PO-77812 to Supplier Z and notify production.",
        )),
        ChooseSupplierResponse(supplier_id="S-Z", justification="Only approved candidate meeting the need date."),
        DraftNotificationResponse(body="Heads up: part of your incoming shipment is being rerouted."),
    ])


def test_scenario_a_runs_from_detection_through_escalation_to_execution(make_harness):
    h = make_harness("scenario_a")
    llm = _reroute_llm()

    # Day 1 (9/2): detection, planning, workflow checks, approval request to Dana.
    tick1 = tick(h.conn, h.clock, llm)
    assert tick1["new_items"]
    approval = h.conn.execute("SELECT * FROM approvals").fetchone()
    assert approval["approver_id"] == "u-101"
    assert approval["status"] == "pending"

    instance = h.conn.execute("SELECT * FROM workflow_instances").fetchone()
    assert instance["status"] == "awaiting_approval"
    state = json.loads(instance["state"])
    assert state["candidates"] == ["S-Z"]
    assert "S-Q" in [e["supplier_id"] for e in state["excluded_by_approval"]]
    assert "S-W" in [e["supplier_id"] for e in state["excluded_by_lead_time"]]
    assert h.conn.execute("SELECT COUNT(*) FROM erp_purchase_orders WHERE supplier_id = 'S-Z'").fetchone()[0] == 0

    # No answer. Day 2 (9/3): Dana is OOO per E-002, so escalation routes to Priya.
    tick2 = tick(h.conn, h.clock, llm)
    assert approval["approval_id"] in tick2["escalated"]
    approval_after = h.conn.execute("SELECT * FROM approvals").fetchone()
    assert approval_after["approver_id"] == "u-102"
    assert approval_after["status"] == "pending"

    # Priya approves, through the same code path the approve CLI uses.
    approve(h.conn, h.clock, llm, approval_id=approval_after["approval_id"], decided_by="u-102")

    final_instance = h.conn.execute("SELECT * FROM workflow_instances").fetchone()
    assert final_instance["status"] == "completed"

    new_po = h.conn.execute(
        "SELECT po_id, qty, status FROM erp_purchase_orders WHERE supplier_id = 'S-Z'"
    ).fetchone()
    assert new_po["qty"] == 120
    assert new_po["status"] == "open"

    original_po = h.conn.execute(
        "SELECT qty, status FROM erp_purchase_orders WHERE po_id = 'PO-77812'"
    ).fetchone()
    assert original_po["qty"] == 280
    assert original_po["status"] == "open"

    notification = h.conn.execute("SELECT to_user FROM notifications").fetchone()
    assert notification["to_user"] == "u-301"

    task = h.conn.execute("SELECT kind, run_at FROM scheduled_tasks").fetchone()
    assert task["kind"] == "arrival_check"
    assert task["run_at"] == "2026-09-04"  # Z's promised date, not Tuesday

    run_row = h.conn.execute("SELECT status FROM runs").fetchone()
    assert run_row["status"] == "completed"


def test_scenario_a_decided_by_the_backup_is_attributed_to_the_backup(make_harness):
    h = make_harness("scenario_a")
    llm = _reroute_llm()

    tick(h.conn, h.clock, llm)
    tick(h.conn, h.clock, llm)  # escalates to u-102
    approval = h.conn.execute("SELECT approval_id FROM approvals").fetchone()
    approve(h.conn, h.clock, llm, approval_id=approval["approval_id"], decided_by="u-102")

    decided = h.conn.execute("SELECT decided_by FROM approvals").fetchone()
    assert decided["decided_by"] == "u-102"


def test_scenario_a_no_qualifying_supplier_reports_without_executing(make_harness):
    h = make_harness("scenario_a_no_supplier")
    llm = FakeLLMClient([
        PlannerOutput(proposal=WorkflowRequest(
            kind="workflow", workflow="reroute_po",
            params={
                "part_id": "P-4471", "original_po_id": "PO-77812", "prod_order_id": "4812",
                "qty": 120, "needed_by": "2026-09-07",
            },
            reasoning="Supplier Y slipped; attempting a reroute.",
            summary_for_user="Attempting to reroute to an approved alternate.",
        )),
    ])

    tick(h.conn, h.clock, llm)

    instance = h.conn.execute("SELECT status FROM workflow_instances").fetchone()
    assert instance["status"] == "halted_no_supplier"
    assert h.conn.execute("SELECT COUNT(*) FROM approvals").fetchone()[0] == 0
    assert h.conn.execute("SELECT COUNT(*) FROM executed_actions").fetchone()[0] == 0
    run_row = h.conn.execute("SELECT status FROM runs").fetchone()
    assert run_row["status"] == "halted_no_supplier"


def test_scenario_a_rejection_marks_the_run_and_instance_rejected_and_writes_nothing(make_harness):
    h = make_harness("scenario_a")
    llm = _reroute_llm()

    tick(h.conn, h.clock, llm)
    approval = h.conn.execute("SELECT approval_id FROM approvals").fetchone()

    reject(h.conn, h.clock, approval_id=approval["approval_id"], decided_by="u-101")

    assert h.conn.execute("SELECT status FROM approvals").fetchone()["status"] == "rejected"
    assert h.conn.execute("SELECT status FROM workflow_instances").fetchone()["status"] == "rejected"
    assert h.conn.execute("SELECT status FROM runs").fetchone()["status"] == "rejected"
    assert h.conn.execute("SELECT COUNT(*) FROM executed_actions").fetchone()[0] == 0


def test_scenario_a_over_limit_routes_approval_to_manager(make_harness):
    h = make_harness("scenario_a_over_limit")
    llm = FakeLLMClient([
        PlannerOutput(proposal=WorkflowRequest(
            kind="workflow", workflow="reroute_po",
            params={
                "part_id": "P-4471", "original_po_id": "PO-77812", "prod_order_id": "4812",
                "qty": 700, "needed_by": "2026-09-07",
            },
            reasoning="Large reroute needed; value will exceed Dana's own limit.",
            summary_for_user="Reroute a large quantity to Supplier Z.",
        )),
        ChooseSupplierResponse(supplier_id="S-Z", justification="Only approved candidate meeting the need date."),
        DraftNotificationResponse(body="Large reroute in progress."),
    ])

    tick(h.conn, h.clock, llm)

    approval = h.conn.execute("SELECT approver_id, routed_reason FROM approvals").fetchone()
    assert approval["approver_id"] == "u-100"
    assert approval["routed_reason"] is not None
