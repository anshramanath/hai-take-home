"""StockoutDetector (section 8). For each planned production order within a
7-day horizon, projects each component part's balance from today to the
order's start: start at on_hand, subtract daily_usage for every day in
that window (background demand), add qty from open POs promised within
the window, subtract other planned orders' demand for the same part on
their own start date if it falls in the window. Raises an item when the
projected balance is short, or when it only clears the bar because of an
inbound PO landing within MARGIN_DAYS of the start date (thin margin: the
ERP shows the part as covered, but that coverage is fragile).

For 4812/P-4471 this fires on the thin-margin path: PO-77812 (promised
9/4) is what keeps the projection non-negative by 9/7, and it lands only 3
days out. The detector doesn't know the shipment has actually slipped (the
ERP still shows it on time) — that confirmation is the email, which the
planner reads via the mail provider.

The inbound window is `today <= promised_date <= scheduled_start`
(inclusive of today): on the day a PO is promised, the ERP still reads it
as on time, so the detector credits it. Found empirically — an earlier
strict `today <` excluded a PO on the exact day it was due, which meant
once the clock reached that date with no receipt recorded yet, a fresh
stockout fired for supply that was, as far as the ERP was concerned, still
on schedule. This matters across a tick boundary: Scenario A's follow-up
check lands on the same day the replacement PO is promised, and the
detector re-runs (since create_po/reduce_po touch an ERP table) in that
same tick.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import date, timedelta

from harness.detection.base import AttentionItem, DetectionContext

HORIZON_DAYS = 7
MARGIN_DAYS = 3


class StockoutDetector:
    name = "stockout"

    def detect(self, ctx: DetectionContext) -> list[AttentionItem]:
        conn = ctx.conn
        today = ctx.clock.today()
        horizon_end = today + timedelta(days=HORIZON_DAYS)

        items: list[AttentionItem] = []
        orders = conn.execute(
            "SELECT prod_order_id, scheduled_start, components FROM erp_production_orders "
            "WHERE status = 'planned'"
        ).fetchall()

        for prod_order_id, scheduled_start_s, components_json in orders:
            scheduled_start = date.fromisoformat(scheduled_start_s)
            if not (today <= scheduled_start <= horizon_end):
                continue
            for component in json.loads(components_json):
                item = self._check_component(
                    conn, today, prod_order_id, scheduled_start, component["part_id"], component["qty"]
                )
                if item is not None:
                    items.append(item)
        return items

    def _check_component(
        self,
        conn: sqlite3.Connection,
        today: date,
        prod_order_id: str,
        scheduled_start: date,
        part_id: str,
        required_qty: int,
    ) -> AttentionItem | None:
        on_hand, daily_usage = conn.execute(
            "SELECT on_hand, daily_usage FROM erp_parts WHERE part_id = ?", (part_id,)
        ).fetchone()

        inbound_in_window = [
            (po_id, date.fromisoformat(promised_s), qty, created_by, supplier_id)
            for po_id, promised_s, qty, created_by, supplier_id in conn.execute(
                "SELECT po_id, promised_date, qty, created_by, supplier_id FROM erp_purchase_orders "
                "WHERE part_id = ? AND status = 'open'",
                (part_id,),
            ).fetchall()
            if today <= date.fromisoformat(promised_s) <= scheduled_start
        ]

        other_demand = 0
        for other_id, other_start_s, other_components_json in conn.execute(
            "SELECT prod_order_id, scheduled_start, components FROM erp_production_orders "
            "WHERE status = 'planned' AND prod_order_id != ?",
            (prod_order_id,),
        ).fetchall():
            if date.fromisoformat(other_start_s) != scheduled_start:
                continue
            for component in json.loads(other_components_json):
                if component["part_id"] == part_id:
                    other_demand += component["qty"]

        num_days = (scheduled_start - today).days
        inbound_total = sum(qty for _, _, qty, _, _ in inbound_in_window)
        balance = on_hand - daily_usage * num_days + inbound_total - other_demand

        if balance < required_qty:
            if inbound_in_window:
                po_id, _, _, created_by, supplier_id = inbound_in_window[0]
            else:
                po_id, created_by, supplier_id = "none", None, None
            return self._make_item(
                conn, "short", part_id, prod_order_id, po_id, created_by, supplier_id,
                balance, required_qty, scheduled_start,
            )

        for po_id, promised_date, qty, created_by, supplier_id in inbound_in_window:
            if (scheduled_start - promised_date).days > MARGIN_DAYS:
                continue
            if balance - qty < required_qty:
                return self._make_item(
                    conn, "thin_margin", part_id, prod_order_id, po_id, created_by, supplier_id,
                    balance, required_qty, scheduled_start, promised_date=promised_date,
                )

        return None

    def _make_item(
        self,
        conn: sqlite3.Connection,
        condition: str,
        part_id: str,
        prod_order_id: str,
        inbound_po_id: str,
        owner_id: str | None,
        supplier_id: str | None,
        balance: int,
        required_qty: int,
        scheduled_start: date,
        promised_date: date | None = None,
    ) -> AttentionItem:
        if owner_id is None:
            fallback = conn.execute(
                "SELECT user_id FROM users WHERE role = 'Purchasing Manager' LIMIT 1"
            ).fetchone()
            owner_id = fallback[0] if fallback else None

        facts: dict[str, object] = {
            "part_id": part_id,
            "prod_order_id": prod_order_id,
            "inbound_po_id": inbound_po_id,
            "supplier_id": supplier_id,
            "condition": condition,
            "balance": balance,
            "required_qty": required_qty,
            "scheduled_start": scheduled_start.isoformat(),
            "needed_by": scheduled_start.isoformat(),
        }
        if promised_date is not None:
            facts["promised_date"] = promised_date.isoformat()

        summary = (
            f"Part {part_id} is projected to cover production order {prod_order_id} "
            f"({condition.replace('_', ' ')}): needs {required_qty}, projected balance {balance}."
        )
        return AttentionItem(
            detector=self.name,
            dedupe_key=f"stockout:{part_id}:{prod_order_id}:{inbound_po_id}",
            owner_id=owner_id,
            summary=summary,
            facts=facts,
        )
