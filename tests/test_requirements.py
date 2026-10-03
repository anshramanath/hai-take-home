"""Section 15.14: one test per assignment requirement, named after it.
Each test asserts its requirement end to end on its own; where another
test file already exercises the same mechanism in more detail, this file
still calls the real code directly (not the other test) so it stands as
its own proof, readable without cross-referencing anything else.
"""

from __future__ import annotations

import json
import re
from dataclasses import replace
from pathlib import Path

import pytest
from pydantic import ValidationError

from harness.app import approve, tick
from harness.context.base import ContextSlice
from harness.context.registry import gather_context
from harness.detection.base import AttentionItem
from harness.detection.registry import run_detectors
from harness.execution.args import CreatePoArgs, NotificationArgs
from harness.execution.catalog import get_tool
from harness.execution.engine import SimulatedCrash, resume_after_approval, resume_all
from harness.execution.executor import ScopeDenied, execute
from harness.execution.tools import ToolContext
from harness.execution.workflows.reroute_po import (
    REROUTE_PO_V1,
    ChooseSupplierResponse,
    DraftNotificationResponse,
)
from harness.planning.llm import FakeLLMClient
from harness.planning.models import NoAction, PlannerOutput, ToolCall, WorkflowRequest
from harness.planning.planner import propose
from harness.policy.gate import Blocked, gate
from harness.world.users import get_user

HARNESS_ROOT = Path(__file__).resolve().parent.parent / "harness"


def _scenario_a_item(conn) -> AttentionItem:
    row = conn.execute("SELECT * FROM attention_items").fetchone()
    return AttentionItem(
        detector=row["detector"], dedupe_key=row["dedupe_key"], owner_id=row["owner_id"],
        summary=row["summary"], facts=json.loads(row["facts"]),
    )


def _reroute_llm() -> FakeLLMClient:
    return FakeLLMClient([
        PlannerOutput(proposal=WorkflowRequest(
            kind="workflow", workflow="reroute_po",
            params={
                "part_id": "P-4471", "original_po_id": "PO-77812", "prod_order_id": "4812",
                "qty": 120, "needed_by": "2026-09-07",
            },
            reasoning="Supplier Y (PO-77812) slipped to 9/8 per M-001; 4812 starts 9/7.",
            summary_for_user="Reroute to Supplier Z and notify production.",
        )),
        ChooseSupplierResponse(supplier_id="S-Z", justification="Only approved candidate meeting the need date."),
        DraftNotificationResponse(body="Heads up: part of your shipment is being rerouted."),
    ])


# ---------------------------------------------------------------------------
# A1-A7: Scenario A's seven required behaviors (assignment section 2)


def test_a1_detect_without_being_prompted(make_harness):
    """A detector raises an item from ERP data alone: no user asked for
    it, and nothing in the detection path calls an LLM."""
    h = make_harness("scenario_a")
    created = run_detectors(h.conn, h.clock)

    assert created
    row = h.conn.execute(
        "SELECT dedupe_key FROM attention_items WHERE item_id = ?", (created[0],)
    ).fetchone()
    assert row["dedupe_key"] == "stockout:P-4471:4812:PO-77812"


def test_a2_gather_context_from_distinct_scoped_providers(make_harness):
    h = make_harness("scenario_a")
    run_detectors(h.conn, h.clock)
    item = _scenario_a_item(h.conn)
    dana = get_user(h.conn, "u-101")
    omar = get_user(h.conn, "u-202")  # no mail:read, no erp:po:read

    dana_context = gather_context(h.conn, h.clock, dana, item)
    omar_context = gather_context(h.conn, h.clock, omar, item)

    assert set(dana_context) == {"erp", "mail", "calendar", "quality"}  # four distinct sources
    assert dana_context["mail"].record_ids == ["M-001"]
    assert omar_context["mail"].record_ids == []  # scoped per user, not shared


def test_a3_reason_to_a_recommendation_and_a_proposed_plan(make_harness):
    h = make_harness("scenario_a")
    run_detectors(h.conn, h.clock)
    item = _scenario_a_item(h.conn)
    dana = get_user(h.conn, "u-101")
    context = gather_context(h.conn, h.clock, dana, item)

    proposal = propose(h.conn, h.clock, _reroute_llm(), item, context, [], dana, run_id="run-1")

    assert proposal.kind == "workflow"
    assert proposal.reasoning  # a recommendation with a stated reason, not a bare action


def test_a4_gate_blocks_before_any_write(make_harness):
    h = make_harness("scenario_a")
    dana = get_user(h.conn, "u-101")
    steps = [ToolCall(tool="create_po", args={
        "po_id": "PO-X", "part_id": "P-4471", "supplier_id": "S-Z", "qty": 10, "unit_price": 1.0,
        "needed_by": "2026-09-04", "created_by": "u-101",
    })]  # workflow-only tool proposed free-form: must be blocked before anything runs

    result = gate(h.conn, dana, steps, workflow=None)

    assert isinstance(result, Blocked)
    assert h.conn.execute("SELECT COUNT(*) FROM executed_actions").fetchone()[0] == 0
    assert h.conn.execute("SELECT COUNT(*) FROM erp_purchase_orders WHERE po_id = 'PO-X'").fetchone()[0] == 0


def test_a5_execution_is_idempotent_with_each_step_logged(make_harness):
    h = make_harness("scenario_a")
    tool = get_tool("notify_user")
    args = NotificationArgs(to_user="u-301", from_user="u-101", subject="x", body="y")
    ctx = ToolContext(run_id="run-1", step="s", today=h.clock.today())

    execute(h.conn, h.clock, tool, args, ctx, run_id="run-1", actor="test", requester_id="u-101")
    execute(h.conn, h.clock, tool, args, ctx, run_id="run-1", actor="test", requester_id="u-101")

    assert h.conn.execute("SELECT COUNT(*) FROM notifications").fetchone()[0] == 1
    events = [r[0] for r in h.conn.execute("SELECT event FROM audit_log WHERE run_id = 'run-1'")]
    assert events.count("action.executed") == 1
    assert events.count("action.skipped_idempotent") == 1


def test_a6_follow_up_schedules_a_check_and_re_enters_if_missing(make_harness):
    h = make_harness("scenario_a_no_arrival")
    llm = _reroute_llm()

    tick(h.conn, h.clock, llm)
    approval = h.conn.execute("SELECT approval_id FROM approvals").fetchone()
    approve(h.conn, h.clock, llm, approval_id=approval["approval_id"], decided_by="u-101")

    task = h.conn.execute("SELECT run_at FROM scheduled_tasks").fetchone()
    assert task is not None  # a follow-up was scheduled
    while h.clock.today().isoformat() < task["run_at"]:
        tick(h.conn, h.clock, llm)
    # No receipt recorded: the check should fire and re-enter the loop.
    new_item_llm = FakeLLMClient([PlannerOutput(proposal=NoAction(kind="none", reasoning="re-entered"))])
    tick(h.conn, h.clock, new_item_llm)

    new_po = h.conn.execute("SELECT po_id FROM erp_purchase_orders WHERE supplier_id = 'S-Z'").fetchone()["po_id"]
    assert h.conn.execute(
        "SELECT COUNT(*) FROM attention_items WHERE dedupe_key = ?", (f"stockout:P-4471:4812:{new_po}",)
    ).fetchone()[0] == 1


def test_a7_explain_reconstructs_the_story_from_audit_alone(make_harness):
    from harness.audit.explain import explain

    h = make_harness("scenario_a")
    llm = _reroute_llm()
    tick(h.conn, h.clock, llm)
    approval = h.conn.execute("SELECT approval_id FROM approvals").fetchone()
    approve(h.conn, h.clock, llm, approval_id=approval["approval_id"], decided_by="u-101")

    text = "\n".join(explain(h.conn))

    assert "detection" in text
    assert "S-Z" in text
    assert "u-101" in text


# ---------------------------------------------------------------------------
# Part 1: each of the eight responsibilities is separately replaceable


def test_part1_the_llm_client_is_swappable_with_no_other_code_change(make_harness):
    h = make_harness("scenario_a")
    run_detectors(h.conn, h.clock)
    item = _scenario_a_item(h.conn)
    dana = get_user(h.conn, "u-101")
    context = gather_context(h.conn, h.clock, dana, item)

    llm_a = FakeLLMClient([PlannerOutput(proposal=NoAction(kind="none", reasoning="first client"))])
    llm_b = FakeLLMClient([PlannerOutput(proposal=NoAction(kind="none", reasoning="second client"))])

    proposal_a = propose(h.conn, h.clock, llm_a, item, context, [], dana, run_id="run-a")
    proposal_b = propose(h.conn, h.clock, llm_b, item, context, [], dana, run_id="run-b")

    assert proposal_a.reasoning == "first client"
    assert proposal_b.reasoning == "second client"


def test_part1_a_dummy_detector_is_pluggable(make_harness):
    from harness.detection import registry as detection_registry

    class DummyDetector:
        name = "dummy"

        def detect(self, ctx):
            return [AttentionItem(
                detector="dummy", dedupe_key="dummy:1", owner_id="u-101", summary="dummy item", facts={},
            )]

    h = make_harness("scenario_a")
    saved = list(detection_registry.DETECTORS)
    detection_registry.DETECTORS.append(DummyDetector())
    try:
        run_detectors(h.conn, h.clock)
    finally:
        detection_registry.DETECTORS[:] = saved

    assert h.conn.execute(
        "SELECT COUNT(*) FROM attention_items WHERE dedupe_key = 'dummy:1'"
    ).fetchone()[0] == 1


def test_part1_a_dummy_provider_is_pluggable(make_harness):
    from harness.context import registry as context_registry

    class DummyProvider:
        source = "dummy"

        def fetch(self, ctx, user, item):
            return ContextSlice(source="dummy", records=[{"hello": "world"}], record_ids=["X"])

    h = make_harness("scenario_a")
    run_detectors(h.conn, h.clock)
    item = _scenario_a_item(h.conn)
    dana = get_user(h.conn, "u-101")

    saved = list(context_registry.PROVIDERS)
    context_registry.PROVIDERS.append(DummyProvider())
    try:
        context = gather_context(h.conn, h.clock, dana, item)
    finally:
        context_registry.PROVIDERS[:] = saved

    assert context["dummy"].records == [{"hello": "world"}]


# ---------------------------------------------------------------------------
# Part 2: the declared workflow


def test_part2_step_order_is_fixed_the_model_cannot_add_or_reorder_steps():
    with pytest.raises(ValidationError):
        WorkflowRequest(
            kind="workflow", workflow="reroute_po", params={}, reasoning="x", summary_for_user="y",
            steps=[{"tool": "create_po", "args": {}}],  # WorkflowRequest has no steps field at all
        )


def test_part2_bounded_llm_step_rejects_a_choice_outside_the_candidates(make_harness):
    from harness.world.users import get_user as _get_user

    h = make_harness("scenario_a")
    dana = _get_user(h.conn, "u-101")
    llm = FakeLLMClient([
        ChooseSupplierResponse(supplier_id="S-Q", justification="not a real candidate"),
        ChooseSupplierResponse(supplier_id="S-W", justification="also not a real candidate"),
    ])
    from harness.execution.engine import enter_workflow

    row = enter_workflow(
        h.conn, h.clock, llm, REROUTE_PO_V1,
        {"part_id": "P-4471", "original_po_id": "PO-77812", "prod_order_id": "4812", "qty": 120, "needed_by": "2026-09-07"},
        run_id="run-1", requester=dana,
    )

    assert row["status"] == "failed"  # two invalid choices, no fallback to an unvetted one
    assert h.conn.execute("SELECT COUNT(*) FROM executed_actions").fetchone()[0] == 0


def test_part2_every_action_step_declares_a_compensation():
    for step in REROUTE_PO_V1.steps:
        if step.kind == "action":
            assert step.compensate is not None, f"{step.name} has no compensation"


def test_part2_resume_after_kill_completes_with_no_duplicate_writes(make_harness):
    h = make_harness("scenario_a")
    dana = get_user(h.conn, "u-101")
    # enter_workflow() takes already-decided params directly; it never
    # calls propose(), so only the two bounded-step responses are needed.
    llm = FakeLLMClient([
        ChooseSupplierResponse(supplier_id="S-Z", justification="Only approved candidate meeting the need date."),
        DraftNotificationResponse(body="Heads up: part of your shipment is being rerouted."),
    ])
    from harness.execution.engine import enter_workflow

    row = enter_workflow(
        h.conn, h.clock, llm, REROUTE_PO_V1,
        {"part_id": "P-4471", "original_po_id": "PO-77812", "prod_order_id": "4812", "qty": 120, "needed_by": "2026-09-07"},
        run_id="run-1", requester=dana,
    )
    state = json.loads(row["state"])
    from harness.policy.approvals import decide

    decide(h.conn, h.clock, approval_id=state["_approval_id"], decided_by="u-101", decision="approved")

    with pytest.raises(SimulatedCrash):
        resume_after_approval(h.conn, h.clock, llm, row["instance_id"], crash_after="create_po")

    resume_all(h.conn, h.clock, llm)

    assert h.conn.execute(
        "SELECT status FROM workflow_instances WHERE instance_id = ?", (row["instance_id"],)
    ).fetchone()[0] == "completed"
    assert h.conn.execute(
        "SELECT COUNT(*) FROM erp_purchase_orders WHERE supplier_id = 'S-Z'"
    ).fetchone()[0] == 1


def test_part2_definitions_are_versioned_instances_keep_their_own_version():
    from dataclasses import replace as _replace

    from harness.execution.engine import _REGISTRY, get_definition, register

    v2 = _replace(REROUTE_PO_V1, version=2)
    register(v2)
    try:
        assert get_definition("reroute_po", 1) is REROUTE_PO_V1
        assert get_definition("reroute_po", 2) is v2
    finally:
        _REGISTRY.pop(("reroute_po", 2), None)


# ---------------------------------------------------------------------------
# Part 3: Scenario B's new components work without core changes


def test_part3_planner_gate_and_audit_have_no_references_to_lots_or_quality():
    core_files = [
        HARNESS_ROOT / "planning" / "planner.py",
        HARNESS_ROOT / "planning" / "prompt.py",
        HARNESS_ROOT / "planning" / "models.py",
        HARNESS_ROOT / "planning" / "llm.py",
        HARNESS_ROOT / "policy" / "gate.py",
        HARNESS_ROOT / "policy" / "approvals.py",
        HARNESS_ROOT / "audit" / "log.py",
        HARNESS_ROOT / "audit" / "explain.py",
    ]
    pattern = re.compile(r"\blot\b|\bquality\b", re.IGNORECASE)
    offenders = [str(path) for path in core_files if pattern.search(path.read_text())]
    assert not offenders, f"core modules reference lots/quality: {offenders}"


def test_part3_quality_manager_has_different_scopes_than_purchasing(make_harness):
    h = make_harness("scenario_a")
    omar = get_user(h.conn, "u-202")
    dana = get_user(h.conn, "u-101")

    assert "erp:lot:allocate" in omar.scopes
    assert "erp:lot:allocate" not in dana.scopes
    assert "erp:po:create" in dana.scopes
    assert "erp:po:create" not in omar.scopes


def test_part3_scenario_b_runs_through_the_same_generic_planner_and_gate(make_harness):
    """Not a new planner or gate for Scenario B: the exact same `propose`
    and `gate` functions Scenario A uses, over different data."""

    from harness.planning.models import ToolPlan

    h = make_harness("scenario_b_covers")
    run_detectors(h.conn, h.clock)
    row = h.conn.execute("SELECT * FROM attention_items").fetchone()
    item = AttentionItem(
        detector=row["detector"], dedupe_key=row["dedupe_key"], owner_id=row["owner_id"],
        summary=row["summary"], facts=json.loads(row["facts"]),
    )
    omar = get_user(h.conn, "u-202")
    context = gather_context(h.conn, h.clock, omar, item)

    llm = FakeLLMClient([PlannerOutput(proposal=ToolPlan(
        kind="plan",
        steps=[{"tool": "reallocate_lot", "args": {
            "prod_order_id": "4820", "part_id": "P-1180",
            "remove": [{"lot_id": "L-2093", "qty": 100}],
            "add": [{"lot_id": "L-2101", "qty": 70}, {"lot_id": "L-2115", "qty": 30}],
        }}],
        reasoning="Reallocate away from the held lot.", summary_for_user="Reallocate.",
    ))])
    proposal = propose(h.conn, h.clock, llm, item, context, [], omar, run_id="run-1")
    result = gate(h.conn, omar, proposal.steps, workflow=None)

    from harness.policy.gate import Allowed

    assert isinstance(result, Allowed)


# ---------------------------------------------------------------------------
# Permission model


def test_permission_model_providers_never_return_unreadable_data(make_harness):
    h = make_harness("scenario_a")
    run_detectors(h.conn, h.clock)
    item = _scenario_a_item(h.conn)
    dana = get_user(h.conn, "u-101")
    scopeless = replace(dana, scopes=frozenset())

    context = gather_context(h.conn, h.clock, scopeless, item)

    for source, slice_ in context.items():
        assert slice_.records == [], f"{source} returned data to a user with no scopes"
        assert slice_.record_ids == []


def test_permission_model_tools_never_run_without_scope(make_harness):
    h = make_harness("scenario_a")
    tool = get_tool("create_po")
    args = CreatePoArgs(
        po_id="PO-X", part_id="P-4471", supplier_id="S-Z", qty=10, unit_price=1.0,
        needed_by="2026-09-04", created_by="u-202",
    )
    ctx = ToolContext(run_id="run-1", step="s", today=h.clock.today())

    with pytest.raises(ScopeDenied):  # Omar has no erp:po:create
        execute(h.conn, h.clock, tool, args, ctx, run_id="run-1", actor="test", requester_id="u-202")

    assert h.conn.execute("SELECT COUNT(*) FROM erp_purchase_orders WHERE po_id = 'PO-X'").fetchone()[0] == 0
