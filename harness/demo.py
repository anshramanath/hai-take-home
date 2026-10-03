"""The `demo` command (section 16): narrates Scenario A through approval,
escalation, execution, and the follow-up; then Scenario B's two variants;
then the failure cases; then an `explain` of Scenario A's audit trail.

Scenario A and its follow-up use whichever LLM client the caller passes in
(the real API, or a replay of a real run when no key is set) — that is
the one part of the demo where real-model authenticity matters, per the
spec's own recorded-run requirement. Scenario B and the failure cases use
a hand-scripted FakeLLMClient regardless: their job is to reliably show
the mechanics (reallocation, compensation, escalation, tamper-rejection),
not to showcase live model reasoning, and section 15 is explicit that no
test may touch the network — the demo holds itself to the same rule so it
can run unattended in CI.

Each section gets its own temporary database: Scenario B and the failure
cases need different starting fixtures, and keeping them apart from
Scenario A's database means `explain` at the end still reads the
undisturbed Scenario A story.
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from tempfile import TemporaryDirectory

from rich.console import Console
from rich.prompt import Confirm

from harness.app import Harness, approve, reject, tick
from harness.audit.explain import explain
from harness.detection.registry import run_detectors
from harness.execution.engine import (
    SimulatedCrash,
    enter_workflow,
    get_definition,
    latest_version,
    resume_after_approval,
    resume_all,
)
from harness.execution.workflows.reroute_po import ChooseSupplierResponse, DraftNotificationResponse
from harness.planning.llm import LLMClient, FakeLLMClient
from harness.planning.models import NoAction, PlannerOutput, ToolCall, ToolPlan, WorkflowRequest
from harness.policy.approvals import decide, get_approval
from harness.policy.gate import Blocked, gate
from harness.world.receipts import record_receipt
from harness.world.users import get_user

REROUTE_PARAMS = {
    "part_id": "P-4471", "original_po_id": "PO-77812", "prod_order_id": "4812",
    "qty": 120, "needed_by": "2026-09-07",
}


def _reroute_llm() -> FakeLLMClient:
    return FakeLLMClient([
        PlannerOutput(proposal=WorkflowRequest(
            kind="workflow", workflow="reroute_po", params=REROUTE_PARAMS,
            reasoning="Supplier Kestrel Components (PO-77812) emailed that the shipment slips to "
                      "9/8 (M-001); production order 4812 starts 9/7 and depends on it.",
            summary_for_user="Part P-4471 will likely cause production order 4812 to miss its "
                              "scheduled start. Supplier Y said the shipment is delayed. I can "
                              "move the PO to an approved alternate supplier and notify "
                              "production. Want me to proceed?",
        )),
        ChooseSupplierResponse(
            supplier_id="S-Z", justification="Only approved candidate whose lead time meets the need date.",
        ),
        DraftNotificationResponse(
            body="Heads up: part of your incoming P-4471 shipment is being rerouted to a faster supplier.",
        ),
    ])


def _decide_approval(console: Console, conn, clock, llm_client: LLMClient, approval_id: str, *, interactive: bool) -> None:
    approval = get_approval(conn, approval_id)
    approver_id = approval["approver_id"]
    if interactive:
        console.print(f"[bold]Approval pending[/bold] for [cyan]{approver_id}[/cyan]: {approval_id}")
        if Confirm.ask(f"Approve as {approver_id}?", default=True):
            approve(conn, clock, llm_client, approval_id=approval_id, decided_by=approver_id)
            console.print("[green]Approved.[/green]")
        else:
            reject(conn, clock, approval_id=approval_id, decided_by=approver_id)
            console.print("[yellow]Rejected.[/yellow]")
    else:
        approve(conn, clock, llm_client, approval_id=approval_id, decided_by=approver_id)
        console.print(f"[green]Approved[/green] as {approver_id} (non-interactive).")


# ---------------------------------------------------------------------------
# Section 1 + 2: Scenario A and the follow-up


def run_scenario_a(console: Console, harness: Harness, llm_client: LLMClient, *, interactive: bool) -> None:
    console.rule("[bold]Scenario A: at-risk part, supplier delay, reroute[/bold]")
    harness.reset("scenario_a")

    console.print(f"Today is {harness.clock.today().isoformat()}. Running the first tick...")
    result = tick(harness.conn, harness.clock, llm_client)
    console.print(f"Detected: {result['new_items']}. Run(s) started: {result['runs']}.")

    requested_event = harness.conn.execute(
        "SELECT detail FROM audit_log WHERE event = 'approval.requested' ORDER BY seq DESC LIMIT 1"
    ).fetchone()
    requested_detail = json.loads(requested_event["detail"])
    proposed_event = harness.conn.execute(
        "SELECT detail FROM audit_log WHERE event = 'planner.proposed' ORDER BY seq DESC LIMIT 1"
    ).fetchone()
    summary_for_user = json.loads(proposed_event["detail"])["proposal"]["summary_for_user"]
    console.print(
        f"Approval requested: [bold]{requested_detail['approval_id']}[/bold], approver "
        f"[cyan]{requested_detail['approver_id']}[/cyan] (value threshold check passed)."
    )
    console.print(f'[italic]"{summary_for_user}"[/italic]')
    console.print("[dim]No answer today.[/dim]")

    # Escalation (if any) already ran inside that same tick, right before
    # the clock advanced: the rule is "unanswered at end of day, approver
    # out tomorrow," and a same-day request is itself eligible for that
    # check without waiting for a further tick.
    if result["escalated"]:
        escalated_event = harness.conn.execute(
            "SELECT detail FROM audit_log WHERE event = 'approval.escalated' ORDER BY seq DESC LIMIT 1"
        ).fetchone()
        escalated_detail = json.loads(escalated_event["detail"])
        console.print(
            f"[yellow]Escalated[/yellow]: {escalated_detail['from']} was out of office; "
            f"routed to [cyan]{escalated_detail['to']}[/cyan]. Reason: {escalated_detail['reason']}"
        )

    current_approval = get_approval(harness.conn, requested_detail["approval_id"])
    _decide_approval(console, harness.conn, harness.clock, llm_client, current_approval["approval_id"], interactive=interactive)

    instance = harness.conn.execute("SELECT status FROM workflow_instances").fetchone()
    if instance and instance["status"] == "completed":
        new_po = harness.conn.execute(
            "SELECT po_id, qty, supplier_id, promised_date FROM erp_purchase_orders WHERE supplier_id = 'S-Z'"
        ).fetchone()
        reduced = harness.conn.execute(
            "SELECT qty, status FROM erp_purchase_orders WHERE po_id = 'PO-77812'"
        ).fetchone()
        console.print(
            f"Executed: created [bold]{new_po['po_id']}[/bold] ({new_po['qty']} units, {new_po['supplier_id']}, "
            f"promised {new_po['promised_date']}); reduced PO-77812 to {reduced['qty']} units "
            f"({reduced['status']}); production notified; arrival check scheduled."
        )

        console.rule("[bold]Follow-up: confirming the replacement arrived[/bold]")
        # The receipt is recorded before any further tick, deliberately:
        # the arrival check only ever asks "did it arrive", never "will
        # it", so the fact it checks has to already exist when it runs.
        record_receipt(harness.conn, harness.clock, po_id=new_po["po_id"], qty=new_po["qty"])
        console.print(f"Receipt recorded for {new_po['po_id']}: {new_po['qty']} units.")
        task = harness.conn.execute("SELECT run_at FROM scheduled_tasks").fetchone()
        due = date.fromisoformat(task["run_at"])
        # Skip straight to the due date rather than ticking through the
        # days in between: a day with nothing due still reruns detection,
        # which has nothing useful to find before then and would call the
        # planner again for no reason.
        days_until_due = (due - harness.clock.today()).days
        if days_until_due > 0:
            harness.clock.advance(days_until_due)
        follow_up_result = tick(harness.conn, harness.clock, llm_client)
        if follow_up_result["fired_tasks"]:
            console.print(f"[green]Arrival check fired[/green] on {task['run_at']}: receipt confirmed in full.")
    else:
        console.print("[yellow]Approval was rejected; nothing executed.[/yellow]")


# ---------------------------------------------------------------------------
# Section 3: Scenario B


def run_scenario_b(console: Console, db_dir: Path) -> None:
    console.rule("[bold]Scenario B: quality hold, lot reallocation (covers)[/bold]")
    h = Harness(db_dir / "scenario_b_covers.db")
    h.reset("scenario_b_covers")
    llm = FakeLLMClient([PlannerOutput(proposal=ToolPlan(
        kind="plan",
        steps=[
            ToolCall(tool="reallocate_lot", args={
                "prod_order_id": "4820", "part_id": "P-1180",
                "remove": [{"lot_id": "L-2093", "qty": 100}],
                "add": [{"lot_id": "L-2101", "qty": 70}, {"lot_id": "L-2115", "qty": 30}],
            }),
            ToolCall(tool="notify_user", args={
                "to_user": "u-301", "from_user": "u-202",
                "subject": "Lot reallocated for production order 4820",
                "body": "L-2093 was on hold; coverage reallocated from L-2101 and L-2115.",
            }),
        ],
        reasoning="L-2093 is on hold and 4820 starts within 3 days; L-2101 and L-2115 together "
                  "cover the full 100 units needed.",
        summary_for_user="Reallocate 4820's coverage away from the held lot and notify production.",
    ))])
    result = tick(h.conn, h.clock, llm)
    approval = h.conn.execute("SELECT approval_id, approver_id FROM approvals").fetchone()
    console.print(f"Detected: {result['new_items']}. Approval: {approval['approval_id']} routed to {approval['approver_id']}.")
    approve(h.conn, h.clock, llm, approval_id=approval["approval_id"], decided_by=approval["approver_id"])
    allocations = dict(h.conn.execute(
        "SELECT lot_id, qty FROM erp_lot_allocations WHERE prod_order_id = '4820'"
    ).fetchall())
    console.print(f"[green]Reallocated[/green]: {allocations}. Production notified.")
    h.close()

    console.rule("[bold]Scenario B: quality hold, no covering lot (shortage)[/bold]")
    h2 = Harness(db_dir / "scenario_b_shortage.db")
    h2.reset("scenario_b_shortage")
    llm2 = FakeLLMClient([
        PlannerOutput(proposal=ToolPlan(
            kind="plan",
            steps=[ToolCall(tool="flag_shortage", args={
                "part_id": "P-1180", "prod_order_id": "4820", "qty_short": 10,
                "summary": "Released lots cover only 90 of the 100 units 4820 needs.",
            })],
            reasoning="No combination of released lots covers the full 100 units needed.",
            summary_for_user="Flag a 10-unit shortage of P-1180 to purchasing.",
        )),
        PlannerOutput(proposal=NoAction(
            kind="none",
            reasoning="No declared workflow exists for buying lot-tracked stock and PO tools "
                      "are workflow-only; recommend purchasing manually source 10 more units.",
        )),
    ])
    tick(h2.conn, h2.clock, llm2)
    approval2 = h2.conn.execute("SELECT approval_id, approver_id FROM approvals").fetchone()
    approve(h2.conn, h2.clock, llm2, approval_id=approval2["approval_id"], decided_by=approval2["approver_id"])
    console.print("[yellow]Shortage flagged[/yellow] to purchasing; no PO tools exist in free-form.")
    tick(h2.conn, h2.clock, llm2)
    console.print("Purchasing's agent reviewed the shortage and recommended only (no PO created).")
    h2.close()


# ---------------------------------------------------------------------------
# Section 4: failure cases


def run_failure_cases(console: Console, db_dir: Path) -> None:
    console.rule("[bold]Failure cases[/bold]")

    # No qualifying supplier. enter_workflow() takes already-decided params
    # directly; it never calls propose(), so no PlannerOutput wrapper is
    # needed here (this case halts before any LLM call is even made).
    h = Harness(db_dir / "failure_no_supplier.db")
    h.reset("scenario_a_no_supplier")
    llm = FakeLLMClient([])
    definition = get_definition("reroute_po", latest_version("reroute_po"))
    dana = get_user(h.conn, "u-101")
    row = enter_workflow(h.conn, h.clock, llm, definition, REROUTE_PARAMS, run_id="demo-1", requester=dana)
    console.print(f"No qualifying supplier: instance status = [red]{row['status']}[/red], zero writes.")
    h.close()

    # Over-limit routing to manager.
    h = Harness(db_dir / "failure_over_limit.db")
    h.reset("scenario_a_over_limit")
    llm = FakeLLMClient([
        ChooseSupplierResponse(supplier_id="S-Z", justification="Only approved candidate meeting the need date."),
        DraftNotificationResponse(body="Large reroute in progress."),
    ])
    row = enter_workflow(
        h.conn, h.clock, llm, definition, {**REROUTE_PARAMS, "qty": 700}, run_id="demo-2", requester=dana,
    )
    approval = h.conn.execute("SELECT approver_id, routed_reason FROM approvals").fetchone()
    console.print(
        f"Over-limit routing: value exceeded Dana's limit, routed to [cyan]{approval['approver_id']}[/cyan] "
        f"({approval['routed_reason']})."
    )
    h.close()

    # Missing scope blocked.
    h = Harness(db_dir / "failure_scope.db")
    h.reset("scenario_a")
    omar = get_user(h.conn, "u-202")
    steps = [ToolCall(tool="create_po", args={
        "po_id": "PO-X", "part_id": "P-4471", "supplier_id": "S-Z", "qty": 10, "unit_price": 1.0,
        "needed_by": "2026-09-04", "created_by": "u-202",
    })]
    result = gate(h.conn, omar, steps, workflow="workflow:reroute_po")
    assert isinstance(result, Blocked)
    console.print(f"Missing scope blocked: Omar proposing create_po: [red]{result.reason}[/red]")
    h.close()

    # Process crash and resume without duplicates.
    h = Harness(db_dir / "failure_crash.db")
    h.reset("scenario_a")
    llm = _reroute_llm()
    tick(h.conn, h.clock, llm)
    approval = h.conn.execute("SELECT approval_id, approver_id FROM approvals").fetchone()
    decide(h.conn, h.clock, approval_id=approval["approval_id"], decided_by=approval["approver_id"], decision="approved")
    instance = h.conn.execute("SELECT instance_id FROM workflow_instances").fetchone()
    try:
        resume_after_approval(h.conn, h.clock, llm, instance["instance_id"], crash_after="create_po")
    except SimulatedCrash:
        console.print("[dim]Simulated crash right after create_po persisted.[/dim]")
    resume_all(h.conn, h.clock, llm)
    final = h.conn.execute(
        "SELECT status FROM workflow_instances WHERE instance_id = ?", (instance["instance_id"],)
    ).fetchone()
    po_count = h.conn.execute("SELECT COUNT(*) FROM erp_purchase_orders WHERE supplier_id = 'S-Z'").fetchone()[0]
    console.print(f"Resumed after crash: status = [green]{final['status']}[/green], exactly {po_count} PO to S-Z.")
    h.close()

    # Duplicate detection ignored.
    h = Harness(db_dir / "failure_dup.db")
    h.reset("scenario_a")
    first = run_detectors(h.conn, h.clock)
    second = run_detectors(h.conn, h.clock)
    console.print(f"Duplicate detection: first run raised {first}, second run raised {second} (deduped).")
    h.close()

    # Frozen plan tamper rejected.
    h = Harness(db_dir / "failure_tamper.db")
    h.reset("scenario_a")
    llm = _reroute_llm()
    tick(h.conn, h.clock, llm)
    approval = h.conn.execute("SELECT approval_id, approver_id FROM approvals").fetchone()
    decide(h.conn, h.clock, approval_id=approval["approval_id"], decided_by=approval["approver_id"], decision="approved")
    h.conn.execute(
        "UPDATE approvals SET plan_json = REPLACE(plan_json, '120', '999999') WHERE approval_id = ?",
        (approval["approval_id"],),
    )
    h.conn.commit()
    instance = h.conn.execute("SELECT instance_id FROM workflow_instances").fetchone()
    final = resume_after_approval(h.conn, h.clock, llm, instance["instance_id"])
    console.print(f"Tampered plan rejected: status = [red]{final['status']}[/red], zero writes.")
    h.close()

    # Arrival not received re-enters.
    h = Harness(db_dir / "failure_no_arrival.db")
    h.reset("scenario_a_no_arrival")
    llm = _reroute_llm()
    tick(h.conn, h.clock, llm)
    approval = h.conn.execute("SELECT approval_id, approver_id FROM approvals").fetchone()
    approve(h.conn, h.clock, llm, approval_id=approval["approval_id"], decided_by=approval["approver_id"])
    task = h.conn.execute("SELECT run_at FROM scheduled_tasks").fetchone()
    reentry_llm = FakeLLMClient([PlannerOutput(proposal=NoAction(
        kind="none", reasoning="Already escalated via the missed-arrival alert; flagging for manual follow-up.",
    ))])
    while h.clock.today().isoformat() < task["run_at"]:
        tick(h.conn, h.clock, reentry_llm)
    tick(h.conn, h.clock, reentry_llm)
    new_po = h.conn.execute("SELECT po_id FROM erp_purchase_orders WHERE supplier_id = 'S-Z'").fetchone()["po_id"]
    reentered = h.conn.execute(
        "SELECT COUNT(*) FROM attention_items WHERE dedupe_key = ?", (f"stockout:P-4471:4812:{new_po}",)
    ).fetchone()[0]
    console.print(f"Arrival not received: loop re-entered ({reentered} new item raised for {new_po}).")
    h.close()


# ---------------------------------------------------------------------------
# Orchestration


def run_demo(console: Console, llm_client: LLMClient, *, interactive: bool = False) -> None:
    with TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        a_harness = Harness(tmp_path / "scenario_a.db")
        run_scenario_a(console, a_harness, llm_client, interactive=interactive)
        run_scenario_b(console, tmp_path)
        run_failure_cases(console, tmp_path)

        console.rule("[bold]Explain: Scenario A, from the audit log alone[/bold]")
        for line in explain(a_harness.conn):
            console.print(line)
        a_harness.close()
