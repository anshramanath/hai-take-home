"""The free-form tool runner (Scenario B's execution path): runs an
approved ToolPlan's steps in the order the plan says, through the same
shared executor the workflow engine uses, so idempotency, the
execution-time scope re-check, and audit mean the same thing regardless
of which path produced the plan. Unlike the workflow engine there is no
fixed step order to enforce — whatever order the planner proposed and the
gate allowed is what runs, including a plan missing a step the spec would
have wanted: free-form does not guarantee completeness, only that the gate
and the approval it got are honored exactly.

On any step's failure, already-completed steps are compensated in
reverse, exactly like the workflow engine's own failure handling — reusing
`executor.compensate()`, not a second implementation of it.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from harness.audit.log import log as audit_log
from harness.execution.catalog import get_tool
from harness.execution.executor import compensate as compensate_tool
from harness.execution.executor import execute as execute_tool
from harness.execution.tools import ToolContext
from harness.policy.approvals import get_approval, verify_plan_hash
from harness.scheduling.clock import Clock


def run_approved_plan(
    conn: sqlite3.Connection,
    clock: Clock,
    approval_id: str,
    *,
    requester_id: str,
    run_id: str,
) -> str:
    """Returns the terminal status: 'completed', 'compensated', or
    'failed' (hash mismatch; nothing executes).
    """

    approval = get_approval(conn, approval_id)
    if approval["status"] != "approved":
        raise ValueError(f"approval {approval_id} is not approved (status={approval['status']})")

    if not verify_plan_hash(approval["plan_json"], approval["plan_hash"]):
        audit_log(
            conn, clock, run_id=run_id, actor="runner", event="plan.halted",
            detail={"reason": "approved plan does not match its recorded hash"},
        )
        conn.commit()
        return "failed"

    steps = json.loads(approval["plan_json"])["steps"]
    compensation_log: list[dict[str, Any]] = []

    for index, step in enumerate(steps):
        tool = get_tool(step["tool"])
        args = tool.input_schema.model_validate(step["args"])
        step_name = f"{index}-{tool.name}"
        ctx = ToolContext(run_id=f"{run_id}:free-form", step=step_name, today=clock.today())

        try:
            result = execute_tool(
                conn, clock, tool, args, ctx, run_id=run_id, actor="free-form", requester_id=requester_id,
            )
        except Exception as exc:
            audit_log(
                conn, clock, run_id=run_id, actor="runner", event="plan.step_failed",
                detail={"tool": tool.name, "step": step_name, "error": str(exc)},
            )
            conn.commit()
            for entry in reversed(compensation_log):
                comp_tool = get_tool(entry["tool"])
                comp_args = comp_tool.input_schema.model_validate(entry["args"])
                comp_ctx = ToolContext(run_id=f"{run_id}:free-form", step=entry["step"], today=clock.today())
                compensate_tool(
                    conn, clock, comp_tool, comp_args, entry["result"], comp_ctx,
                    run_id=run_id, actor="free-form", requester_id=requester_id,
                )
            return "compensated"

        if tool.compensate is not None:
            compensation_log.append({
                "tool": tool.name, "args": step["args"], "result": result, "step": step_name,
            })

    return "completed"
