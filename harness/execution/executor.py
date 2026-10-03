"""The one place `Tool.run` is ever called. Shared by the free-form tool
runner and the workflow engine (phases 3 and 4), so idempotency, the
execution-time scope re-check, and audit logging only exist once.

Idempotency check, scope re-check, precheck, run, and the executed_actions
bookkeeping insert all happen in one SQLite transaction: either the write
and its record of having happened land together, or (on any failure)
neither does. The audit entry describing the outcome is appended and
committed right after.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from harness.audit.log import log as audit_log
from harness.detection.registry import run_detectors
from harness.execution.catalog import ERP_WRITING_TOOLS, get_tool
from harness.execution.tools import PrecheckFailed, Tool, ToolContext
from harness.scheduling.clock import Clock
from harness.world.users import get_user, missing_scopes

from pydantic import BaseModel


class ScopeDenied(Exception):
    def __init__(self, user_id: str, scope: str, tool: str):
        super().__init__(f"{user_id} lacks scope {scope} required by {tool}")
        self.user_id = user_id
        self.scope = scope
        self.tool = tool


def execute(
    conn: sqlite3.Connection,
    clock: Clock,
    tool: Tool,
    args: BaseModel,
    ctx: ToolContext,
    *,
    run_id: str | None,
    actor: str,
    requester_id: str,
    skip_precheck: bool = False,
) -> dict[str, Any]:
    """skip_precheck is for compensate() only: a tool's precheck encodes a
    forward-looking business rule (e.g. reallocate_lot refuses to allocate
    onto a held lot). Reversing a previously-successful action is cleanup,
    not a new business decision, so compensations do not re-run it. Scope
    and idempotency still apply either way.
    """

    idempotency_key = tool.idempotency_key(args, ctx)

    existing = conn.execute(
        "SELECT result FROM executed_actions WHERE idempotency_key = ?", (idempotency_key,)
    ).fetchone()
    if existing is not None:
        audit_log(
            conn, clock, run_id=run_id, actor=actor, event="action.skipped_idempotent",
            detail={"tool": tool.name, "idempotency_key": idempotency_key},
        )
        conn.commit()
        return json.loads(existing[0])

    try:
        requester = get_user(conn, requester_id)
        missing = missing_scopes(requester, tool.required_scopes)
        if missing:
            raise ScopeDenied(requester_id, missing[0], tool.name)

        if tool.precheck is not None and not skip_precheck:
            tool.precheck(conn, args)

        result = tool.run(conn, args, ctx)

        conn.execute(
            "INSERT INTO executed_actions (idempotency_key, tool, args, result, executed_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (
                idempotency_key,
                tool.name,
                args.model_dump_json(),
                json.dumps(result),
                clock.today().isoformat(),
            ),
        )
    except ScopeDenied as exc:
        conn.rollback()
        audit_log(
            conn, clock, run_id=run_id, actor=actor, event="action.scope_denied",
            detail={"tool": tool.name, "user_id": exc.user_id, "scope": exc.scope},
        )
        conn.commit()
        raise
    except PrecheckFailed as exc:
        conn.rollback()
        audit_log(
            conn, clock, run_id=run_id, actor=actor, event="action.precheck_failed",
            detail={"tool": tool.name, "idempotency_key": idempotency_key, "reason": exc.reason},
        )
        conn.commit()
        raise
    except Exception as exc:
        conn.rollback()
        audit_log(
            conn, clock, run_id=run_id, actor=actor, event="action.failed",
            detail={"tool": tool.name, "idempotency_key": idempotency_key, "error": str(exc)},
        )
        conn.commit()
        raise

    audit_log(
        conn, clock, run_id=run_id, actor=actor, event="action.executed",
        detail={
            "tool": tool.name,
            "idempotency_key": idempotency_key,
            "args": json.loads(args.model_dump_json()),
            "result": result,
        },
    )
    conn.commit()

    if tool.name in ERP_WRITING_TOOLS:
        # Section 8: detectors run on every tick and also right after a
        # tool writes to an ERP table, so a new risk introduced by this
        # very write (or one it just resolved) is caught immediately
        # rather than waiting for the next tick.
        run_detectors(conn, clock)

    return result


def compensate(
    conn: sqlite3.Connection,
    clock: Clock,
    tool: Tool,
    original_args: BaseModel,
    original_result: dict[str, Any],
    ctx: ToolContext,
    *,
    run_id: str | None,
    actor: str,
    requester_id: str,
) -> dict[str, Any]:
    if tool.compensate is None or tool.compensation_args is None:
        raise ValueError(f"tool {tool.name} declares no compensation")

    comp_tool = get_tool(tool.compensate)
    comp_args = tool.compensation_args(original_args, original_result)
    # A distinct step name, not the original action's: otherwise the
    # compensation's idempotency key would collide with the forward
    # action's and execute() would treat it as already done.
    comp_ctx = ToolContext(run_id=ctx.run_id, step=f"{ctx.step}:compensate", today=ctx.today)
    idempotency_key = comp_tool.idempotency_key(comp_args, comp_ctx)
    already_compensated = conn.execute(
        "SELECT 1 FROM executed_actions WHERE idempotency_key = ?", (idempotency_key,)
    ).fetchone() is not None

    result = execute(
        conn, clock, comp_tool, comp_args, comp_ctx,
        run_id=run_id, actor=actor, requester_id=requester_id, skip_precheck=True,
    )
    if already_compensated:
        # A retried compensation loop (e.g. resumed after a crash
        # mid-compensation) replays this entry; execute()'s own
        # idempotency check already logged action.skipped_idempotent for
        # it. Logging action.compensated again here would claim a second
        # reversal happened when the underlying write did not.
        return result

    audit_log(
        conn, clock, run_id=run_id, actor=actor, event="action.compensated",
        detail={"original_tool": tool.name, "compensation_tool": comp_tool.name, "result": result},
    )
    conn.commit()
    return result
