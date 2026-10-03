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
from harness.planning.models import PlannerOutput, ToolPlan


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
