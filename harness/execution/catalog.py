"""The tool catalog: every registered Tool, with its run/precheck logic and
the registry the gate, executor, and (later) planner all read from.
"""

from __future__ import annotations

import json
import uuid
from datetime import timedelta
from typing import Any

from harness.execution.args import (
    AllocationEntry,
    CancelPoArgs,
    CancelTaskArgs,
    CreatePoArgs,
    FlagShortageArgs,
    NotificationArgs,
    ReallocateLotArgs,
    ReducePoArgs,
    RestorePoArgs,
    ScheduleCheckArgs,
    WithdrawFlagArgs,
)
from harness.execution.tools import PrecheckFailed, Tool, ToolContext
from harness.world.users import User, missing_scopes


class UnknownTool(Exception):
    pass


# ---------------------------------------------------------------------------
# create_po / cancel_po / reduce_po / restore_po


def _precheck_create_po(db, args: CreatePoArgs) -> None:
    row = db.execute(
        "SELECT approved, approved_parts FROM erp_suppliers WHERE supplier_id = ?",
        (args.supplier_id,),
    ).fetchone()
    if row is None:
        raise PrecheckFailed(f"supplier {args.supplier_id} does not exist")
    approved, approved_parts_json = row
    approved_parts = json.loads(approved_parts_json)
    if not approved or args.part_id not in approved_parts:
        raise PrecheckFailed(f"supplier {args.supplier_id} is not approved for part {args.part_id}")


def _run_create_po(db, args: CreatePoArgs, ctx: ToolContext) -> dict:
    """`promised_date` is a fact the supplier system hands back when the
    order is actually placed, not a value approved ahead of time (F3): a
    plan approved on day N must not freeze a promised date computed as if
    it had been placed on day N minus however many days escalation took.
    Computed here, at execution, from execution-time "today" plus the
    supplier's own lead time, and refused before any write if it would
    land after `needed_by`, the frozen decision the human did approve.
    """

    lead_time_days = db.execute(
        "SELECT lead_time_days FROM erp_suppliers WHERE supplier_id = ?", (args.supplier_id,)
    ).fetchone()[0]
    promised_date = (ctx.today + timedelta(days=lead_time_days)).isoformat()
    if promised_date > args.needed_by:
        raise PrecheckFailed(
            f"supplier {args.supplier_id}'s lead time now lands on {promised_date}, "
            f"after needed_by {args.needed_by}"
        )

    total_value = round(args.qty * args.unit_price, 2)
    db.execute(
        "INSERT INTO erp_purchase_orders (po_id, part_id, supplier_id, qty, unit_price, "
        "total_value, ordered_date, promised_date, status, created_by) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'open', ?)",
        (
            args.po_id,
            args.part_id,
            args.supplier_id,
            args.qty,
            args.unit_price,
            total_value,
            ctx.today.isoformat(),
            promised_date,
            args.created_by,
        ),
    )
    return {"po_id": args.po_id, "total_value": total_value, "promised_date": promised_date}


def _compensation_args_create_po(args: CreatePoArgs, result: dict) -> CancelPoArgs:
    return CancelPoArgs(po_id=result["po_id"])


def _precheck_cancel_po(db, args: CancelPoArgs) -> None:
    row = db.execute(
        "SELECT status FROM erp_purchase_orders WHERE po_id = ?", (args.po_id,)
    ).fetchone()
    if row is None:
        raise PrecheckFailed(f"PO {args.po_id} does not exist")
    if row[0] != "open":
        raise PrecheckFailed(f"PO {args.po_id} is not open (status={row[0]})")


def _run_cancel_po(db, args: CancelPoArgs, ctx: ToolContext) -> dict:
    previous_qty, previous_status = db.execute(
        "SELECT qty, status FROM erp_purchase_orders WHERE po_id = ?", (args.po_id,)
    ).fetchone()
    db.execute("UPDATE erp_purchase_orders SET status = 'cancelled' WHERE po_id = ?", (args.po_id,))
    return {"po_id": args.po_id, "previous_qty": previous_qty, "previous_status": previous_status}


def _compensation_args_cancel_or_reduce_po(args, result: dict) -> RestorePoArgs:
    return RestorePoArgs(po_id=args.po_id, qty=result["previous_qty"], status=result["previous_status"])


def _precheck_reduce_po(db, args: ReducePoArgs) -> None:
    row = db.execute(
        "SELECT qty, status FROM erp_purchase_orders WHERE po_id = ?", (args.po_id,)
    ).fetchone()
    if row is None:
        raise PrecheckFailed(f"PO {args.po_id} does not exist")
    current_qty, status = row
    if status != "open":
        raise PrecheckFailed(f"PO {args.po_id} is not open (status={status})")
    if not (0 <= args.new_qty < current_qty):
        raise PrecheckFailed(
            f"new_qty {args.new_qty} must be >= 0 and < current qty {current_qty}"
        )


def _run_reduce_po(db, args: ReducePoArgs, ctx: ToolContext) -> dict:
    previous_qty, unit_price, previous_status = db.execute(
        "SELECT qty, unit_price, status FROM erp_purchase_orders WHERE po_id = ?", (args.po_id,)
    ).fetchone()
    new_total = round(args.new_qty * unit_price, 2)
    db.execute(
        "UPDATE erp_purchase_orders SET qty = ?, total_value = ? WHERE po_id = ?",
        (args.new_qty, new_total, args.po_id),
    )
    return {"po_id": args.po_id, "previous_qty": previous_qty, "previous_status": previous_status}


def _run_restore_po(db, args: RestorePoArgs, ctx: ToolContext) -> dict:
    unit_price = db.execute(
        "SELECT unit_price FROM erp_purchase_orders WHERE po_id = ?", (args.po_id,)
    ).fetchone()[0]
    total_value = round(args.qty * unit_price, 2)
    db.execute(
        "UPDATE erp_purchase_orders SET qty = ?, status = ?, total_value = ? WHERE po_id = ?",
        (args.qty, args.status, total_value, args.po_id),
    )
    return {"po_id": args.po_id, "qty": args.qty, "status": args.status}


# ---------------------------------------------------------------------------
# notify_user / send_correction


def _precheck_notify_user(db, args: NotificationArgs) -> None:
    row = db.execute("SELECT 1 FROM users WHERE user_id = ?", (args.to_user,)).fetchone()
    if row is None:
        raise PrecheckFailed(f"recipient {args.to_user} does not exist")


def _run_notify(db, args: NotificationArgs, ctx: ToolContext) -> dict:
    notification_id = f"N-{uuid.uuid4().hex[:8].upper()}"
    db.execute(
        "INSERT INTO notifications (notification_id, to_user, from_user, sent_at, subject, body) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (notification_id, args.to_user, args.from_user, ctx.today.isoformat(), args.subject, args.body),
    )
    return {"notification_id": notification_id, "to_user": args.to_user, "subject": args.subject}


def _compensation_args_notify_user(args: NotificationArgs, result: dict) -> NotificationArgs:
    return NotificationArgs(
        to_user=args.to_user,
        from_user=args.from_user,
        subject=f"Correction: {args.subject}",
        body=f'This supersedes our previous message, "{args.subject}": {args.body}',
    )


# ---------------------------------------------------------------------------
# schedule_check / cancel_task


def _run_schedule_check(db, args: ScheduleCheckArgs, ctx: ToolContext) -> dict:
    task_id = f"T-{uuid.uuid4().hex[:8].upper()}"
    db.execute(
        "INSERT INTO scheduled_tasks (task_id, run_at, kind, payload, status, created_by_run) "
        "VALUES (?, ?, ?, ?, 'pending', ?)",
        (task_id, args.run_at, args.kind, json.dumps(args.payload), args.created_by_run),
    )
    return {"task_id": task_id, "run_at": args.run_at, "kind": args.kind}


def _compensation_args_schedule_check(args: ScheduleCheckArgs, result: dict) -> CancelTaskArgs:
    return CancelTaskArgs(task_id=result["task_id"])


def _run_cancel_task(db, args: CancelTaskArgs, ctx: ToolContext) -> dict:
    db.execute("UPDATE scheduled_tasks SET status = 'cancelled' WHERE task_id = ?", (args.task_id,))
    return {"task_id": args.task_id}


# ---------------------------------------------------------------------------
# reallocate_lot


def _free_qty(db, lot_id: str) -> int:
    lot_qty = db.execute("SELECT qty FROM erp_lots WHERE lot_id = ?", (lot_id,)).fetchone()[0]
    allocated = db.execute(
        "SELECT COALESCE(SUM(qty), 0) FROM erp_lot_allocations WHERE lot_id = ?", (lot_id,)
    ).fetchone()[0]
    return lot_qty - allocated


def _precheck_reallocate_lot(db, args: ReallocateLotArgs) -> None:
    removed_total = sum(entry.qty for entry in args.remove)
    added_total = sum(entry.qty for entry in args.add)
    if added_total != removed_total:
        # A reallocation either fully covers what it's taking off the
        # source lot(s) or it shouldn't happen at all: moving less than
        # removed_total would silently leave the order under-allocated,
        # with nothing else in the harness noticing. Observed against a
        # real model proposing exactly this (partial coverage, no
        # flag_shortage) on the shortage fixture, most of the time.
        raise PrecheckFailed(
            f"reallocation moves {added_total} units but removes {removed_total}; "
            "a reallocation must fully cover what it takes off the source lot(s) "
            "(propose flag_shortage instead if nothing covers the full amount)"
        )

    for entry in args.remove:
        row = db.execute(
            "SELECT qty FROM erp_lot_allocations WHERE lot_id = ? AND prod_order_id = ?",
            (entry.lot_id, args.prod_order_id),
        ).fetchone()
        if row is None:
            raise PrecheckFailed(
                f"no allocation of {entry.lot_id} to {args.prod_order_id} exists"
            )
        if row[0] != entry.qty:
            raise PrecheckFailed(
                f"allocation of {entry.lot_id} to {args.prod_order_id} is {row[0]}, not {entry.qty}"
            )

    for entry in args.add:
        lot_row = db.execute(
            "SELECT part_id, status FROM erp_lots WHERE lot_id = ?", (entry.lot_id,)
        ).fetchone()
        if lot_row is None:
            raise PrecheckFailed(f"lot {entry.lot_id} does not exist")
        part_id, status = lot_row
        if part_id != args.part_id:
            raise PrecheckFailed(f"lot {entry.lot_id} is part {part_id}, not {args.part_id}")
        if status != "released":
            raise PrecheckFailed(f"lot {entry.lot_id} is not released (status={status})")
        if _free_qty(db, entry.lot_id) < entry.qty:
            raise PrecheckFailed(f"lot {entry.lot_id} does not have {entry.qty} free")


def _run_reallocate_lot(db, args: ReallocateLotArgs, ctx: ToolContext) -> dict:
    for entry in args.remove:
        db.execute(
            "DELETE FROM erp_lot_allocations WHERE lot_id = ? AND prod_order_id = ?",
            (entry.lot_id, args.prod_order_id),
        )
    for entry in args.add:
        db.execute(
            "INSERT INTO erp_lot_allocations (lot_id, prod_order_id, qty) VALUES (?, ?, ?) "
            "ON CONFLICT(lot_id, prod_order_id) DO UPDATE SET qty = qty + excluded.qty",
            (entry.lot_id, args.prod_order_id, entry.qty),
        )
    return {
        "prod_order_id": args.prod_order_id,
        "removed": [e.model_dump() for e in args.remove],
        "added": [e.model_dump() for e in args.add],
    }


def _compensation_args_reallocate_lot(args: ReallocateLotArgs, result: dict) -> ReallocateLotArgs:
    return ReallocateLotArgs(
        prod_order_id=args.prod_order_id,
        part_id=args.part_id,
        remove=args.add,
        add=args.remove,
    )


# ---------------------------------------------------------------------------
# flag_shortage / withdraw_flag


def _resolve_purchasing_manager(db) -> str | None:
    row = db.execute("SELECT user_id FROM users WHERE role = 'Purchasing Manager' LIMIT 1").fetchone()
    return row[0] if row else None


def _run_flag_shortage(db, args: FlagShortageArgs, ctx: ToolContext) -> dict:
    item_id = f"AI-{uuid.uuid4().hex[:8].upper()}"
    dedupe_key = f"shortage:{args.part_id}:{args.prod_order_id}"
    owner_id = _resolve_purchasing_manager(db)
    facts: dict[str, Any] = {
        "part_id": args.part_id,
        "prod_order_id": args.prod_order_id,
        "qty_short": args.qty_short,
    }
    db.execute(
        "INSERT INTO attention_items (item_id, dedupe_key, detector, owner_id, summary, facts, "
        "status, created_at) VALUES (?, ?, 'flag_shortage', ?, ?, ?, 'open', ?)",
        (item_id, dedupe_key, owner_id, args.summary, json.dumps(facts), ctx.today.isoformat()),
    )
    return {"item_id": item_id, "dedupe_key": dedupe_key, "owner_id": owner_id}


def _compensation_args_flag_shortage(args: FlagShortageArgs, result: dict) -> WithdrawFlagArgs:
    return WithdrawFlagArgs(item_id=result["item_id"])


def _run_withdraw_flag(db, args: WithdrawFlagArgs, ctx: ToolContext) -> dict:
    db.execute("UPDATE attention_items SET status = 'withdrawn' WHERE item_id = ?", (args.item_id,))
    return {"item_id": args.item_id}


# ---------------------------------------------------------------------------
# Registry

WORKFLOW_REROUTE_PO = "workflow:reroute_po"

_TOOLS: list[Tool] = [
    Tool(
        name="create_po",
        description="Create a new purchase order with an approved supplier.",
        input_schema=CreatePoArgs,
        required_scopes=("erp:po:create",),
        writes=True,
        allowed_in=(WORKFLOW_REROUTE_PO,),
        value=lambda args: round(args.qty * args.unit_price, 2),
        precheck=_precheck_create_po,
        run=_run_create_po,
        compensate="cancel_po",
        compensation_args=_compensation_args_create_po,
    ),
    Tool(
        name="cancel_po",
        description="Cancel an open purchase order.",
        input_schema=CancelPoArgs,
        required_scopes=("erp:po:cancel",),
        writes=True,
        allowed_in=(WORKFLOW_REROUTE_PO,),
        precheck=_precheck_cancel_po,
        run=_run_cancel_po,
        compensate="restore_po",
        compensation_args=_compensation_args_cancel_or_reduce_po,
    ),
    Tool(
        name="reduce_po",
        description="Reduce the quantity on an open purchase order.",
        input_schema=ReducePoArgs,
        required_scopes=("erp:po:cancel",),
        writes=True,
        allowed_in=(WORKFLOW_REROUTE_PO,),
        precheck=_precheck_reduce_po,
        run=_run_reduce_po,
        compensate="restore_po",
        compensation_args=_compensation_args_cancel_or_reduce_po,
    ),
    Tool(
        name="restore_po",
        description="Restore a purchase order's quantity and status. Compensation only.",
        input_schema=RestorePoArgs,
        required_scopes=("erp:po:cancel",),
        writes=True,
        allowed_in=(WORKFLOW_REROUTE_PO,),
        run=_run_restore_po,
    ),
    Tool(
        name="notify_user",
        description=(
            "Send a one-way internal notification to a user. This informs someone; it "
            "creates no tracked follow-up and does not by itself resolve anything still "
            "open. Use it to report on an action already taken, or alongside another tool "
            "that actually does the resolving -- never as a substitute for one."
        ),
        input_schema=NotificationArgs,
        required_scopes=("production:notify",),
        writes=True,
        resolves=False,
        precheck=_precheck_notify_user,
        run=_run_notify,
        compensate="send_correction",
        compensation_args=_compensation_args_notify_user,
    ),
    Tool(
        name="send_correction",
        description="Send a correction superseding an earlier notification. Compensation only.",
        input_schema=NotificationArgs,
        required_scopes=("production:notify",),
        writes=True,
        resolves=False,
        run=_run_notify,
    ),
    Tool(
        name="schedule_check",
        description="Schedule a deferred follow-up task. Workflow-only: created_by_run must be "
                     "the run's own id, which is never part of any context shown to a free-form "
                     "plan, so a free-form proposal could never fill it in correctly.",
        input_schema=ScheduleCheckArgs,
        required_scopes=(),
        writes=True,
        allowed_in=(WORKFLOW_REROUTE_PO,),
        run=_run_schedule_check,
        compensate="cancel_task",
        compensation_args=_compensation_args_schedule_check,
    ),
    Tool(
        name="cancel_task",
        description="Cancel a scheduled task. Compensation only.",
        input_schema=CancelTaskArgs,
        required_scopes=(),
        writes=True,
        allowed_in=(WORKFLOW_REROUTE_PO,),
        run=_run_cancel_task,
    ),
    Tool(
        name="reallocate_lot",
        description="Move allocation of a production order's demand from one set of lots to another.",
        input_schema=ReallocateLotArgs,
        required_scopes=("erp:lot:allocate",),
        writes=True,
        precheck=_precheck_reallocate_lot,
        run=_run_reallocate_lot,
        compensate="reallocate_lot",
        compensation_args=_compensation_args_reallocate_lot,
    ),
    Tool(
        name="flag_shortage",
        description=(
            "Flag a part shortage to purchasing: creates an attention item owned by a "
            "purchasing manager so someone with the authority and tools to source more "
            "stock actually sees and acts on it. Use this whenever nothing available "
            "covers the full requirement -- a notification alone leaves the shortfall "
            "unresolved and nobody tracking it."
        ),
        input_schema=FlagShortageArgs,
        required_scopes=("purchasing:flag",),
        writes=True,
        run=_run_flag_shortage,
        compensate="withdraw_flag",
        compensation_args=_compensation_args_flag_shortage,
    ),
    Tool(
        name="withdraw_flag",
        description="Withdraw a purchasing shortage flag. Compensation only.",
        input_schema=WithdrawFlagArgs,
        required_scopes=("purchasing:flag",),
        writes=True,
        run=_run_withdraw_flag,
    ),
]

TOOLS: dict[str, Tool] = {tool.name: tool for tool in _TOOLS}

# The tools that write to erp_* tables. Section 8: detectors run on every
# tick and also right after a tool writes to an ERP table; the executor
# checks membership here to decide whether to re-run them. Tools outside
# this set only touch harness bookkeeping (notifications, scheduled_tasks,
# attention_items), which no detector reads.
ERP_WRITING_TOOLS: frozenset[str] = frozenset({
    "create_po", "cancel_po", "reduce_po", "restore_po", "reallocate_lot",
})


def get_tool(name: str) -> Tool:
    try:
        return TOOLS[name]
    except KeyError:
        raise UnknownTool(name) from None


def all_tools() -> list[Tool]:
    return list(TOOLS.values())


def user_has_a_resolving_tool(user: User) -> bool:
    """Whether at least one free-form-usable, scope-satisfied tool this
    user could actually propose has `resolves=True`. Used to decide
    whether a NoAction proposal deserves a second look: for a user with
    no resolving tool available at all (missing every relevant scope, with
    every write tool either workflow-only or non-resolving), NoAction is
    the genuinely correct, expected outcome -- retrying it would be
    pointless at best and could push a real model toward inventing an
    action it has no real way to take.
    """

    for tool in TOOLS.values():
        if tool.allowed_in is not None:
            continue
        if not tool.resolves:
            continue
        if not missing_scopes(user, tool.required_scopes):
            return True
    return False
