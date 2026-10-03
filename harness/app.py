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
from harness.execution.runner import run_approved_plan
from harness.memory.facts import facts_for_prompt, write_fact
from harness.memory.runs import create_run, get_run, set_run_status, update_run_state
from harness.planning.llm import LLMClient, OpenAIClient, ReplayClient
from harness.planning.models import NoAction, ToolPlan, WorkflowRequest
from harness.planning.planner import PlannerFailed, propose
from harness.policy.approvals import create_approval, decide, escalate_pending, get_approval
from harness.policy.gate import Allowed, Blocked, gate
from harness.scheduling import arrival_check as _arrival_check  # noqa: F401  (registers the arrival_check task handler)
from harness.scheduling.clock import Clock
from harness.scheduling.tasks import run_due_tasks
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
    # Marked immediately, before planning: an item is handed to the
    # planner at most once, regardless of what the planner does with it.
    conn.execute("UPDATE attention_items SET status = 'planned' WHERE item_id = ?", (item_row["item_id"],))
    conn.commit()

    user = get_user(conn, item.owner_id)
    run_id = create_run(conn, clock, item_id=item_row["item_id"], user_id=user.user_id)

    context = gather_context(conn, clock, user, item, run_id=run_id)
    memory_facts = facts_for_prompt(conn, clock)
    # Kept for the confirmed-outcome memory fact written on approval
    # (section 13): the mail evidence behind this run's reasoning, and the
    # item's own facts (supplier, dependent PO), are not otherwise
    # reachable from the approval record alone.
    update_run_state(conn, run_id, {
        "item_facts": item.facts,
        "context_record_ids": {source: slice_.record_ids for source, slice_ in context.items()},
    })

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
    scheduled tasks, approval escalation, resuming any workflow a crash
    left running, detection, and planning for anything still unplanned.
    The clock advances last, preparing for the next tick.

    Planning runs over every `open` attention item, not just the ones
    `run_detectors` returned this tick: an arrival-check task firing in
    the same tick (above) can raise a new item of its own, and it needs
    planning exactly the same way a freshly detected one does.
    """

    fired_tasks = run_due_tasks(conn, clock)
    escalated = escalate_pending(conn, clock)
    resumed = resume_all(conn, clock, llm_client)
    new_item_ids = run_detectors(conn, clock)

    unplanned = conn.execute("SELECT * FROM attention_items WHERE status = 'open'").fetchall()
    run_ids = [handle_attention_item(conn, clock, llm_client, row) for row in unplanned]

    today_before = clock.today().isoformat()
    clock.advance(1)
    return {
        "today": today_before,
        "fired_tasks": fired_tasks,
        "escalated": escalated,
        "resumed_workflows": resumed,
        "new_items": new_item_ids,
        "runs": run_ids,
    }


def _write_completion_fact(conn: sqlite3.Connection, clock: Clock, run_id: str) -> None:
    """A workflow reaching `completed` is a confirmed outcome (section 13):
    the planner's evidence for entering it (a supplier's own mail,
    typically) is now acted on, not just proposed. Stashed at planning
    time in run state, since the approval record alone doesn't carry it.
    """

    run = get_run(conn, run_id)
    state = json.loads(run["state"])
    item_facts = state.get("item_facts", {})
    supplier_id = item_facts.get("supplier_id")
    inbound_po_id = item_facts.get("inbound_po_id")
    if not supplier_id or not inbound_po_id or inbound_po_id == "none":
        return
    mail_ids = state.get("context_record_ids", {}).get("mail", [])
    write_fact(
        conn, clock, subject=supplier_id,
        fact=f"{supplier_id} was rerouted away from {inbound_po_id} after a confirmed delay.",
        source_ids=mail_ids or [inbound_po_id],
    )


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
            if final_row["status"] == "completed":
                _write_completion_fact(conn, clock, approval["run_id"])
    else:
        # Free-form: the requester's own scopes govern execution, same as
        # the workflow path's requester. The approval record doesn't carry
        # requester_id directly; the run it belongs to does.
        requester_id = get_run(conn, approval["run_id"])["user_id"]
        final_status = run_approved_plan(
            conn, clock, approval_id, requester_id=requester_id, run_id=approval["run_id"],
        )
        set_run_status(conn, approval["run_id"], final_status)


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

    def close(self) -> None:
        self.conn.close()

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
