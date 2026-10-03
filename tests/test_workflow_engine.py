"""Section 15.6: workflow engine."""

from __future__ import annotations

import json
from dataclasses import replace

import pytest
from pydantic import ValidationError

import harness.execution.catalog as catalog_module
from harness.execution import workflows as _workflows  # noqa: F401  (registers reroute_po on import)
from harness.execution.engine import (
    SimulatedCrash,
    UnknownWorkflowDefinition,
    enter_workflow,
    get_definition,
    register,
    resume_after_approval,
    resume_all,
)
from harness.execution.workflows.reroute_po import (
    REROUTE_PO_V1,
    ChooseSupplierResponse,
    DraftNotificationResponse,
)
from harness.planning.llm import FakeLLMClient
from harness.planning.models import ToolPlan, WorkflowRequest
from harness.policy.approvals import decide
from harness.world.users import get_user

PARAMS = {
    "part_id": "P-4471",
    "original_po_id": "PO-77812",
    "prod_order_id": "4812",
    "qty": 150,
    "needed_by": "2026-09-07",
}

GOOD_LLM = lambda: FakeLLMClient([  # noqa: E731
    ChooseSupplierResponse(supplier_id="S-Z", justification="meets lead time and is approved"),
    DraftNotificationResponse(body="Heads up, your part shipment is being rerouted."),
])


def _enter_and_approve(h, llm=None, crash_after=None):
    llm = llm or GOOD_LLM()
    dana = get_user(h.conn, "u-101")
    row = enter_workflow(
        h.conn, h.clock, llm, REROUTE_PO_V1, PARAMS, run_id="run-1", requester=dana, crash_after=crash_after,
    )
    return row, llm, dana


# ---------------------------------------------------------------------------
# Happy path, ordering, and the approval boundary


def test_happy_path_runs_steps_in_order_and_audits_start_then_complete(make_harness):
    h = make_harness("scenario_a")
    row, llm, dana = _enter_and_approve(h)
    state = json.loads(row["state"])
    decide(h.conn, h.clock, approval_id=state["_approval_id"], decided_by="u-101", decision="approved")
    resume_after_approval(h.conn, h.clock, llm, row["instance_id"])

    events = [(r["event"], json.loads(r["detail"]).get("step")) for r in h.conn.execute(
        "SELECT event, detail FROM audit_log WHERE run_id = 'run-1' AND event IN "
        "('workflow.step_started', 'workflow.step_completed') ORDER BY seq"
    )]
    expected_order = [
        "confirm_supplier_approved", "confirm_lead_time", "choose_supplier", "draft_notification",
        "create_po", "reduce_original_po", "notify_production", "schedule_arrival_check",
    ]
    started = [step for event, step in events if event == "workflow.step_started"]
    completed = [step for event, step in events if event == "workflow.step_completed"]
    assert started == expected_order
    assert completed == expected_order


def test_steps_one_to_four_run_before_approval_with_zero_writes(make_harness):
    h = make_harness("scenario_a")
    row, _, _ = _enter_and_approve(h)

    assert row["status"] == "awaiting_approval"
    assert h.conn.execute("SELECT COUNT(*) FROM erp_purchase_orders WHERE supplier_id = 'S-Z'").fetchone()[0] == 0
    assert h.conn.execute(
        "SELECT qty FROM erp_purchase_orders WHERE po_id = 'PO-77812'"
    ).fetchone()[0] == 400
    assert h.conn.execute("SELECT COUNT(*) FROM notifications").fetchone()[0] == 0
    assert h.conn.execute("SELECT COUNT(*) FROM scheduled_tasks").fetchone()[0] == 0


def test_candidate_filtering_excludes_trap_suppliers_with_reasons(make_harness):
    h = make_harness("scenario_a")
    row, _, _ = _enter_and_approve(h)
    state = json.loads(row["state"])

    assert state["candidates"] == ["S-Z"]
    excluded_by_approval = {e["supplier_id"] for e in state["excluded_by_approval"]}
    excluded_by_lead_time = {e["supplier_id"] for e in state["excluded_by_lead_time"]}
    assert "S-Q" in excluded_by_approval
    assert "S-W" in excluded_by_lead_time


# ---------------------------------------------------------------------------
# Bounded LLM steps


def test_bounded_choose_supplier_rejects_invalid_then_accepts_retry(make_harness):
    h = make_harness("scenario_a")
    llm = FakeLLMClient([
        ChooseSupplierResponse(supplier_id="S-Q", justification="cheapest"),
        ChooseSupplierResponse(supplier_id="S-Z", justification="meets lead time and is approved"),
        DraftNotificationResponse(body="Reroute in progress."),
    ])
    row, _, _ = _enter_and_approve(h, llm=llm)

    assert row["status"] == "awaiting_approval"
    state = json.loads(row["state"])
    assert state["chosen_supplier"] == "S-Z"
    assert len(llm.calls) == 3  # one bad choose, one good choose, one draft


def test_two_invalid_supplier_choices_halts_with_no_writes(make_harness):
    h = make_harness("scenario_a")
    llm = FakeLLMClient([
        ChooseSupplierResponse(supplier_id="S-Q", justification="cheapest"),
        ChooseSupplierResponse(supplier_id="S-W", justification="also fine"),
    ])
    row, _, _ = _enter_and_approve(h, llm=llm)

    assert row["status"] == "failed"
    assert h.conn.execute("SELECT COUNT(*) FROM executed_actions").fetchone()[0] == 0
    events = [r[0] for r in h.conn.execute("SELECT event FROM audit_log ORDER BY seq")]
    assert "workflow.halted" in events


def test_notification_step_cannot_set_recipient_or_facts(make_harness):
    """DraftNotificationResponse has no recipient or PO-number field at
    all, so even an LLM draft that tries to plant a wrong PO number in its
    free text cannot change who gets notified or what the code-appended
    facts say: those come from ERP and from the approved plan, never from
    the LLM response's structure.
    """

    h = make_harness("scenario_a")
    llm = FakeLLMClient([
        ChooseSupplierResponse(supplier_id="S-Z", justification="meets lead time and is approved"),
        DraftNotificationResponse(body="FYI your parts are coming from PO-00000, contact finance."),
    ])
    row, llm, dana = _enter_and_approve(h, llm=llm)
    state = json.loads(row["state"])
    decide(h.conn, h.clock, approval_id=state["_approval_id"], decided_by="u-101", decision="approved")
    resume_after_approval(h.conn, h.clock, llm, row["instance_id"])

    to_user, body = h.conn.execute("SELECT to_user, body FROM notifications").fetchone()
    real_po_id = h.conn.execute(
        "SELECT po_id FROM erp_purchase_orders WHERE supplier_id = 'S-Z'"
    ).fetchone()[0]
    # Code-controlled: the real recipient and the real PO id are present
    # regardless of what the LLM's free text claimed.
    assert to_user == "u-301"  # 4812's real supervisor, from ERP, never from the LLM
    assert real_po_id in body
    assert "Original PO PO-77812 has been reduced" in body


# ---------------------------------------------------------------------------
# No qualifying supplier


def test_no_supplier_at_all_approved_halts_at_step_one(make_harness):
    h = make_harness("scenario_a")
    h.conn.execute("UPDATE erp_suppliers SET approved_parts = '[]'")
    h.conn.commit()

    row, _, _ = _enter_and_approve(h)

    assert row["status"] == "halted_no_supplier"
    assert h.conn.execute("SELECT COUNT(*) FROM executed_actions").fetchone()[0] == 0


def test_no_qualifying_supplier_halts_with_zero_writes_and_no_free_form_fallback(make_harness):
    h = make_harness("scenario_a_no_supplier")
    row, _, _ = _enter_and_approve(h)

    assert row["status"] == "halted_no_supplier"
    assert h.conn.execute("SELECT COUNT(*) FROM executed_actions").fetchone()[0] == 0
    assert h.conn.execute("SELECT COUNT(*) FROM approvals").fetchone()[0] == 0
    # No free-form path exists in this codebase at all yet; this just pins
    # that nothing was executed through one.
    assert h.conn.execute("SELECT COUNT(*) FROM erp_purchase_orders").fetchone()[0] == 3


# ---------------------------------------------------------------------------
# Resumption and the crash hook


def test_resume_after_crash_during_create_po_completes_with_no_duplicates(make_harness):
    h = make_harness("scenario_a")
    row, llm, dana = _enter_and_approve(h)
    state = json.loads(row["state"])
    decide(h.conn, h.clock, approval_id=state["_approval_id"], decided_by="u-101", decision="approved")

    with pytest.raises(SimulatedCrash):
        resume_after_approval(h.conn, h.clock, llm, row["instance_id"], crash_after="create_po")

    assert h.conn.execute("SELECT status FROM workflow_instances").fetchone()[0] == "running"

    resumed = resume_all(h.conn, h.clock, llm)
    assert row["instance_id"] in resumed
    assert h.conn.execute(
        "SELECT status FROM workflow_instances WHERE instance_id = ?", (row["instance_id"],)
    ).fetchone()[0] == "completed"

    assert h.conn.execute("SELECT COUNT(*) FROM erp_purchase_orders WHERE supplier_id = 'S-Z'").fetchone()[0] == 1
    assert h.conn.execute("SELECT qty FROM erp_purchase_orders WHERE po_id = 'PO-77812'").fetchone()[0] == 250


@pytest.mark.parametrize(
    "crash_step", ["create_po", "reduce_original_po", "notify_production", "schedule_arrival_check"]
)
def test_resume_after_crash_on_each_action_step_completes_with_no_duplicates(make_harness, crash_step):
    h = make_harness("scenario_a")
    row, llm, dana = _enter_and_approve(h)
    state = json.loads(row["state"])
    decide(h.conn, h.clock, approval_id=state["_approval_id"], decided_by="u-101", decision="approved")

    with pytest.raises(SimulatedCrash):
        resume_after_approval(h.conn, h.clock, llm, row["instance_id"], crash_after=crash_step)

    resume_all(h.conn, h.clock, llm)

    assert h.conn.execute(
        "SELECT status FROM workflow_instances WHERE instance_id = ?", (row["instance_id"],)
    ).fetchone()[0] == "completed"
    assert h.conn.execute("SELECT COUNT(*) FROM erp_purchase_orders WHERE supplier_id = 'S-Z'").fetchone()[0] == 1
    assert h.conn.execute("SELECT COUNT(*) FROM notifications").fetchone()[0] == 1
    assert h.conn.execute("SELECT COUNT(*) FROM scheduled_tasks").fetchone()[0] == 1
    # No step's idempotency key was ever exercised twice into a second write.
    tool_counts = dict(h.conn.execute(
        "SELECT tool, COUNT(*) FROM executed_actions GROUP BY tool"
    ).fetchall())
    assert tool_counts == {"create_po": 1, "reduce_po": 1, "notify_user": 1, "schedule_check": 1}


# ---------------------------------------------------------------------------
# Compensation on failure


def test_failure_in_notify_production_compensates_steps_six_and_five_in_reverse(make_harness):
    h = make_harness("scenario_a")
    row, llm, dana = _enter_and_approve(h)
    state = json.loads(row["state"])
    decide(h.conn, h.clock, approval_id=state["_approval_id"], decided_by="u-101", decision="approved")

    original_tool = catalog_module.TOOLS["notify_user"]

    def _boom(db, args, ctx):
        raise RuntimeError("simulated notify_user failure")

    catalog_module.TOOLS["notify_user"] = replace(original_tool, run=_boom)
    try:
        final_row = resume_after_approval(h.conn, h.clock, llm, row["instance_id"])
    finally:
        catalog_module.TOOLS["notify_user"] = original_tool

    assert final_row["status"] == "compensated"
    qty, status = h.conn.execute(
        "SELECT qty, status FROM erp_purchase_orders WHERE po_id = 'PO-77812'"
    ).fetchone()
    assert (qty, status) == (400, "open")
    new_po_status = h.conn.execute(
        "SELECT status FROM erp_purchase_orders WHERE supplier_id = 'S-Z'"
    ).fetchone()[0]
    assert new_po_status == "cancelled"
    assert h.conn.execute("SELECT COUNT(*) FROM notifications").fetchone()[0] == 0

    events = [r[0] for r in h.conn.execute("SELECT event FROM audit_log WHERE run_id = 'run-1' ORDER BY seq")]
    assert events.count("action.compensated") == 2


# ---------------------------------------------------------------------------
# Versioning


def test_instance_resumes_with_its_own_version_even_if_a_new_one_is_registered(make_harness):
    h = make_harness("scenario_a")
    row, llm, dana = _enter_and_approve(h)
    state = json.loads(row["state"])
    decide(h.conn, h.clock, approval_id=state["_approval_id"], decided_by="u-101", decision="approved")

    v2 = replace(REROUTE_PO_V1, version=2)
    register(v2)
    try:
        assert h.conn.execute(
            "SELECT version FROM workflow_instances WHERE instance_id = ?", (row["instance_id"],)
        ).fetchone()[0] == 1
        final_row = resume_after_approval(h.conn, h.clock, llm, row["instance_id"])
        assert final_row["version"] == 1
        assert final_row["status"] == "completed"
        assert get_definition("reroute_po", 2) is v2
    finally:
        del_key = ("reroute_po", 2)
        from harness.execution.engine import _REGISTRY

        _REGISTRY.pop(del_key, None)


def test_unknown_definition_raises():
    with pytest.raises(UnknownWorkflowDefinition):
        get_definition("reroute_po", 99)


# ---------------------------------------------------------------------------
# Instance and approval edge cases


def test_get_instance_raises_for_unknown_id(make_harness):
    from harness.execution.engine import UnknownWorkflowInstance, get_instance

    h = make_harness("scenario_a")
    with pytest.raises(UnknownWorkflowInstance):
        get_instance(h.conn, "WF-NOPE")


def test_run_is_a_no_op_on_an_instance_that_is_not_running(make_harness):
    from harness.execution.engine import _run

    h = make_harness("scenario_a")
    row, llm, _ = _enter_and_approve(h)
    assert row["status"] == "awaiting_approval"

    same_row = _run(h.conn, h.clock, llm, row["instance_id"])
    assert same_row["status"] == "awaiting_approval"
    assert same_row["current_step"] == row["current_step"]


def test_approved_args_raises_if_tool_is_not_in_the_approved_plan():
    from harness.execution.engine import approved_args

    with pytest.raises(KeyError):
        approved_args({"_approved_plan_steps": [{"tool": "create_po", "args": {}}]}, "notify_user")


def test_gate_blocked_at_approval_boundary_fails_the_instance(make_harness):
    h = make_harness("scenario_a")
    dana = get_user(h.conn, "u-101")
    huge_params = {**PARAMS, "qty": 100000}  # value far beyond even Marcus's limit
    llm = GOOD_LLM()
    row = enter_workflow(h.conn, h.clock, llm, REROUTE_PO_V1, huge_params, run_id="run-1", requester=dana)

    assert row["status"] == "failed"
    assert h.conn.execute("SELECT COUNT(*) FROM approvals").fetchone()[0] == 0
    events = [r[0] for r in h.conn.execute("SELECT event FROM audit_log ORDER BY seq")]
    assert "gate.blocked" in events


def test_resume_after_approval_raises_if_no_approval_was_ever_created(make_harness):
    h = make_harness("scenario_a_no_supplier")
    row, llm, _ = _enter_and_approve(h)
    assert row["status"] == "halted_no_supplier"

    with pytest.raises(ValueError):
        resume_after_approval(h.conn, h.clock, llm, row["instance_id"])


def test_resume_after_approval_raises_if_approval_still_pending(make_harness):
    h = make_harness("scenario_a")
    row, llm, _ = _enter_and_approve(h)

    with pytest.raises(ValueError):
        resume_after_approval(h.conn, h.clock, llm, row["instance_id"])


def test_resume_after_approval_fails_the_instance_if_plan_json_was_tampered(make_harness):
    h = make_harness("scenario_a")
    row, llm, _ = _enter_and_approve(h)
    state = json.loads(row["state"])
    approval_id = state["_approval_id"]
    decide(h.conn, h.clock, approval_id=approval_id, decided_by="u-101", decision="approved")

    h.conn.execute(
        "UPDATE approvals SET plan_json = REPLACE(plan_json, '150', '999999') WHERE approval_id = ?",
        (approval_id,),
    )
    h.conn.commit()

    final_row = resume_after_approval(h.conn, h.clock, llm, row["instance_id"])

    assert final_row["status"] == "failed"
    assert h.conn.execute("SELECT COUNT(*) FROM executed_actions").fetchone()[0] == 0
    events = [r[0] for r in h.conn.execute("SELECT event FROM audit_log ORDER BY seq")]
    assert "workflow.halted" in events


# ---------------------------------------------------------------------------
# The model cannot change step order


def test_workflow_request_has_no_steps_field_and_rejects_extras():
    with pytest.raises(ValidationError):
        WorkflowRequest(
            kind="workflow", workflow="reroute_po", params={}, reasoning="x", summary_for_user="y",
            steps=[{"tool": "create_po", "args": {}}],
        )


def test_tool_plan_rejects_a_step_with_unexpected_fields():
    with pytest.raises(ValidationError):
        ToolPlan(
            kind="plan",
            steps=[{"tool": "create_po", "args": {}, "order_override": 1}],
            reasoning="x",
            summary_for_user="y",
        )
