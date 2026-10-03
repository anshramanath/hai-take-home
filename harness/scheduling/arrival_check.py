"""The arrival_check task (section 13): confirms whether a rerouted PO's
replacement shipment actually arrived. Received in full: log success and
write a confirmed-outcome memory fact about the replacement supplier.
Not received: the loop re-enters through a brand new attention item keyed
on the new PO, exactly like a fresh stockout detection — the ERP-shaped
risk is identical, only the dependent PO has changed.
"""

from __future__ import annotations

import sqlite3
from typing import Any

from harness.audit.log import log as audit_log
from harness.detection.base import AttentionItem
from harness.detection.registry import raise_attention_item
from harness.memory.facts import write_fact
from harness.memory.runs import update_run_state
from harness.scheduling.clock import Clock
from harness.scheduling.tasks import register_handler


def _resolve_owner(conn: sqlite3.Connection, created_by: str | None) -> str | None:
    if created_by:
        return created_by
    fallback = conn.execute("SELECT user_id FROM users WHERE role = 'Purchasing Manager' LIMIT 1").fetchone()
    return fallback[0] if fallback else None


def _handle_arrival_check(conn: sqlite3.Connection, clock: Clock, task: dict[str, Any]) -> None:
    payload = task["payload"]
    po_id = payload["po_id"]
    part_id = payload["part_id"]
    prod_order_id = payload["prod_order_id"]
    run_id = task["created_by_run"]

    po_row = conn.execute(
        "SELECT qty, supplier_id, created_by FROM erp_purchase_orders WHERE po_id = ?", (po_id,)
    ).fetchone()
    ordered_qty, supplier_id, created_by = po_row

    received_total = conn.execute(
        "SELECT COALESCE(SUM(qty), 0) FROM erp_receipts WHERE po_id = ?", (po_id,)
    ).fetchone()[0]

    if received_total >= ordered_qty:
        audit_log(
            conn, clock, run_id=run_id, actor="scheduler", event="arrival_check.confirmed",
            detail={"po_id": po_id, "received": received_total, "ordered": ordered_qty},
        )
        conn.commit()
        update_run_state(conn, run_id, {"arrival_confirmed": True})
        write_fact(
            conn, clock, subject=supplier_id,
            fact=f"{supplier_id} delivered {po_id} in full, as promised.",
            source_ids=[po_id],
        )
        return

    scheduled_start = conn.execute(
        "SELECT scheduled_start FROM erp_production_orders WHERE prod_order_id = ?", (prod_order_id,)
    ).fetchone()[0]

    audit_log(
        conn, clock, run_id=run_id, actor="scheduler", event="arrival_check.missed",
        detail={"po_id": po_id, "received": received_total, "ordered": ordered_qty},
    )
    conn.commit()

    item = AttentionItem(
        detector="arrival_check",
        dedupe_key=f"stockout:{part_id}:{prod_order_id}:{po_id}",
        owner_id=_resolve_owner(conn, created_by),
        summary=(
            f"Replacement PO {po_id} for {part_id} has not arrived as promised; "
            f"production order {prod_order_id} is at risk again."
        ),
        facts={
            "part_id": part_id, "prod_order_id": prod_order_id, "inbound_po_id": po_id,
            "supplier_id": supplier_id, "condition": "missed_arrival",
            "needed_by": scheduled_start, "received": received_total, "ordered": ordered_qty,
        },
    )
    raise_attention_item(conn, clock, "arrival_check", item)


register_handler("arrival_check", _handle_arrival_check)
