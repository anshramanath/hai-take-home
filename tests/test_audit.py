"""Section 15.2: audit. The append-only trigger tests live in
test_world.py, since schema.sql (where the trigger lives) was a phase 1
deliverable. The `explain` renderer does not exist until phase 5, so the
"explain reads only from audit" case is deferred there.
"""

from __future__ import annotations

import json

from harness.audit.log import all_events, events_for_run, log


def test_log_inserts_ordered_rows_with_run_actor_event_detail(make_harness):
    h = make_harness("scenario_a")
    log(h.conn, h.clock, run_id="run-1", actor="detector", event="detection.raised", detail={"a": 1})
    log(h.conn, h.clock, run_id="run-1", actor="planner", event="planner.proposed", detail={"b": 2})
    log(h.conn, h.clock, run_id="run-2", actor="detector", event="detection.raised", detail={"c": 3})
    h.conn.commit()

    run1_events = events_for_run(h.conn, "run-1")
    assert [row["event"] for row in run1_events] == ["detection.raised", "planner.proposed"]
    assert [row["seq"] for row in run1_events] == sorted(row["seq"] for row in run1_events)
    assert run1_events[0]["actor"] == "detector"
    assert json.loads(run1_events[0]["detail"]) == {"a": 1}
    assert run1_events[0]["ts"] == "2026-09-02"

    assert len(events_for_run(h.conn, "run-2")) == 1
    assert len(all_events(h.conn)) == 3


def test_mail_body_text_never_appears_in_audit_detail(make_harness):
    """T10 (Tier 2): context.gathered logs record_ids per source, never
    record content (harness/context/registry.py); nothing else in the
    system touches mail_messages.body at all. Running Scenario A through
    approval and execution and scanning every audit_log.detail for M-001's
    literal body text proves the mechanism, not just one call site.
    """

    from harness.app import approve, tick
    from harness.execution.workflows.reroute_po import ChooseSupplierResponse, DraftNotificationResponse
    from harness.planning.llm import FakeLLMClient
    from harness.planning.models import PlannerOutput, WorkflowRequest

    h = make_harness("scenario_a")
    mail_body = h.conn.execute(
        "SELECT body FROM mail_messages WHERE message_id = 'M-001'"
    ).fetchone()[0]
    assert mail_body  # sanity: the fixture actually has body text to look for

    llm = FakeLLMClient([
        PlannerOutput(proposal=WorkflowRequest(
            kind="workflow", workflow="reroute_po",
            params={
                "part_id": "P-4471", "original_po_id": "PO-77812", "prod_order_id": "4812",
                "qty": 120, "needed_by": "2026-09-07",
            },
            reasoning="Supplier Y (PO-77812) slipped per M-001; 4812 starts 9/7.",
            summary_for_user="Reroute part of PO-77812 to an approved alternate supplier.",
        )),
        ChooseSupplierResponse(supplier_id="S-Z", justification="Only approved candidate meeting the need date."),
        DraftNotificationResponse(body="Heads up: part of your incoming shipment is being rerouted."),
    ])
    tick(h.conn, h.clock, llm)
    approval = h.conn.execute("SELECT approval_id, approver_id FROM approvals").fetchone()
    approve(h.conn, h.clock, llm, approval_id=approval["approval_id"], decided_by=approval["approver_id"])

    all_detail_text = " ".join(
        row[0] for row in h.conn.execute("SELECT detail FROM audit_log")
    )
    assert mail_body not in all_detail_text
