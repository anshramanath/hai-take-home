"""Tier 2, T2 of FIXES (1).md: every change Scenario A makes to the fake
company's tables (and to notifications/scheduled_tasks) must be traceable
to an `action.executed` or `action.compensated` audit event carrying a
run id. Snapshots every row before and after, diffs by primary key, and
confirms each changed id is actually named inside some audit detail.

Scoped to detection through execution, deliberately short of the
follow-up: recording a receipt is an external-world event (a warehouse
clerk scanning a shipment in), not a gated agent action (world/receipts.py
says so explicitly), so it has no action.executed counterpart by design,
and including it here would be asserting a claim the codebase never made.
"""

from __future__ import annotations

import json

from harness.app import approve, tick
from harness.execution.workflows.reroute_po import ChooseSupplierResponse, DraftNotificationResponse
from harness.planning.llm import FakeLLMClient
from harness.planning.models import PlannerOutput, WorkflowRequest

# table -> primary key column(s); only tables Scenario A can plausibly touch.
TABLES = {
    "erp_parts": "part_id",
    "erp_suppliers": "supplier_id",
    "erp_purchase_orders": "po_id",
    "erp_production_orders": "prod_order_id",
    "erp_receipts": "receipt_id",
    "notifications": "notification_id",
    "scheduled_tasks": "task_id",
}


def _snapshot(conn) -> dict[str, dict[str, dict]]:
    snapshot = {}
    for table, pk in TABLES.items():
        rows = conn.execute(f"SELECT * FROM {table}").fetchall()
        snapshot[table] = {row[pk]: dict(row) for row in rows}
    return snapshot


def _changed_ids(before: dict, after: dict) -> set[str]:
    changed = set()
    for table in TABLES:
        for row_id, row in after[table].items():
            if before[table].get(row_id) != row:
                changed.add(row_id)
    return changed


def test_every_changed_row_in_scenario_a_maps_to_an_audited_action(make_harness):
    h = make_harness("scenario_a")
    llm = FakeLLMClient([
        PlannerOutput(proposal=WorkflowRequest(
            kind="workflow", workflow="reroute_po",
            params={
                "part_id": "P-4471", "original_po_id": "PO-77812", "prod_order_id": "4812",
                "qty": 120, "needed_by": "2026-09-07",
            },
            reasoning="Supplier Y slipped per M-001.", summary_for_user="Reroute to Z.",
        )),
        ChooseSupplierResponse(supplier_id="S-Z", justification="Only valid candidate."),
        DraftNotificationResponse(body="Reroute in progress."),
    ])

    before = _snapshot(h.conn)
    tick(h.conn, h.clock, llm)  # 9/2 -> 9/3
    tick(h.conn, h.clock, llm)  # 9/3 -> 9/4: escalates to Priya
    approval = h.conn.execute("SELECT approval_id FROM approvals").fetchone()
    approve(h.conn, h.clock, llm, approval_id=approval["approval_id"], decided_by="u-102")
    after = _snapshot(h.conn)

    changed_ids = _changed_ids(before, after)
    assert changed_ids, "nothing changed; this test would pass vacuously otherwise"
    # PO-77812 (reduced), the new S-Z PO, one notification, one scheduled task.
    assert len(changed_ids) == 4

    action_rows = h.conn.execute(
        "SELECT run_id, detail FROM audit_log WHERE event IN ('action.executed', 'action.compensated')"
    ).fetchall()
    assert action_rows, "no action.executed/action.compensated events were logged at all"
    for run_id, _detail in action_rows:
        assert run_id is not None, "an action event was logged with no run_id"

    combined_detail_text = " ".join(detail for _run_id, detail in action_rows)
    for row_id in changed_ids:
        assert row_id in combined_detail_text, (
            f"{row_id!r} changed but is not named in any action.executed/action.compensated detail"
        )
