"""Recording a receipt is an external-world event (a warehouse clerk
scanning in a shipment), not an agent action: it carries no scopes, no
gate, no approval. It exists purely so the arrival-check task has a fact
to check (section 5).
"""

from __future__ import annotations

import sqlite3
import uuid

from harness.scheduling.clock import Clock


def record_receipt(conn: sqlite3.Connection, clock: Clock, *, po_id: str, qty: int) -> str:
    receipt_id = f"RCPT-{uuid.uuid4().hex[:8].upper()}"
    conn.execute(
        "INSERT INTO erp_receipts (receipt_id, po_id, qty, received_date) VALUES (?, ?, ?, ?)",
        (receipt_id, po_id, qty, clock.today().isoformat()),
    )
    conn.commit()
    return receipt_id
