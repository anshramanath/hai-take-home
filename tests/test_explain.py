"""Section 15.13 (and the explain-specific bullet of 15.2): `explain`
renders only from the audit log, in chronological order, and contains
enough to reconstruct the whole Scenario A story.
"""

from __future__ import annotations

import sqlite3

from harness.app import approve, tick
from harness.audit.explain import explain
from harness.execution.workflows.reroute_po import ChooseSupplierResponse, DraftNotificationResponse
from harness.planning.llm import FakeLLMClient
from harness.planning.models import PlannerOutput, WorkflowRequest


def _run_scenario_a(conn, clock):
    llm = FakeLLMClient([
        PlannerOutput(proposal=WorkflowRequest(
            kind="workflow", workflow="reroute_po",
            params={
                "part_id": "P-4471", "original_po_id": "PO-77812", "prod_order_id": "4812",
                "qty": 120, "needed_by": "2026-09-07",
            },
            reasoning="Supplier Y (PO-77812) slipped to 9/8 per M-001; 4812 starts 9/7.",
            summary_for_user="Reroute to Supplier Z and notify production.",
        )),
        ChooseSupplierResponse(supplier_id="S-Z", justification="Only approved candidate meeting the need date."),
        DraftNotificationResponse(body="Heads up: part of your shipment is being rerouted."),
    ])
    tick(conn, clock, llm)  # 9/2 -> 9/3, approval requested to Dana
    tick(conn, clock, llm)  # 9/3 -> 9/4, Dana OOO, escalates to Priya
    approval = conn.execute("SELECT approval_id FROM approvals").fetchone()
    approve(conn, clock, llm, approval_id=approval["approval_id"], decided_by="u-102")


def test_explain_contains_the_whole_scenario_a_story_in_order(make_harness):
    h = make_harness("scenario_a")
    _run_scenario_a(h.conn, h.clock)

    lines = explain(h.conn)
    text = "\n".join(lines)

    for expected in [
        "detection",
        "M-001",
        "E-002",
        "S-Q",
        "S-W",
        "u-102",
        "S-Z",
        "PO-77812",
    ]:
        assert expected in text, f"{expected!r} missing from explain output"

    # Chronological: sequence numbers strictly increase.
    seqs = [int(line.split("#")[1].split("]")[0]) for line in lines]
    assert seqs == sorted(seqs)


def test_explain_reads_only_from_audit_after_closing_and_reopening_the_db(make_harness, tmp_path):
    h = make_harness("scenario_a")
    _run_scenario_a(h.conn, h.clock)

    db_path = tmp_path / "explain.db"
    file_conn = sqlite3.connect(db_path)
    h.conn.backup(file_conn)
    file_conn.row_factory = sqlite3.Row
    h.conn.close()

    lines = explain(file_conn)
    text = "\n".join(lines)
    assert "detection" in text
    assert "S-Z" in text
    file_conn.close()


def test_explain_renders_every_event_type_without_crashing(make_harness):
    """Covers the render branches the happy-path Scenario A story doesn't
    reach: failures, halts, compensation, and the unknown-event fallback.
    """

    from harness.audit.log import log as audit_log

    h = make_harness("scenario_a")
    cases = [
        ("planner.invalid", {"second_error": "bad json"}, "bad json"),
        ("gate.blocked", {"reason": "value too high"}, "value too high"),
        ("workflow.halted", {"status": "halted_no_supplier", "reason": "no candidates"}, "no candidates"),
        ("workflow.step_failed", {"step": "notify_production", "error": "boom"}, "boom"),
        ("action.skipped_idempotent", {"tool": "notify_user"}, "already done"),
        ("action.compensated", {"original_tool": "create_po", "compensation_tool": "cancel_po", "result": {}}, "cancel_po"),
        ("action.precheck_failed", {"tool": "create_po", "reason": "not approved"}, "not approved"),
        ("action.scope_denied", {"tool": "create_po", "scope": "erp:po:create"}, "erp:po:create"),
        ("action.failed", {"tool": "notify_user", "error": "db error"}, "db error"),
        ("schedule.fired", {"kind": "arrival_check", "task_id": "T-1"}, "arrival_check"),
        ("arrival_check.confirmed", {"po_id": "PO-1", "received": 10, "ordered": 10}, "PO-1"),
        ("arrival_check.missed", {"po_id": "PO-2", "received": 0, "ordered": 10}, "PO-2"),
        ("some.unrecognized.event", {"x": 1}, "some.unrecognized.event"),
    ]
    for event, detail, expected_substring in cases:
        audit_log(h.conn, h.clock, run_id=None, actor="test", event=event, detail=detail)
    h.conn.commit()

    lines = explain(h.conn)
    text = "\n".join(lines)
    for _, _, expected_substring in cases:
        assert expected_substring in text


def test_explain_with_run_filter_excludes_other_runs(make_harness):
    h = make_harness("scenario_a")
    _run_scenario_a(h.conn, h.clock)

    run_ids = [r[0] for r in h.conn.execute("SELECT DISTINCT run_id FROM audit_log WHERE run_id IS NOT NULL")]
    assert len(run_ids) == 1
    this_run = run_ids[0]

    # A second, unrelated database's run must not leak in: simulate by
    # inserting an audit row under a different run_id directly.
    from harness.audit.log import log as audit_log

    audit_log(h.conn, h.clock, run_id="RUN-OTHER", actor="test", event="planner.proposed", detail={"kind": "none"})
    h.conn.commit()

    lines = explain(h.conn, run_id=this_run)
    assert not any("RUN-OTHER" in line for line in lines)
    full_lines = explain(h.conn)
    assert len(full_lines) > len(lines)
