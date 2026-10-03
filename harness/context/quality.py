"""QualityProvider (section 9, Scenario B): the held lot, its allocations,
the affected production order, and every released lot of the same part
with free quantity computed (qty minus its own allocations) — what the
planner needs to decide whether another lot can cover the hold.
"""

from __future__ import annotations

import json

from harness.context.base import ContextSlice, ProviderContext
from harness.detection.base import AttentionItem
from harness.world.users import User


class QualityProvider:
    source = "quality"

    def fetch(self, ctx: ProviderContext, user: User, item: AttentionItem) -> ContextSlice:
        if "erp:lot:read" not in user.scopes:
            return ContextSlice(source=self.source, records=[], record_ids=[])

        conn = ctx.conn
        lot_id = item.facts.get("lot_id")
        part_id = item.facts.get("part_id")
        prod_order_id = item.facts.get("prod_order_id")

        records: list[dict] = []
        record_ids: list[str] = []

        if lot_id:
            row = conn.execute(
                "SELECT lot_id, part_id, qty, status, received_date, hold_reason, hold_placed_by, "
                "hold_placed_on FROM erp_lots WHERE lot_id = ?", (lot_id,)
            ).fetchone()
            if row:
                records.append({"type": "held_lot", **dict(row)})
                record_ids.append(lot_id)

            for alloc_row in conn.execute(
                "SELECT lot_id, prod_order_id, qty FROM erp_lot_allocations WHERE lot_id = ?", (lot_id,)
            ).fetchall():
                records.append({"type": "allocation", **dict(alloc_row)})

        if prod_order_id:
            row = conn.execute(
                "SELECT prod_order_id, product, qty, scheduled_start, scheduled_end, status, line, "
                "supervisor_id, components FROM erp_production_orders WHERE prod_order_id = ?",
                (prod_order_id,),
            ).fetchone()
            if row:
                rec = dict(row)
                rec["components"] = json.loads(rec["components"])
                records.append({"type": "production_order", **rec})
                record_ids.append(prod_order_id)

        if part_id:
            for lot_row in conn.execute(
                "SELECT lot_id, part_id, qty FROM erp_lots WHERE part_id = ? AND status = 'released'",
                (part_id,),
            ).fetchall():
                allocated = conn.execute(
                    "SELECT COALESCE(SUM(qty), 0) FROM erp_lot_allocations WHERE lot_id = ?",
                    (lot_row["lot_id"],),
                ).fetchone()[0]
                # Only free_qty is shown, not the lot's raw total: the raw
                # total is not what's available to pull from this lot, and
                # showing both invites exactly the mistake of reallocating
                # against the wrong number (observed against a real model).
                records.append({
                    "type": "released_lot", "lot_id": lot_row["lot_id"], "part_id": lot_row["part_id"],
                    "free_qty": lot_row["qty"] - allocated,
                })
                record_ids.append(lot_row["lot_id"])

        return ContextSlice(source=self.source, records=records, record_ids=record_ids)
