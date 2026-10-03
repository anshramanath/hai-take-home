"""F5: a process killed between `decide()` recording an approval and the
execution that follows it must not strand that approval forever.
`resume_pending_executions` (called every tick) picks up any approval
already 'approved' whose run never reached a terminal status and runs it,
for both the declared-workflow and free-form paths.

Simulates the crash the same way test_scheduling.py's
test_task_survives_restart does: back up the live connection to a real
file, close it, and reopen a fresh connection, so nothing in memory
survives except what was actually committed.
"""

from __future__ import annotations

import json
import sqlite3

from harness.app import resume_pending_executions
from harness.execution.engine import enter_workflow
from harness.execution.workflows.reroute_po import (
    REROUTE_PO_V1,
    ChooseSupplierResponse,
    DraftNotificationResponse,
)
from harness.memory.runs import create_run, get_run, set_run_status
from harness.planning.llm import FakeLLMClient
from harness.planning.models import ToolCall
from harness.policy.approvals import create_approval, decide
from harness.scheduling.clock import Clock
from harness.world.users import get_user


def _restart(h, tmp_path, name: str) -> tuple[sqlite3.Connection, Clock]:
    db_path = tmp_path / name
    file_conn = sqlite3.connect(db_path)
    h.conn.backup(file_conn)
    file_conn.close()
    h.conn.close()

    new_conn = sqlite3.connect(db_path)
    new_conn.row_factory = sqlite3.Row
    return new_conn, Clock(new_conn)


def test_workflow_approval_decided_but_not_executed_resumes_once_after_a_crash(make_harness, tmp_path):
    h = make_harness("scenario_a")
    dana = get_user(h.conn, "u-101")
    run_id = create_run(h.conn, h.clock, item_id="AI-TEST", user_id="u-101")

    llm = FakeLLMClient([
        ChooseSupplierResponse(supplier_id="S-Z", justification="meets lead time and is approved"),
        DraftNotificationResponse(body="Heads up, your part shipment is being rerouted."),
    ])
    params = {
        "part_id": "P-4471", "original_po_id": "PO-77812", "prod_order_id": "4812",
        "qty": 120, "needed_by": "2026-09-07",
    }
    row = enter_workflow(h.conn, h.clock, llm, REROUTE_PO_V1, params, run_id=run_id, requester=dana)
    set_run_status(h.conn, run_id, row["status"])
    assert row["status"] == "awaiting_approval"

    approval_id = json.loads(row["state"])["_approval_id"]
    decide(h.conn, h.clock, approval_id=approval_id, decided_by="u-101", decision="approved")
    # Crash simulated here: the process dies before resume_after_approval
    # ever runs. Nothing has executed yet.
    assert h.conn.execute("SELECT COUNT(*) FROM executed_actions").fetchone()[0] == 0

    new_conn, new_clock = _restart(h, tmp_path, "workflow-crash.db")
    new_llm = FakeLLMClient([])  # steps 1-4 already ran before approval; no LLM calls expected now

    resumed = resume_pending_executions(new_conn, new_clock, new_llm)

    assert approval_id in resumed
    instance = new_conn.execute("SELECT status FROM workflow_instances WHERE run_id = ?", (run_id,)).fetchone()
    assert instance["status"] == "completed"
    assert new_conn.execute(
        "SELECT COUNT(*) FROM erp_purchase_orders WHERE supplier_id = 'S-Z'"
    ).fetchone()[0] == 1
    assert new_conn.execute(
        "SELECT qty FROM erp_purchase_orders WHERE po_id = 'PO-77812'"
    ).fetchone()[0] == 280
    assert get_run(new_conn, run_id)["status"] == "completed"

    # Calling it again (e.g. another tick) must not duplicate anything:
    # the run is now terminal, so the join in resume_pending_executions no
    # longer matches it.
    resumed_again = resume_pending_executions(new_conn, new_clock, new_llm)
    assert resumed_again == []
    assert new_conn.execute(
        "SELECT COUNT(*) FROM erp_purchase_orders WHERE supplier_id = 'S-Z'"
    ).fetchone()[0] == 1
    new_conn.close()


def test_free_form_approval_decided_but_not_executed_resumes_once_after_a_crash(make_harness, tmp_path):
    h = make_harness("scenario_b_covers")
    omar = get_user(h.conn, "u-202")
    run_id = create_run(h.conn, h.clock, item_id="AI-TEST", user_id="u-202")

    steps = [
        ToolCall(tool="reallocate_lot", args={
            "prod_order_id": "4820", "part_id": "P-1180",
            "remove": [{"lot_id": "L-2093", "qty": 100}],
            "add": [{"lot_id": "L-2101", "qty": 70}, {"lot_id": "L-2115", "qty": 30}],
        }),
        ToolCall(tool="notify_user", args={
            "to_user": "u-301", "from_user": "u-202", "subject": "Reallocated", "body": "Done.",
        }),
    ]
    approval_id = create_approval(
        h.conn, h.clock, run_id=run_id, requester=omar, steps=steps,
        approver_id="u-202", routed_reason=None, workflow=None,
    )
    set_run_status(h.conn, run_id, "awaiting_approval")
    decide(h.conn, h.clock, approval_id=approval_id, decided_by="u-202", decision="approved")
    # Crash simulated here: nothing has executed yet.
    assert h.conn.execute("SELECT COUNT(*) FROM executed_actions").fetchone()[0] == 0

    new_conn, new_clock = _restart(h, tmp_path, "free-form-crash.db")

    resumed = resume_pending_executions(new_conn, new_clock, FakeLLMClient([]))

    assert approval_id in resumed
    allocations = dict(new_conn.execute(
        "SELECT lot_id, qty FROM erp_lot_allocations WHERE prod_order_id = '4820'"
    ).fetchall())
    assert allocations == {"L-2101": 70, "L-2115": 30}
    assert new_conn.execute("SELECT COUNT(*) FROM notifications").fetchone()[0] == 1
    assert get_run(new_conn, run_id)["status"] == "completed"

    resumed_again = resume_pending_executions(new_conn, new_clock, FakeLLMClient([]))
    assert resumed_again == []
    assert new_conn.execute("SELECT COUNT(*) FROM notifications").fetchone()[0] == 1
    new_conn.close()
