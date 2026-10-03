"""Gathers every registered provider's slice for one user and one
attention item, and audits exactly which record ids were returned per
source — the thing `explain` (phase 5) will need to reconstruct what the
agent saw.
"""

from __future__ import annotations

import sqlite3

from harness.audit.log import log as audit_log
from harness.context.base import ContextSlice, ProviderContext
from harness.context.calendar import CalendarProvider
from harness.context.erp import ErpProvider
from harness.context.mail import MailProvider
from harness.context.quality import QualityProvider
from harness.detection.base import AttentionItem
from harness.scheduling.clock import Clock
from harness.world.users import User

PROVIDERS: list = [ErpProvider(), MailProvider(), CalendarProvider(), QualityProvider()]


def gather_context(
    conn: sqlite3.Connection, clock: Clock, user: User, item: AttentionItem, *, run_id: str | None = None
) -> dict[str, ContextSlice]:
    ctx = ProviderContext(conn=conn, clock=clock)
    slices: dict[str, ContextSlice] = {}
    record_ids_by_source: dict[str, list[str]] = {}

    for provider in PROVIDERS:
        slice_ = provider.fetch(ctx, user, item)
        slices[provider.source] = slice_
        record_ids_by_source[provider.source] = slice_.record_ids

    audit_log(
        conn, clock, run_id=run_id, actor="context", event="context.gathered",
        detail={"user_id": user.user_id, "record_ids": record_ids_by_source},
    )
    conn.commit()
    return slices
