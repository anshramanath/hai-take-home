"""Scenario A's declared workflow (section 12): reroute an at-risk part to
an approved alternate supplier. Purchasing's fixed order is the step
order, not a suggestion the model could reorder: confirm the alternate
supplier is approved, confirm their lead time meets the production date,
create the new PO, reduce the old one, notify production, schedule the
arrival check.

Steps 1-2 are plain code (no LLM). Steps 3-4 are LLM calls, but bounded:
step 3 may only choose from a pre-filtered candidate list and must justify
it; step 4 may only draft the free-text body of a notification, never its
recipient or the facts inside it (those have no field for the model to set
at all, so there is nothing to validate against, let alone override).
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import date, timedelta
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from harness.execution.args import CreatePoArgs, NotificationArgs, ReducePoArgs, ScheduleCheckArgs
from harness.execution.engine import (
    Step,
    StepContext,
    StepHalted,
    WorkflowDefinition,
    approved_args,
    register,
    run_approved_action,
)
from harness.planning.models import ToolCall
from harness.scheduling.clock import Clock

PROMISED_DATE_PLACEHOLDER = "{PROMISED_DATE}"


class RerouteParams(BaseModel):
    model_config = ConfigDict(extra="forbid")

    part_id: str
    original_po_id: str
    prod_order_id: str
    qty: int = Field(gt=0)
    needed_by: str  # ISO date: when the replacement must arrive by


class ChooseSupplierResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    supplier_id: str
    justification: str


class DraftNotificationResponse(BaseModel):
    """No recipient, no PO numbers, no dates: there is nothing here for the
    model to set that code does not already own. Section 12: "Recipient
    ... and facts ... are filled by code."
    """

    model_config = ConfigDict(extra="forbid")

    body: str


# ---------------------------------------------------------------------------
# Steps 1-2: checks (no LLM)


def _validate_params(ctx: StepContext) -> None:
    """Section 11 invariant 5: the gate (here, step 1 itself) enforces this
    in code, not just in the workflow's description text. A real model
    once proposed this workflow for a quality-hold item with a production
    order id standing in for original_po_id; prompt wording alone cannot
    be trusted to prevent a repeat.
    """

    state = ctx.state
    part_id = state["part_id"]
    original_po_id = state["original_po_id"]
    prod_order_id = state["prod_order_id"]
    qty = state["qty"]

    po_row = ctx.conn.execute(
        "SELECT part_id, status, qty FROM erp_purchase_orders WHERE po_id = ?", (original_po_id,)
    ).fetchone()
    if po_row is None:
        raise StepHalted("halted_invalid_params", f"original_po_id {original_po_id!r} does not exist")
    po_part_id, po_status, po_qty = po_row
    if po_status != "open":
        raise StepHalted(
            "halted_invalid_params", f"original PO {original_po_id} is not open (status={po_status})"
        )
    if po_part_id != part_id:
        raise StepHalted(
            "halted_invalid_params",
            f"original PO {original_po_id} is for part {po_part_id}, not {part_id}",
        )
    if qty > po_qty:
        raise StepHalted(
            "halted_invalid_params",
            f"qty {qty} exceeds original PO {original_po_id}'s open quantity {po_qty}",
        )

    prod_row = ctx.conn.execute(
        "SELECT components FROM erp_production_orders WHERE prod_order_id = ?", (prod_order_id,)
    ).fetchone()
    if prod_row is None:
        raise StepHalted("halted_invalid_params", f"prod_order_id {prod_order_id!r} does not exist")
    components = {component["part_id"] for component in json.loads(prod_row[0])}
    if part_id not in components:
        raise StepHalted(
            "halted_invalid_params",
            f"production order {prod_order_id} does not consume part {part_id}",
        )


def _confirm_supplier_approved(ctx: StepContext) -> dict[str, Any]:
    _validate_params(ctx)
    part_id = ctx.state["part_id"]
    rows = ctx.conn.execute(
        "SELECT supplier_id, approved, approved_parts FROM erp_suppliers"
    ).fetchall()
    candidates: list[str] = []
    excluded: list[dict[str, str]] = []
    for supplier_id, approved, approved_parts_json in rows:
        if approved and part_id in json.loads(approved_parts_json):
            candidates.append(supplier_id)
        else:
            excluded.append({"supplier_id": supplier_id, "reason": "not approved for this part"})
    if not candidates:
        raise StepHalted("halted_no_supplier", f"no supplier is approved for {part_id}")
    return {"candidates": candidates, "excluded_by_approval": excluded}


def _confirm_lead_time(ctx: StepContext) -> dict[str, Any]:
    needed_by = date.fromisoformat(ctx.state["needed_by"])
    today = ctx.clock.today()
    surviving: list[str] = []
    excluded: list[dict[str, str]] = []
    for supplier_id in ctx.state["candidates"]:
        lead_time_days = ctx.conn.execute(
            "SELECT lead_time_days FROM erp_suppliers WHERE supplier_id = ?", (supplier_id,)
        ).fetchone()[0]
        eta = today + timedelta(days=lead_time_days)
        if eta <= needed_by:
            surviving.append(supplier_id)
        else:
            excluded.append({
                "supplier_id": supplier_id,
                "reason": f"eta {eta.isoformat()} misses needed_by {needed_by.isoformat()}",
            })
    if not surviving:
        raise StepHalted("halted_no_supplier", "no approved supplier can meet the need date")
    return {"candidates": surviving, "excluded_by_lead_time": excluded}


# ---------------------------------------------------------------------------
# Steps 3-4: bounded LLM calls


def _choose_supplier(ctx: StepContext) -> dict[str, Any]:
    candidates: list[str] = ctx.state["candidates"]
    placeholders = ",".join("?" * len(candidates))
    supplier_info = {
        row[0]: {"name": row[1], "lead_time_days": row[2], "price": json.loads(row[3]).get(ctx.state["part_id"])}
        for row in ctx.conn.execute(
            f"SELECT supplier_id, name, lead_time_days, pricing FROM erp_suppliers "
            f"WHERE supplier_id IN ({placeholders})",
            candidates,
        ).fetchall()
    }
    messages = [
        {
            "role": "system",
            "content": (
                "Choose the best supplier from the pre-approved candidates for an urgent "
                "parts reroute. Respond with the supplier_id and a one-sentence justification. "
                "You may only choose a supplier_id from the candidates given."
            ),
        },
        {"role": "user", "content": json.dumps({"part_id": ctx.state["part_id"], "candidates": supplier_info})},
    ]

    error: str | None = None
    for _ in range(2):
        response = ctx.llm_client.complete(messages, ChooseSupplierResponse)
        if response.supplier_id in candidates:
            return {"chosen_supplier": response.supplier_id, "supplier_justification": response.justification}
        error = f"{response.supplier_id!r} is not one of the approved candidates {candidates}"
        messages = messages + [{"role": "user", "content": f"Invalid: {error}. Choose again from {candidates}."}]

    raise StepHalted("failed", f"planner could not settle on a valid supplier: {error}")


def _draft_notification(ctx: StepContext) -> dict[str, Any]:
    messages = [
        {
            "role": "system",
            "content": (
                "Draft a brief notification body telling production about a parts reroute. "
                "Write only the body text; exact PO numbers, dates, and the recipient are "
                "filled in separately."
            ),
        },
        {
            "role": "user",
            "content": json.dumps({
                "part_id": ctx.state["part_id"],
                "prod_order_id": ctx.state["prod_order_id"],
                "chosen_supplier": ctx.state["chosen_supplier"],
            }),
        },
    ]
    response = ctx.llm_client.complete(messages, DraftNotificationResponse)
    return {"notification_draft": response.body}


# ---------------------------------------------------------------------------
# Steps 5-8: actions, run only after approval, against the frozen plan


def _create_po_step(ctx: StepContext) -> dict[str, Any]:
    """create_po's own result carries the real promised_date (F3: a fact
    the supplier system hands back at order-placement time, computed from
    execution-time "today", not something frozen into the plan at
    approval). Surfaced into state so the later steps, which only know the
    placeholder, can substitute the real value in.
    """

    updates = run_approved_action(ctx, "create_po", "create_po")
    updates["new_promised_date"] = updates["_compensation_log"][-1]["result"]["promised_date"]
    return updates


def _reduce_original_po_step(ctx: StepContext) -> dict[str, Any]:
    return run_approved_action(ctx, "reduce_po", "reduce_original_po")


def _notify_production_step(ctx: StepContext) -> dict[str, Any]:
    body = approved_args(ctx.state, "notify_user")["body"].replace(
        PROMISED_DATE_PLACEHOLDER, ctx.state["new_promised_date"]
    )
    return run_approved_action(ctx, "notify_user", "notify_production", overrides={"body": body})


def _schedule_arrival_check_step(ctx: StepContext) -> dict[str, Any]:
    return run_approved_action(
        ctx, "schedule_check", "schedule_arrival_check",
        overrides={"run_at": ctx.state["new_promised_date"]},
    )


# ---------------------------------------------------------------------------
# Building the plan to approve: the one place steps 5-8's args are computed


def _compute_create_po_args(conn: sqlite3.Connection, state: dict[str, Any]) -> CreatePoArgs:
    supplier_id = state["chosen_supplier"]
    part_id = state["part_id"]
    pricing_json = conn.execute(
        "SELECT pricing FROM erp_suppliers WHERE supplier_id = ?", (supplier_id,)
    ).fetchone()[0]
    unit_price = json.loads(pricing_json)[part_id]
    return CreatePoArgs(
        po_id=f"PO-{uuid.uuid4().hex[:8].upper()}",
        part_id=part_id,
        supplier_id=supplier_id,
        qty=state["qty"],
        unit_price=unit_price,
        needed_by=state["needed_by"],
        created_by=state["_requester_id"],
    )


def _compute_reduce_po_args(conn: sqlite3.Connection, state: dict[str, Any]) -> ReducePoArgs:
    original_po_id = state["original_po_id"]
    current_qty = conn.execute(
        "SELECT qty FROM erp_purchase_orders WHERE po_id = ?", (original_po_id,)
    ).fetchone()[0]
    new_qty = max(current_qty - state["qty"], 0)
    return ReducePoArgs(po_id=original_po_id, new_qty=new_qty)


def _compute_notify_args(
    conn: sqlite3.Connection, state: dict[str, Any], create_args: CreatePoArgs
) -> NotificationArgs:
    supervisor_id = conn.execute(
        "SELECT supervisor_id FROM erp_production_orders WHERE prod_order_id = ?", (state["prod_order_id"],)
    ).fetchone()[0]
    subject = f"Parts reroute for production order {state['prod_order_id']}"
    body = (
        f"{state['notification_draft']}\n\n"
        f"Part {state['part_id']}: replacement PO {create_args.po_id} placed with "
        f"{create_args.supplier_id}, expected {PROMISED_DATE_PLACEHOLDER}. "
        f"Original PO {state['original_po_id']} has been reduced accordingly."
    )
    return NotificationArgs(to_user=supervisor_id, from_user=state["_requester_id"], subject=subject, body=body)


def _compute_schedule_check_args(
    state: dict[str, Any], create_args: CreatePoArgs, run_id: str
) -> ScheduleCheckArgs:
    payload = {"po_id": create_args.po_id, "part_id": state["part_id"], "prod_order_id": state["prod_order_id"]}
    return ScheduleCheckArgs(
        run_at=PROMISED_DATE_PLACEHOLDER, kind="arrival_check", payload=payload, created_by_run=run_id
    )


def build_plan_steps(
    conn: sqlite3.Connection, clock: Clock, state: dict[str, Any], run_id: str
) -> list[ToolCall]:
    create_args = _compute_create_po_args(conn, state)
    reduce_args = _compute_reduce_po_args(conn, state)
    notify_args = _compute_notify_args(conn, state, create_args)
    schedule_args = _compute_schedule_check_args(state, create_args, run_id)
    return [
        ToolCall(tool="create_po", args=json.loads(create_args.model_dump_json())),
        ToolCall(tool="reduce_po", args=json.loads(reduce_args.model_dump_json())),
        ToolCall(tool="notify_user", args=json.loads(notify_args.model_dump_json())),
        ToolCall(tool="schedule_check", args=json.loads(schedule_args.model_dump_json())),
    ]


REROUTE_PO_V1 = WorkflowDefinition(
    name="reroute_po",
    version=1,
    description=(
        "Use only when there is an existing open purchase order for the part (a real "
        "original_po_id already in the ERP) whose promised delivery is at risk or confirmed "
        "delayed by the supplier itself (for example, a supplier email reporting a slipped "
        "ship date) and a production order depends on that purchase order arriving in time. "
        "Reroutes the at-risk quantity to an approved alternate supplier: confirms the "
        "alternate is approved and can meet the need date, creates a replacement PO, "
        "reduces the original PO by the rerouted quantity, notifies production, and "
        "schedules a check that the replacement actually arrives.\n\n"
        "Does NOT apply when there is no existing purchase order to reroute: a lot already "
        "received into inventory being on quality hold, or any situation where the fix is "
        "reallocating stock you already have rather than a supplier's shipment, is a "
        "different kind of problem and should get a free-form plan instead, never this "
        "workflow with a guessed or repurposed original_po_id."
    ),
    params_model=RerouteParams,
    steps=(
        Step(name="confirm_supplier_approved", kind="check", fn=_confirm_supplier_approved),
        Step(name="confirm_lead_time", kind="check", fn=_confirm_lead_time),
        Step(name="choose_supplier", kind="llm", fn=_choose_supplier),
        Step(name="draft_notification", kind="llm", fn=_draft_notification),
        Step(name="create_po", kind="action", fn=_create_po_step, compensate="cancel_po"),
        Step(name="reduce_original_po", kind="action", fn=_reduce_original_po_step, compensate="restore_po"),
        Step(name="notify_production", kind="action", fn=_notify_production_step, compensate="send_correction"),
        Step(
            name="schedule_arrival_check", kind="action", fn=_schedule_arrival_check_step, compensate="cancel_task",
        ),
    ),
    build_plan_steps=build_plan_steps,
    applies_to_detectors=("stockout", "arrival_check"),
)

register(REROUTE_PO_V1)
