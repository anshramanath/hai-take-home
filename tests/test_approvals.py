"""Section 15.5: approvals and escalation."""

from __future__ import annotations

import json

import pytest

from harness.planning.models import ToolCall
from harness.policy.approvals import (
    ApprovalAlreadyDecided,
    NotCurrentApprover,
    UnknownApproval,
    create_approval,
    decide,
    escalate_pending,
    get_approval,
    plan_hash,
    verify_plan_hash,
)
from harness.world.users import get_user


def _reroute_steps(qty: int = 150, unit_price: float = 46.50) -> list[ToolCall]:
    return [
        ToolCall(
            tool="create_po",
            args={
                "po_id": "PO-TEST",
                "part_id": "P-4471",
                "supplier_id": "S-Z",
                "qty": qty,
                "unit_price": unit_price,
                "needed_by": "2026-09-04",
                "created_by": "u-101",
            },
        )
    ]


def test_approval_stores_canonical_plan_json_and_matching_hash(make_harness):
    h = make_harness("scenario_a")
    dana = get_user(h.conn, "u-101")
    approval_id = create_approval(
        h.conn, h.clock, run_id="run-1", requester=dana, steps=_reroute_steps(),
        approver_id="u-101", routed_reason=None, workflow="workflow:reroute_po",
    )
    row = get_approval(h.conn, approval_id)
    assert verify_plan_hash(row["plan_json"], row["plan_hash"])

    events = [r[0] for r in h.conn.execute("SELECT event FROM audit_log ORDER BY seq")]
    assert "approval.requested" in events


def test_hash_is_identical_regardless_of_dict_key_order_or_numeric_literal_form(make_harness):
    """T3 (Tier 2): `json.dumps(..., sort_keys=True)` is what makes the
    hash a function of the plan's actual content, not of the order some
    code happened to build the dict in, or which literal (46.5 vs 46.50)
    produced the same float.
    """

    h = make_harness("scenario_a")
    dana = get_user(h.conn, "u-101")
    steps_a = [ToolCall(tool="create_po", args={
        "po_id": "PO-TEST", "part_id": "P-4471", "supplier_id": "S-Z",
        "qty": 150, "unit_price": 46.5, "needed_by": "2026-09-04", "created_by": "u-101",
    })]
    steps_b = [ToolCall(tool="create_po", args={
        "created_by": "u-101", "needed_by": "2026-09-04", "unit_price": 46.50,
        "supplier_id": "S-Z", "qty": 150, "part_id": "P-4471", "po_id": "PO-TEST",
    })]

    approval_a = create_approval(
        h.conn, h.clock, run_id="run-a", requester=dana, steps=steps_a,
        approver_id="u-101", routed_reason=None, workflow="workflow:reroute_po",
    )
    approval_b = create_approval(
        h.conn, h.clock, run_id="run-b", requester=dana, steps=steps_b,
        approver_id="u-101", routed_reason=None, workflow="workflow:reroute_po",
    )

    row_a, row_b = get_approval(h.conn, approval_a), get_approval(h.conn, approval_b)
    assert row_a["plan_json"] == row_b["plan_json"]
    assert row_a["plan_hash"] == row_b["plan_hash"]


def test_hash_survives_a_json_and_db_round_trip(make_harness):
    h = make_harness("scenario_a")
    dana = get_user(h.conn, "u-101")
    approval_id = create_approval(
        h.conn, h.clock, run_id="run-1", requester=dana, steps=_reroute_steps(),
        approver_id="u-101", routed_reason=None, workflow="workflow:reroute_po",
    )
    stored = get_approval(h.conn, approval_id)

    # Simulate exactly what resume_after_approval does: pull plan_json
    # back out of the DB as plain TEXT, parse it, and recompute the hash
    # from scratch instead of trusting the stored one.
    round_tripped = json.loads(stored["plan_json"])
    recomputed_json = json.dumps(round_tripped, sort_keys=True, separators=(",", ":"))
    assert recomputed_json == stored["plan_json"]
    assert plan_hash(recomputed_json) == stored["plan_hash"]


def test_tampering_with_plan_json_breaks_the_hash(make_harness):
    h = make_harness("scenario_a")
    dana = get_user(h.conn, "u-101")
    approval_id = create_approval(
        h.conn, h.clock, run_id="run-1", requester=dana, steps=_reroute_steps(),
        approver_id="u-101", routed_reason=None, workflow="workflow:reroute_po",
    )
    h.conn.execute(
        "UPDATE approvals SET plan_json = REPLACE(plan_json, '150', '999999') WHERE approval_id = ?",
        (approval_id,),
    )
    h.conn.commit()
    row = get_approval(h.conn, approval_id)
    assert not verify_plan_hash(row["plan_json"], row["plan_hash"])


def test_get_approval_raises_for_unknown_id(make_harness):
    h = make_harness("scenario_a")
    with pytest.raises(UnknownApproval):
        get_approval(h.conn, "AP-NOPE")


def test_only_the_current_approver_can_decide(make_harness):
    h = make_harness("scenario_a")
    dana = get_user(h.conn, "u-101")
    approval_id = create_approval(
        h.conn, h.clock, run_id="run-1", requester=dana, steps=_reroute_steps(),
        approver_id="u-101", routed_reason=None, workflow="workflow:reroute_po",
    )
    with pytest.raises(NotCurrentApprover):
        decide(h.conn, h.clock, approval_id=approval_id, decided_by="u-202", decision="approved")
    assert get_approval(h.conn, approval_id)["status"] == "pending"


def test_rejecting_marks_rejected_writes_nothing_and_is_audited(make_harness):
    h = make_harness("scenario_a")
    dana = get_user(h.conn, "u-101")
    approval_id = create_approval(
        h.conn, h.clock, run_id="run-1", requester=dana, steps=_reroute_steps(),
        approver_id="u-101", routed_reason=None, workflow="workflow:reroute_po",
    )
    decide(h.conn, h.clock, approval_id=approval_id, decided_by="u-101", decision="rejected")

    row = get_approval(h.conn, approval_id)
    assert row["status"] == "rejected"
    assert h.conn.execute("SELECT COUNT(*) FROM executed_actions").fetchone()[0] == 0

    events = [r[0] for r in h.conn.execute("SELECT event FROM audit_log ORDER BY seq")]
    assert "approval.decided" in events


def test_deciding_an_already_decided_approval_raises(make_harness):
    h = make_harness("scenario_a")
    dana = get_user(h.conn, "u-101")
    approval_id = create_approval(
        h.conn, h.clock, run_id="run-1", requester=dana, steps=_reroute_steps(),
        approver_id="u-101", routed_reason=None, workflow="workflow:reroute_po",
    )
    decide(h.conn, h.clock, approval_id=approval_id, decided_by="u-101", decision="approved")
    with pytest.raises(ApprovalAlreadyDecided):
        decide(h.conn, h.clock, approval_id=approval_id, decided_by="u-101", decision="approved")


def test_escalation_reassigns_to_backup_when_approver_is_ooo_tomorrow(make_harness):
    h = make_harness("scenario_a")
    dana = get_user(h.conn, "u-101")
    approval_id = create_approval(
        h.conn, h.clock, run_id="run-1", requester=dana, steps=_reroute_steps(),
        approver_id="u-101", routed_reason=None, workflow="workflow:reroute_po",
    )
    # Today is 2026-09-02; Dana's E-002 OOO event covers 2026-09-03..04.
    escalated = escalate_pending(h.conn, h.clock)

    assert approval_id in escalated
    row = get_approval(h.conn, approval_id)
    assert row["approver_id"] == "u-102"
    assert row["routed_reason"] is not None

    events = [r[0] for r in h.conn.execute("SELECT event FROM audit_log ORDER BY seq")]
    assert "approval.escalated" in events


def test_escalation_audit_detail_includes_the_calendar_event_id(make_harness):
    """T2 (Tier 2): the escalation reason is prose; the actual evidence
    for it is a specific calendar row. explain and anyone auditing later
    should be able to point at E-002 itself, not just a sentence claiming
    it exists.
    """

    h = make_harness("scenario_a")
    dana = get_user(h.conn, "u-101")
    approval_id = create_approval(
        h.conn, h.clock, run_id="run-1", requester=dana, steps=_reroute_steps(),
        approver_id="u-101", routed_reason=None, workflow="workflow:reroute_po",
    )
    escalate_pending(h.conn, h.clock)

    detail = json.loads(
        h.conn.execute(
            "SELECT detail FROM audit_log WHERE event = 'approval.escalated' AND run_id = 'run-1'"
        ).fetchone()[0]
    )
    assert detail["ooo_event_id"] == "E-002"


def test_no_escalation_when_approver_is_not_ooo_tomorrow(make_harness):
    h = make_harness("scenario_a")
    priya = get_user(h.conn, "u-102")  # no OOO calendar event in this fixture
    approval_id = create_approval(
        h.conn, h.clock, run_id="run-1", requester=priya, steps=_reroute_steps(),
        approver_id="u-102", routed_reason=None, workflow="workflow:reroute_po",
    )
    escalated = escalate_pending(h.conn, h.clock)
    assert escalated == []
    assert get_approval(h.conn, approval_id)["approver_id"] == "u-102"


def test_no_escalation_when_already_decided(make_harness):
    h = make_harness("scenario_a")
    dana = get_user(h.conn, "u-101")
    approval_id = create_approval(
        h.conn, h.clock, run_id="run-1", requester=dana, steps=_reroute_steps(),
        approver_id="u-101", routed_reason=None, workflow="workflow:reroute_po",
    )
    decide(h.conn, h.clock, approval_id=approval_id, decided_by="u-101", decision="approved")

    escalated = escalate_pending(h.conn, h.clock)

    assert escalated == []
    assert get_approval(h.conn, approval_id)["approver_id"] == "u-101"


def test_no_escalation_when_approver_has_no_backup(make_harness):
    h = make_harness("scenario_a")
    marcus = get_user(h.conn, "u-100")  # no backup_approver_id
    h.conn.execute(
        "INSERT INTO cal_events (event_id, owner, start, end, title, out_of_office) "
        "VALUES ('E-900', 'u-100', '2026-09-03T00:00:00', '2026-09-03T23:59:59', 'Conference', 1)"
    )
    h.conn.commit()
    approval_id = create_approval(
        h.conn, h.clock, run_id="run-1", requester=marcus, steps=_reroute_steps(),
        approver_id="u-100", routed_reason=None, workflow="workflow:reroute_po",
    )
    escalated = escalate_pending(h.conn, h.clock)
    assert escalated == []
    assert get_approval(h.conn, approval_id)["approver_id"] == "u-100"


def test_no_escalation_when_nobody_in_the_backup_chain_qualifies(make_harness):
    h = make_harness("scenario_a")
    dana = get_user(h.conn, "u-101")
    # 3000 * 46.50 = 139500: over Dana's, Priya's (both 25000), and even
    # Marcus's (100000) limit, so no one in the chain can take the escalation.
    approval_id = create_approval(
        h.conn, h.clock, run_id="run-1", requester=dana, steps=_reroute_steps(qty=3000),
        approver_id="u-101", routed_reason=None, workflow="workflow:reroute_po",
    )
    escalated = escalate_pending(h.conn, h.clock)
    assert escalated == []
    assert get_approval(h.conn, approval_id)["approver_id"] == "u-101"


def test_escalation_continues_to_backups_manager_when_backup_limit_too_low(make_harness):
    h = make_harness("scenario_a_backup_low_limit")
    dana = get_user(h.conn, "u-101")
    # qty 150 * 46.50 = 6975: over u-102's lowered 3000 limit, under u-100's 100000.
    approval_id = create_approval(
        h.conn, h.clock, run_id="run-1", requester=dana, steps=_reroute_steps(qty=150),
        approver_id="u-101", routed_reason=None, workflow="workflow:reroute_po",
    )
    escalated = escalate_pending(h.conn, h.clock)

    assert approval_id in escalated
    assert get_approval(h.conn, approval_id)["approver_id"] == "u-100"


def test_approval_decided_by_backup_is_attributed_to_backup(make_harness):
    h = make_harness("scenario_a")
    dana = get_user(h.conn, "u-101")
    approval_id = create_approval(
        h.conn, h.clock, run_id="run-1", requester=dana, steps=_reroute_steps(),
        approver_id="u-101", routed_reason=None, workflow="workflow:reroute_po",
    )
    escalate_pending(h.conn, h.clock)

    decide(h.conn, h.clock, approval_id=approval_id, decided_by="u-102", decision="approved")

    row = get_approval(h.conn, approval_id)
    assert row["status"] == "approved"
    assert row["decided_by"] == "u-102"
