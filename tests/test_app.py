"""Orchestration-level behavior of `handle_attention_item`/`tick()`
(harness/app.py) that the scenario-specific test files don't exercise:
the two early-return branches when something goes wrong before a workflow
or approval is ever involved. `propose()` raising `PlannerFailed` and
`gate()` returning `Blocked` are both unit-tested in isolation
(test_planner.py, test_gate.py); these tests instead drive the whole
`tick()` path to confirm the orchestration code around them actually
catches the failure and records it, rather than crashing the tick or
silently leaving the run in a non-terminal state.
"""

from __future__ import annotations

from harness.app import tick
from harness.planning.llm import FakeLLMClient, LLMOutputInvalid
from harness.planning.models import NoAction, PlannerOutput, ToolCall, ToolPlan


def test_a_planner_failure_marks_the_run_failed_without_crashing_the_tick(make_harness):
    h = make_harness("scenario_a")
    llm = FakeLLMClient([LLMOutputInvalid("bad json"), LLMOutputInvalid("bad json again")])

    result = tick(h.conn, h.clock, llm)  # must not raise

    assert result["runs"]
    run = h.conn.execute("SELECT status, state FROM runs WHERE run_id = ?", (result["runs"][0],)).fetchone()
    assert run["status"] == "failed"
    assert "bad json again" in run["state"]
    events = [r[0] for r in h.conn.execute("SELECT event FROM audit_log")]
    assert "planner.invalid" in events


def test_a_free_form_gate_rejection_marks_the_run_failed_with_the_reason(make_harness):
    """Omar's own planner wouldn't normally be shown create_po at all
    (prompt.py filters it out, being workflow-only), but the gate is what
    actually has to block it if a model proposes it anyway; this proves
    tick()'s orchestration records that rejection rather than assuming the
    prompt alone is enough.
    """

    h = make_harness("scenario_b_covers")
    llm = FakeLLMClient([PlannerOutput(proposal=ToolPlan(
        kind="plan",
        steps=[{"tool": "create_po", "args": {
            "po_id": "PO-X", "part_id": "P-1180", "supplier_id": "S-Z", "qty": 10,
            "unit_price": 1.0, "needed_by": "2026-09-05", "created_by": "u-202",
        }}],
        reasoning="Invalid: create_po is workflow-only.",
        summary_for_user="This should never be approvable.",
    ))])

    result = tick(h.conn, h.clock, llm)

    assert result["runs"]
    run = h.conn.execute("SELECT status, state FROM runs WHERE run_id = ?", (result["runs"][0],)).fetchone()
    assert run["status"] == "failed"
    assert "workflow-only" in run["state"]
    assert h.conn.execute("SELECT COUNT(*) FROM approvals").fetchone()[0] == 0
    assert h.conn.execute("SELECT COUNT(*) FROM executed_actions").fetchone()[0] == 0


def _notify_only_proposal(note: str) -> PlannerOutput:
    return PlannerOutput(proposal=ToolPlan(
        kind="plan",
        steps=[ToolCall(tool="notify_user", args={
            "to_user": "u-301", "from_user": "u-202", "subject": "Lot on hold", "body": note,
        })],
        reasoning="Lot L-2093 is on hold; notifying production.",
        summary_for_user="Notify production about the hold.",
    ))


def _flag_shortage_proposal() -> PlannerOutput:
    return PlannerOutput(proposal=ToolPlan(
        kind="plan",
        steps=[ToolCall(tool="flag_shortage", args={
            "part_id": "P-1180", "prod_order_id": "4820", "qty_short": 10,
            "summary": "Insufficient released stock for 4820.",
        })],
        reasoning="No combination of released lots covers the full 100 units.",
        summary_for_user="Flag the shortage to purchasing.",
    ))


def test_a_retryable_gate_rejection_is_reproposed_once_and_can_succeed(make_harness):
    """The gate's new all-non-resolving-tools rule (notify_user alone)
    is retryable: tick() must re-plan exactly once, and if the second
    proposal actually resolves the item, the run proceeds to approval.
    """
    h = make_harness("scenario_b_shortage")
    llm = FakeLLMClient([_notify_only_proposal("first attempt"), _flag_shortage_proposal()])

    result = tick(h.conn, h.clock, llm)

    assert result["runs"]
    run = h.conn.execute("SELECT status FROM runs WHERE run_id = ?", (result["runs"][0],)).fetchone()
    assert run["status"] == "awaiting_approval"
    assert h.conn.execute("SELECT COUNT(*) FROM approvals").fetchone()[0] == 1

    events = [r[0] for r in h.conn.execute("SELECT event, detail FROM audit_log ORDER BY seq").fetchall()]
    assert events.count("planner.proposed") == 2
    assert "gate.blocked" in events
    assert "gate.allowed" in events


def test_a_retryable_gate_rejection_fails_after_exactly_one_retry_if_still_inadequate(make_harness):
    h = make_harness("scenario_b_shortage")
    llm = FakeLLMClient([_notify_only_proposal("first attempt"), _notify_only_proposal("second attempt")])

    result = tick(h.conn, h.clock, llm)

    assert result["runs"]
    run = h.conn.execute("SELECT status FROM runs WHERE run_id = ?", (result["runs"][0],)).fetchone()
    assert run["status"] == "failed"
    assert h.conn.execute("SELECT COUNT(*) FROM approvals").fetchone()[0] == 0

    detail_rows = [r[0] for r in h.conn.execute(
        "SELECT detail FROM audit_log WHERE event = 'planner.proposed' ORDER BY seq"
    ).fetchall()]
    assert len(detail_rows) == 2  # original attempt + exactly one retry, never a third


def test_gate_allowed_is_audited_for_a_straightforward_free_form_plan(make_harness):
    """Section 13 requires gate.allowed/gate.blocked as audit events; the
    free-form path previously never logged either -- only the declared
    workflow path's execution-time re-check did.
    """
    h = make_harness("scenario_b_shortage")
    llm = FakeLLMClient([_flag_shortage_proposal()])

    tick(h.conn, h.clock, llm)

    events = [r[0] for r in h.conn.execute("SELECT event FROM audit_log ORDER BY seq").fetchall()]
    assert "gate.allowed" in events


def _no_action_proposal(note: str) -> PlannerOutput:
    return PlannerOutput(proposal=NoAction(
        kind="none",
        reasoning=f"Other lots can cover this ({note}), so no action is needed.",
    ))


def test_a_no_action_is_reproposed_once_when_the_user_has_a_resolving_tool_and_can_succeed(make_harness):
    """A real model, on the covers fixture, repeatedly reasoned its way to
    'other lots can cover this' and then proposed NoAction anyway --
    confirming a fix is possible is not the same as it happening. Omar
    has erp:lot:allocate, so this is retried once.
    """
    h = make_harness("scenario_b_covers")
    llm = FakeLLMClient([_no_action_proposal("first attempt"), _flag_shortage_proposal()])

    result = tick(h.conn, h.clock, llm)

    assert result["runs"]
    run = h.conn.execute("SELECT status FROM runs WHERE run_id = ?", (result["runs"][0],)).fetchone()
    assert run["status"] == "awaiting_approval"
    events = [r[0] for r in h.conn.execute("SELECT event FROM audit_log ORDER BY seq").fetchall()]
    assert events.count("planner.proposed") == 2
    assert "planner.no_action_retried" in events


def test_a_no_action_is_accepted_after_exactly_one_retry_if_reconfirmed(make_harness):
    h = make_harness("scenario_b_covers")
    llm = FakeLLMClient([_no_action_proposal("first attempt"), _no_action_proposal("second attempt")])

    result = tick(h.conn, h.clock, llm)

    assert result["runs"]
    run = h.conn.execute("SELECT status FROM runs WHERE run_id = ?", (result["runs"][0],)).fetchone()
    assert run["status"] == "closed"
    detail_rows = [r[0] for r in h.conn.execute(
        "SELECT detail FROM audit_log WHERE event = 'planner.proposed' ORDER BY seq"
    ).fetchall()]
    assert len(detail_rows) == 2  # original attempt + exactly one retry, never a third


def test_a_no_action_is_never_retried_for_a_user_with_no_resolving_tool(make_harness):
    """Dana's 'recommend only' handoff (test_scenario_b.py) is the real
    case this guards: she has no resolving tool available at all (no
    erp:lot:allocate, no purchasing:flag, every PO tool workflow-only), so
    her NoAction is the correct, final answer -- not something to retry.
    """
    h = make_harness("scenario_a")
    llm = FakeLLMClient([_no_action_proposal("first attempt")])

    result = tick(h.conn, h.clock, llm)

    assert result["runs"]
    run = h.conn.execute("SELECT status FROM runs WHERE run_id = ?", (result["runs"][0],)).fetchone()
    assert run["status"] == "closed"
    events = [r[0] for r in h.conn.execute("SELECT event FROM audit_log ORDER BY seq").fetchall()]
    assert events.count("planner.proposed") == 1
    assert "planner.no_action_retried" not in events
    assert "gate.blocked" not in events
