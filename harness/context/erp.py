"""ErpProvider (section 9): the part, its open POs, the production order,
and every supplier that has anything to do with the part — approved or
not, including the traps — so the planner sees the temptation and the
workflow's own checks are what filter it out, not the provider hiding it
upfront.
"""

from __future__ import annotations

import json

from harness.context.base import ContextSlice, ProviderContext
from harness.detection.base import AttentionItem
from harness.world.users import User


class ErpProvider:
    source = "erp"

    def fetch(self, ctx: ProviderContext, user: User, item: AttentionItem) -> ContextSlice:
        conn = ctx.conn
        part_id = item.facts.get("part_id")
        prod_order_id = item.facts.get("prod_order_id")
        has_po_read = "erp:po:read" in user.scopes
        has_production_read = "erp:production:read" in user.scopes

        records: list[dict] = []
        record_ids: list[str] = []

        if part_id and (has_po_read or has_production_read):
            row = conn.execute(
                "SELECT part_id, description, on_hand, daily_usage, safety_stock, unit_cost, lot_tracked "
                "FROM erp_parts WHERE part_id = ?",
                (part_id,),
            ).fetchone()
            if row:
                records.append({"type": "part", **dict(row)})
                record_ids.append(part_id)

        if prod_order_id and has_production_read:
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

        if part_id and has_po_read:
            for row in conn.execute(
                "SELECT po_id, part_id, supplier_id, qty, unit_price, total_value, ordered_date, "
                "promised_date, status, created_by FROM erp_purchase_orders WHERE part_id = ? AND status = 'open'",
                (part_id,),
            ).fetchall():
                records.append({"type": "purchase_order", **dict(row)})
                record_ids.append(row["po_id"])

            for row in conn.execute(
                "SELECT supplier_id, name, contact_email, approved, approved_parts, lead_time_days, pricing "
                "FROM erp_suppliers"
            ).fetchall():
                approved_parts = json.loads(row["approved_parts"])
                pricing = json.loads(row["pricing"])
                if part_id not in approved_parts and part_id not in pricing:
                    continue
                rec = dict(row)
                rec["approved_parts"] = approved_parts
                rec["pricing"] = pricing
                records.append({"type": "supplier", **rec})
                record_ids.append(row["supplier_id"])

        return ContextSlice(source=self.source, records=records, record_ids=record_ids)
