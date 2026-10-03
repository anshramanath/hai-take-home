"""Wires the fake company, the clock, and the registries together: the
`Harness` object the CLI holds onto, and the orchestration functions
(`handle_attention_item`, `tick`, `approve`, `reject`) that turn a raised
attention item into a proposal, a gate decision, an approval, and
(depending on the path) a declared workflow or a free-form plan.

`tick()` runs every "today"-scoped step (due tasks, escalation, resuming
crashed workflows, detection, planning) before advancing the clock, not
after. Section 13 lists "advance clock" first; doing it last instead is a
deliberate, documented deviation (see BUILD_LOG.md) — the escalation rule
reads as "unanswered at end of day the first time no answer comes back"
meaning the check is naturally made at the end of the day that just
happened, not after the day has already turned over. Advancing first would
mean the first tick after seeding processes tomorrow's date instead of
today's, and would shift `escalate_pending`'s "is the approver out
tomorrow" check by one day from what it was built and tested against.
"""

from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path

from harness.context.registry import gather_context
from harness.detection.base import AttentionItem
from harness.detection.registry import run_detectors
from harness.execution import workflows as _workflows  # noqa: F401  (registers every workflow definition)
from harness.execution.engine import (
    enter_workflow,
    get_definition,
    latest_version,
    resume_after_approval,
    resume_all,
    set_instance_status,
)
from harness.memory.runs import create_run, set_run_status, update_run_state
from harness.planning.llm import LLMClient, OpenAIClient, ReplayClient
from harness.planning.models import NoAction, ToolPlan, WorkflowRequest
from harness.planning.planner import PlannerFailed, propose
from harness.policy.approvals import create_approval, decide, escalate_pending, get_approval
from harness.policy.gate import Allowed, Blocked, gate
from harness.scheduling.clock import Clock
from harness.world.seed import seed
from harness.world.users import get_user

DEFAULT_DB_PATH = Path("harness.db")
DEFAULT_REPLAY_PATH = Path("runs/scenario_a_responses.json")

STATUS_TABLES = (
    "erp_parts",
    "erp_suppliers",
    "erp_purchase_orders",
    "erp_production_orders",
    "erp_receipts",
    "erp_lots",
    "erp_lot_allocations",
    "mail_messages",
    "cal_events",
    "users",
    "notifications",
    "attention_items",
    "runs",
    "approvals",
    "workflow_instances",
    "scheduled_tasks",
    "executed_actions",
    "memory_facts",
    "audit_log",
)


def default_llm_client() -> LLMClient:
    if os.environ.get("OPENAI_API_KEY"):
        return OpenAIClient()
    if DEFAULT_REPLAY_PATH.exists():
        return ReplayClient(DEFAULT_REPLAY_PATH)
    raise RuntimeError(
        "No OPENAI_API_KEY set and no replay fixture found at "
        f"{DEFAULT_REPLAY_PATH}. Set OPENAI_API_KEY, or run the demo once "
        "with a key to record one."
    )


def handle_attention_item(
    conn: sqlite3.Connection, clock: Clock, llm_client: LLMClient, item_row: sqlite3.Row
) -> str:
    """Attention item -> context -> proposal -> gate/workflow entry. Always
    creates a run and always leaves it in a terminal or awaiting-approval
    status; never raises for a planner failure, only for a genuine bug.
    """

    item = AttentionItem(
        detector=item_row["detector"], dedupe_key=item_row["dedupe_key"],
        owner_id=item_row["owner_id"], summary=item_row["summary"], facts=json.loads(item_row["facts"]),
    )
    user = get_user(conn, item.owner_id)
    run_id = create_run(conn, clock, item_id=item_row["item_id"], user_id=user.user_id)

    context = gather_context(conn, clock, user, item, run_id=run_id)
    memory_facts: list[dict] = []  # phase 5 populates this from confirmed outcomes

    try:
        proposal = propose(conn, clock, llm_client, item, context, memory_facts, user, run_id=run_id)
    except PlannerFailed as exc:
        set_run_status(conn, run_id, "failed")
        update_run_state(conn, run_id, {"error": str(exc)})
        return run_id

    if isinstance(proposal, NoAction):
        set_run_status(conn, run_id, "closed")
        update_run_state(conn, run_id, {"reasoning": proposal.reasoning})
        return run_id

    if isinstance(proposal, WorkflowRequest):
        definition = get_definition(proposal.workflow, latest_version(proposal.workflow))
        instance_row = enter_workflow(
            conn, clock, llm_client, definition, proposal.params, run_id=run_id, requester=user,
        )
        update_run_state(conn, run_id, {"instance_id": instance_row["instance_id"]})
        set_run_status(conn, run_id, instance_row["status"])
        return run_id

    assert isinstance(proposal, ToolPlan)
    result = gate(conn, user, proposal.steps, workflow=None)
    if isinstance(result, Blocked):
        set_run_status(conn, run_id, "failed")
        update_run_state(conn, run_id, {"gate_blocked_reason": result.reason})
        return run_id

    assert isinstance(result, Allowed)
    approval_id = create_approval(
        conn, clock, run_id=run_id, requester=user, steps=proposal.steps,
        approver_id=result.approver_id, routed_reason=result.routed_reason, workflow=None,
    )
    update_run_state(conn, run_id, {"approval_id": approval_id})
    set_run_status(conn, run_id, "awaiting_approval")
    return run_id


def tick(conn: sqlite3.Connection, clock: Clock, llm_client: LLMClient) -> dict:
    """Everything scoped to "today" runs before the clock advances: due
    scheduled tasks (added in phase 5), approval escalation, resuming any
    workflow a crash left running, detection, and planning for whatever
    detection just raised. The clock advances last, preparing for the next
    tick.
    """

    escalated = escalate_pending(conn, clock)
    resumed = resume_all(conn, clock, llm_client)
    new_item_ids = run_detectors(conn, clock)

    run_ids = []
    for item_id in new_item_ids:
        item_row = conn.execute("SELECT * FROM attention_items WHERE item_id = ?", (item_id,)).fetchone()
        run_ids.append(handle_attention_item(conn, clock, llm_client, item_row))

    today_before = clock.today().isoformat()
    clock.advance(1)
    return {
        "today": today_before,
        "escalated": escalated,
        "resumed_workflows": resumed,
        "new_items": new_item_ids,
        "runs": run_ids,
    }


def approve(conn: sqlite3.Connection, clock: Clock, llm_client: LLMClient, *, approval_id: str, decided_by: str) -> None:
    decide(conn, clock, approval_id=approval_id, decided_by=decided_by, decision="approved")
    approval = get_approval(conn, approval_id)
    plan = json.loads(approval["plan_json"])
    if plan.get("workflow"):
        instance_row = conn.execute(
            "SELECT instance_id FROM workflow_instances WHERE run_id = ?", (approval["run_id"],)
        ).fetchone()
        if instance_row is not None:
            final_row = resume_after_approval(conn, clock, llm_client, instance_row["instance_id"])
            set_run_status(conn, approval["run_id"], final_row["status"])
    # A free-form (workflow is None) approved plan's execution is phase 6's
    # tool runner; nothing to do here yet for that path.


def reject(conn: sqlite3.Connection, clock: Clock, *, approval_id: str, decided_by: str) -> None:
    decide(conn, clock, approval_id=approval_id, decided_by=decided_by, decision="rejected")
    approval = get_approval(conn, approval_id)
    plan = json.loads(approval["plan_json"])
    set_run_status(conn, approval["run_id"], "rejected")
    if plan.get("workflow"):
        instance_row = conn.execute(
            "SELECT instance_id FROM workflow_instances WHERE run_id = ?", (approval["run_id"],)
        ).fetchone()
        if instance_row is not None:
            set_instance_status(conn, instance_row["instance_id"], "rejected")


class Harness:
    """Owns the SQLite connection and the clock for one database file."""

    def __init__(self, db_path: Path = DEFAULT_DB_PATH):
        self.db_path = Path(db_path)
        self.conn = self._connect()
        self.clock = Clock(self.conn)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def reset(self, fixture: str) -> None:
        self.conn.close()
        if self.db_path.exists():
            self.db_path.unlink()
        self.conn = self._connect()
        seed(self.conn, fixture)
        self.clock = Clock(self.conn)

    def status(self) -> dict[str, int | str]:
        counts: dict[str, int | str] = {
            table: self.conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in STATUS_TABLES
        }
        counts["today"] = self.clock.today().isoformat()
        return counts

    def tick(self, llm_client: LLMClient | None = None) -> dict:
        return tick(self.conn, self.clock, llm_client or default_llm_client())

    def approve(self, approval_id: str, decided_by: str, llm_client: LLMClient | None = None) -> None:
        approve(self.conn, self.clock, llm_client or default_llm_client(), approval_id=approval_id, decided_by=decided_by)

    def reject(self, approval_id: str, decided_by: str) -> None:
        reject(self.conn, self.clock, approval_id=approval_id, decided_by=decided_by)
