"""Renders the append-only audit log into a human-readable narrative
(section 13): someone reading only this output should be able to
reconstruct what the agent saw, what it concluded, what it was allowed to
do, who approved what, and what actually happened in each system. Reads
only from audit_log; nothing here touches any other table.
"""

from __future__ import annotations

import json
import sqlite3


def _render(event: str, detail: dict, actor: str) -> str:
    if event == "detection.raised":
        return f"detection ({actor}): {detail.get('summary')} [{detail.get('item_id')}]"
    if event == "detection.duplicate_ignored":
        return f"detection ({actor}): already known [{detail.get('dedupe_key')}]"
    if event == "context.gathered":
        parts = ", ".join(f"{source} {ids}" for source, ids in detail.get("record_ids", {}).items())
        return f"context for {detail.get('user_id')}: {parts}"
    if event == "planner.proposed":
        return f"planner: {detail.get('kind')} - reason: {detail.get('reasoning')}"
    if event == "planner.invalid":
        return f"planner: gave up after invalid output - {detail.get('second_error')}"
    if event == "gate.allowed":
        reason = f" ({detail['routed_reason']})" if detail.get("routed_reason") else ""
        return f"gate: allowed, approver {detail.get('approver_id')}{reason}"
    if event == "gate.blocked":
        return f"gate: blocked - {detail.get('reason')}"
    if event == "approval.requested":
        return f"approval requested: {detail.get('approval_id')}, approver {detail.get('approver_id')}"
    if event == "approval.escalated":
        return f"escalation: {detail.get('reason')}"
    if event == "approval.decided":
        return f"approval {detail.get('approval_id')} {detail.get('decision')} by {actor}"
    if event == "workflow.step_started":
        return f"workflow step started: {detail.get('step')}"
    if event == "workflow.step_completed":
        updates = detail.get("updates") or {}
        extra = f" {updates}" if updates else ""
        return f"workflow step completed: {detail.get('step')}{extra}"
    if event == "workflow.halted":
        return f"workflow halted ({detail.get('status')}): {detail.get('reason')}"
    if event == "workflow.step_failed":
        return f"workflow step failed: {detail.get('step')} - {detail.get('error')}"
    if event == "action.executed":
        return f"executed {detail.get('tool')}: {detail.get('result')}"
    if event == "action.skipped_idempotent":
        return f"skipped (already done): {detail.get('tool')}"
    if event == "action.compensated":
        return f"compensated {detail.get('original_tool')} via {detail.get('compensation_tool')}: {detail.get('result')}"
    if event == "action.precheck_failed":
        return f"precheck failed for {detail.get('tool')}: {detail.get('reason')}"
    if event == "action.scope_denied":
        return f"scope denied for {detail.get('tool')}: missing {detail.get('scope')}"
    if event == "action.failed":
        return f"action failed: {detail.get('tool')} - {detail.get('error')}"
    if event == "schedule.fired":
        return f"scheduled task fired: {detail.get('kind')} [{detail.get('task_id')}]"
    if event == "arrival_check.confirmed":
        return f"arrival check: confirmed [{detail.get('po_id')}] received {detail.get('received')}/{detail.get('ordered')}"
    if event == "arrival_check.missed":
        return f"arrival check: not received [{detail.get('po_id')}] ({detail.get('received')}/{detail.get('ordered')})"
    if event == "memory.fact_written":
        return f"memory: {detail.get('fact')} (source {detail.get('source_ids')})"
    return f"{event}: {json.dumps(detail)}"


def explain(conn: sqlite3.Connection, run_id: str | None = None) -> list[str]:
    if run_id is not None:
        rows = conn.execute(
            "SELECT * FROM audit_log WHERE run_id = ? ORDER BY seq", (run_id,)
        ).fetchall()
    else:
        rows = conn.execute("SELECT * FROM audit_log ORDER BY seq").fetchall()

    lines = []
    for row in rows:
        detail = json.loads(row["detail"]) if row["detail"] else {}
        lines.append(f"[{row['ts']} #{row['seq']}] {_render(row['event'], detail, row['actor'])}")
    return lines
