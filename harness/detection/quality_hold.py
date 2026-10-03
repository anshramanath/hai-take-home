"""QualityHoldDetector (section 8): for each allocation on a lot with
status 'hold', raises an item if the production order it's allocated to
starts within HORIZON_DAYS. Owner is the lot's hold_placed_by (the
quality inspector who placed the hold), falling back to any user with
role 'Quality Manager' — the same two-tier resolution StockoutDetector
uses for its inbound PO's created_by.
"""

from __future__ import annotations

import sqlite3
from datetime import date, timedelta

from harness.detection.base import AttentionItem, DetectionContext

HORIZON_DAYS = 3


class QualityHoldDetector:
    name = "quality_hold"

    def detect(self, ctx: DetectionContext) -> list[AttentionItem]:
        conn = ctx.conn
        today = ctx.clock.today()
        horizon_end = today + timedelta(days=HORIZON_DAYS)

        items: list[AttentionItem] = []
        for lot_id, part_id, hold_placed_by in conn.execute(
            "SELECT lot_id, part_id, hold_placed_by FROM erp_lots WHERE status = 'hold'"
        ).fetchall():
            for prod_order_id, qty in conn.execute(
                "SELECT prod_order_id, qty FROM erp_lot_allocations WHERE lot_id = ?", (lot_id,)
            ).fetchall():
                order_row = conn.execute(
                    "SELECT scheduled_start, status FROM erp_production_orders WHERE prod_order_id = ?",
                    (prod_order_id,),
                ).fetchone()
                if order_row is None:
                    continue
                scheduled_start_s, status = order_row
                if status != "planned":
                    continue
                scheduled_start = date.fromisoformat(scheduled_start_s)
                if not (today <= scheduled_start <= horizon_end):
                    continue

                items.append(AttentionItem(
                    detector=self.name,
                    dedupe_key=f"quality_hold:{lot_id}:{prod_order_id}",
                    owner_id=self._resolve_owner(conn, hold_placed_by),
                    summary=(
                        f"Lot {lot_id} ({part_id}) is on quality hold and allocated {qty} units "
                        f"to production order {prod_order_id}, starting {scheduled_start.isoformat()}."
                    ),
                    facts={
                        "lot_id": lot_id, "part_id": part_id, "prod_order_id": prod_order_id,
                        "qty": qty, "scheduled_start": scheduled_start.isoformat(),
                        "needed_by": scheduled_start.isoformat(),
                    },
                ))
        return items

    def _resolve_owner(self, conn: sqlite3.Connection, hold_placed_by: str | None) -> str | None:
        if hold_placed_by:
            return hold_placed_by
        fallback = conn.execute("SELECT user_id FROM users WHERE role = 'Quality Manager' LIMIT 1").fetchone()
        return fallback[0] if fallback else None
