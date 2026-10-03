"""Approvals: freezing a gated plan, letting the current approver decide,
and the end-of-day escalation rule.

Approval binds to a frozen plan (invariant 3): once created, the plan is
stored as canonical JSON plus its SHA-256 hash, and nothing about it can
change. The executor (phase 3/4) is what refuses to run a plan whose
current JSON no longer matches its stored hash; `verify_plan_hash` here is
the check it will call.

The calendar read in `_is_out_of_office` is a policy read, not a provider
read (section 9): it never goes through a scoped context provider and is
never shown to the planner. It exists only to decide who the current
approver should be.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from datetime import date, timedelta

from harness.audit.log import log as audit_log
from harness.planning.models import ToolCall
from harness.policy.gate import plan_value, qualifying_approver
from harness.scheduling.clock import Clock
from harness.world.users import User, get_user


class UnknownApproval(Exception):
    pass


class ApprovalAlreadyDecided(Exception):
    pass


class NotCurrentApprover(Exception):
    def __init__(self, decided_by: str, approver_id: str):
        super().__init__(f"{decided_by} is not the current approver ({approver_id})")
        self.decided_by = decided_by
        self.approver_id = approver_id


def _canonical_plan_json(workflow: str | None, steps: list[ToolCall]) -> str:
    plan = {
        "workflow": workflow,
        "steps": [{"tool": s.tool, "args": s.args} for s in steps],
    }
    return json.dumps(plan, sort_keys=True, separators=(",", ":"))


def plan_hash(plan_json: str) -> str:
    return hashlib.sha256(plan_json.encode("utf-8")).hexdigest()


def verify_plan_hash(plan_json: str, expected_hash: str) -> bool:
    return plan_hash(plan_json) == expected_hash


def create_approval(
    conn: sqlite3.Connection,
    clock: Clock,
    *,
    run_id: str,
    requester: User,
    steps: list[ToolCall],
    approver_id: str,
    routed_reason: str | None,
    workflow: str | None = None,
) -> str:
    approval_id = f"AP-{uuid.uuid4().hex[:8].upper()}"
    plan_json = _canonical_plan_json(workflow, steps)
    the_hash = plan_hash(plan_json)

    conn.execute(
        "INSERT INTO approvals (approval_id, run_id, approver_id, plan_json, plan_hash, "
        "status, requested_at, decided_at, decided_by, routed_reason) "
        "VALUES (?, ?, ?, ?, ?, 'pending', ?, NULL, NULL, ?)",
        (approval_id, run_id, approver_id, plan_json, the_hash, clock.today().isoformat(), routed_reason),
    )
    audit_log(
        conn, clock, run_id=run_id, actor="gate", event="approval.requested",
        detail={
            "approval_id": approval_id,
            "requester_id": requester.user_id,
            "approver_id": approver_id,
            "plan_hash": the_hash,
            "routed_reason": routed_reason,
        },
    )
    conn.commit()
    return approval_id


def get_approval(conn: sqlite3.Connection, approval_id: str) -> sqlite3.Row:
    row = conn.execute(
        "SELECT * FROM approvals WHERE approval_id = ?", (approval_id,)
    ).fetchone()
    if row is None:
        raise UnknownApproval(approval_id)
    return row


def decide(
    conn: sqlite3.Connection,
    clock: Clock,
    *,
    approval_id: str,
    decided_by: str,
    decision: str,
) -> None:
    row = get_approval(conn, approval_id)
    if row["status"] != "pending":
        raise ApprovalAlreadyDecided(f"approval {approval_id} is already {row['status']}")
    if decided_by != row["approver_id"]:
        raise NotCurrentApprover(decided_by, row["approver_id"])

    new_status = "approved" if decision == "approved" else "rejected"
    conn.execute(
        "UPDATE approvals SET status = ?, decided_at = ?, decided_by = ? WHERE approval_id = ?",
        (new_status, clock.today().isoformat(), decided_by, approval_id),
    )
    audit_log(
        conn, clock, run_id=row["run_id"], actor=decided_by, event="approval.decided",
        detail={"approval_id": approval_id, "decision": new_status},
    )
    conn.commit()


def _out_of_office_event(conn: sqlite3.Connection, user_id: str, day: date) -> str | None:
    """The matching OOO event's id, or None. Returning the id rather than
    a bare bool is what lets `escalate_pending` audit its actual evidence
    (the calendar event that triggered the reassignment), not just the
    conclusion it drew from it.
    """

    rows = conn.execute(
        "SELECT event_id, start, end FROM cal_events WHERE owner = ? AND out_of_office = 1", (user_id,)
    ).fetchall()
    for event_id, start, end in rows:
        start_date = date.fromisoformat(start[:10])
        end_date = date.fromisoformat(end[:10])
        if start_date <= day <= end_date:
            return event_id
    return None


def escalate_pending(conn: sqlite3.Connection, clock: Clock) -> list[str]:
    """Run once per tick, last, right before the clock advances -- so it
    sees today's own newly-created approvals too, not just ones already
    pending from a prior tick (app.py's tick()). Any approval still pending
    whose approver's calendar shows them out of office tomorrow gets
    reassigned to their backup (walking the backup's own manager chain if
    the backup's limit is too low). Returns the ids of approvals that were
    escalated.
    """

    tomorrow = clock.today() + timedelta(days=1)
    escalated: list[str] = []

    rows = conn.execute(
        "SELECT approval_id, run_id, approver_id, plan_json FROM approvals WHERE status = 'pending'"
    ).fetchall()

    for approval_id, run_id, approver_id, plan_json in rows:
        ooo_event_id = _out_of_office_event(conn, approver_id, tomorrow)
        if ooo_event_id is None:
            continue

        approver = get_user(conn, approver_id)
        if not approver.backup_approver_id:
            continue
        backup = get_user(conn, approver.backup_approver_id)

        value = json.loads(plan_json)["steps"]
        steps = [ToolCall(tool=s["tool"], args=s["args"]) for s in value]
        total_value = plan_value(steps)

        new_approver = qualifying_approver(conn, backup, total_value) if total_value > 0 else backup
        if new_approver is None:
            continue

        reason = (
            f"{approver_id} had not responded by end of day and is out of office "
            f"{tomorrow.isoformat()}; routed to {new_approver.user_id}"
        )
        conn.execute(
            "UPDATE approvals SET approver_id = ?, routed_reason = ? WHERE approval_id = ?",
            (new_approver.user_id, reason, approval_id),
        )
        audit_log(
            conn, clock, run_id=run_id, actor="scheduler", event="approval.escalated",
            detail={
                "approval_id": approval_id,
                "from": approver_id,
                "to": new_approver.user_id,
                "reason": reason,
                "ooo_event_id": ooo_event_id,
            },
        )
        conn.commit()
        escalated.append(approval_id)

    return escalated
