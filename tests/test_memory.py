"""Section 15.11: memory."""

from __future__ import annotations

import json

from harness.memory.facts import facts_for_prompt, write_fact


def test_write_fact_records_source_ids_and_is_audited(make_harness):
    h = make_harness("scenario_a")
    fact_id = write_fact(
        h.conn, h.clock, subject="S-Y", fact="S-Y slipped PO-77812 from 9/4 to 9/8.", source_ids=["M-001"],
    )

    row = h.conn.execute("SELECT subject, fact, source_ids FROM memory_facts WHERE fact_id = ?", (fact_id,)).fetchone()
    assert row["subject"] == "S-Y"
    assert json.loads(row["source_ids"]) == ["M-001"]

    events = [r[0] for r in h.conn.execute("SELECT event FROM audit_log")]
    assert "memory.fact_written" in events


def test_expired_facts_are_not_included_in_the_prompt(make_harness):
    h = make_harness("scenario_a")
    write_fact(
        h.conn, h.clock, subject="S-Y", fact="still relevant", source_ids=["M-001"], expires_in_days=10,
    )
    write_fact(
        h.conn, h.clock, subject="S-Q", fact="stale info", source_ids=["M-002"], expires_in_days=1,
    )

    h.clock.advance(5)  # the second fact's expiry (1 day out) is now in the past

    facts = facts_for_prompt(h.conn, h.clock)
    subjects = {f["subject"] for f in facts}

    assert subjects == {"S-Y"}


def test_facts_with_no_expiry_never_filtered(make_harness):
    h = make_harness("scenario_a")
    write_fact(h.conn, h.clock, subject="S-Z", fact="reliable so far", source_ids=["PO-1"])

    h.clock.advance(365)

    facts = facts_for_prompt(h.conn, h.clock)
    assert any(f["subject"] == "S-Z" for f in facts)


def test_a_misleading_memory_fact_does_not_change_gate_or_workflow_results(make_harness):
    """Memory is a hint shown to the model, never a substitute for the gate
    or the workflow reading live data themselves. Plant a fact claiming
    S-Z is slow and unapproved; the workflow must still find S-Z through
    its own ERP checks regardless of what the "hint" says.
    """

    from harness.execution.engine import enter_workflow
    from harness.execution.workflows.reroute_po import (
        REROUTE_PO_V1,
        ChooseSupplierResponse,
        DraftNotificationResponse,
    )
    from harness.planning.llm import FakeLLMClient
    from harness.world.users import get_user

    h = make_harness("scenario_a")
    write_fact(
        h.conn, h.clock, subject="S-Z",
        fact="S-Z is not approved for P-4471 and has a 10-day lead time.",  # false
        source_ids=["M-999"],
    )
    dana = get_user(h.conn, "u-101")
    llm = FakeLLMClient([
        ChooseSupplierResponse(supplier_id="S-Z", justification="Meets the need date per the real ERP data."),
        DraftNotificationResponse(body="Reroute in progress."),
    ])

    row = enter_workflow(
        h.conn, h.clock, llm, REROUTE_PO_V1,
        {"part_id": "P-4471", "original_po_id": "PO-77812", "prod_order_id": "4812", "qty": 120, "needed_by": "2026-09-07"},
        run_id="run-1", requester=dana,
    )

    # The workflow's own checks (real ERP data) still find S-Z, unaffected
    # by the planted fact claiming otherwise.
    state = json.loads(row["state"])
    assert state["candidates"] == ["S-Z"]
    assert row["status"] == "awaiting_approval"


def test_scenario_a_completion_writes_a_fact_only_after_approval_not_before(make_harness):
    """Facts are written on confirmed outcomes: entering a workflow and
    reaching awaiting_approval is not yet confirmed (it's a proposal still
    pending a human); only execution after approval is.
    """

    from harness.app import approve, tick
    from harness.execution.workflows.reroute_po import ChooseSupplierResponse, DraftNotificationResponse
    from harness.planning.llm import FakeLLMClient
    from harness.planning.models import PlannerOutput, WorkflowRequest

    h = make_harness("scenario_a")
    llm = FakeLLMClient([
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

    tick(h.conn, h.clock, llm)
    assert h.conn.execute("SELECT COUNT(*) FROM memory_facts").fetchone()[0] == 0

    approval = h.conn.execute("SELECT approval_id FROM approvals").fetchone()
    approve(h.conn, h.clock, llm, approval_id=approval["approval_id"], decided_by="u-101")

    fact = h.conn.execute("SELECT subject, fact, source_ids FROM memory_facts").fetchone()
    assert fact["subject"] == "S-Y"
    assert json.loads(fact["source_ids"]) == ["M-001"]
