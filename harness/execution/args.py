"""Pydantic input schemas for every registered tool. `extra="forbid"` on
each one so a tool call cannot smuggle in fields it didn't declare.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class CreatePoArgs(BaseModel):
    """`po_id` is supplied by the caller rather than generated inside
    create_po's run(), so a workflow can decide it before approval and have
    it be part of the frozen, approved plan: later steps (the notification,
    the arrival-check schedule) need to reference the new PO's id, and
    nothing may be computed between approval and execution.
    """

    model_config = ConfigDict(extra="forbid")

    po_id: str
    part_id: str
    supplier_id: str
    qty: int = Field(gt=0)
    unit_price: float = Field(gt=0)
    promised_date: str
    created_by: str


class CancelPoArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    po_id: str


class ReducePoArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    po_id: str
    new_qty: int = Field(ge=0)


class RestorePoArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    po_id: str
    qty: int = Field(ge=0)
    status: str


class NotificationArgs(BaseModel):
    """Shared shape for notify_user and its compensation, send_correction."""

    model_config = ConfigDict(extra="forbid")

    to_user: str
    from_user: str
    subject: str
    body: str


class ScheduleCheckArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    run_at: str
    kind: str
    payload: dict[str, Any]
    created_by_run: str


class CancelTaskArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    task_id: str


class AllocationEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")

    lot_id: str
    qty: int = Field(gt=0)


class ReallocateLotArgs(BaseModel):
    """Generalized beyond the appendix's `from_lot: str` into symmetric
    `remove`/`add` lists so the tool can compensate for itself: undoing a
    reallocation is just calling it again with the two lists swapped. The
    forward case (one held lot emptied into one or more released lots)
    still only ever populates `remove` with one entry.
    """

    model_config = ConfigDict(extra="forbid")

    prod_order_id: str
    part_id: str
    remove: list[AllocationEntry] = Field(min_length=1)
    add: list[AllocationEntry] = Field(min_length=1)


class FlagShortageArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    part_id: str
    prod_order_id: str
    owner_id: str
    qty_short: int = Field(gt=0)
    summary: str


class WithdrawFlagArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    item_id: str
