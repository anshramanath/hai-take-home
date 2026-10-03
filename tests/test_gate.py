"""Section 15.4: gate and policy."""

from __future__ import annotations

from pathlib import Path

from harness.planning.models import ToolCall
from harness.policy.gate import Allowed, Blocked, gate
from harness.world.users import get_user


def test_missing_scope_blocks_dana_reallocating_a_lot(make_harness):
    h = make_harness("scenario_a")
    dana = get_user(h.conn, "u-101")
    steps = [ToolCall(tool="reallocate_lot", args={
        "prod_order_id": "4820", "part_id": "P-1180",
        "remove": [{"lot_id": "L-2093", "qty": 100}],
        "add": [{"lot_id": "L-2101", "qty": 70}],
    })]
    result = gate(h.conn, dana, steps)
    assert isinstance(result, Blocked)
    assert "erp:lot:allocate" in result.reason


def test_missing_scope_blocks_omar_creating_a_po(make_harness):
    h = make_harness("scenario_a")
    omar = get_user(h.conn, "u-202")
    steps = [ToolCall(tool="create_po", args={
        "po_id": "PO-TEST", "part_id": "P-4471", "supplier_id": "S-Z", "qty": 100, "unit_price": 46.50,
        "needed_by": "2026-09-04", "created_by": "u-202",
    })]
    result = gate(h.conn, omar, steps, workflow="workflow:reroute_po")
    assert isinstance(result, Blocked)
    assert "erp:po:create" in result.reason


def test_workflow_only_tool_blocked_in_free_form_plan(make_harness):
    h = make_harness("scenario_a")
    dana = get_user(h.conn, "u-101")
    steps = [ToolCall(tool="create_po", args={
        "po_id": "PO-TEST", "part_id": "P-4471", "supplier_id": "S-Z", "qty": 100, "unit_price": 46.50,
        "needed_by": "2026-09-04", "created_by": "u-101",
    })]
    result = gate(h.conn, dana, steps, workflow=None)
    assert isinstance(result, Blocked)
    assert "workflow-only" in result.reason


def test_free_form_plan_of_only_notify_user_is_blocked_and_retryable(make_harness):
    """A real model, on the quality-hold shortage fixture, repeatedly
    proposed only notify_user for an unresolved shortfall -- correct
    reasoning, wrong action, nothing in the plan actually addresses the
    attention item. Blocked, and retryable since a fresh proposal naming
    an actual resolving tool is a plausible fix.
    """
    h = make_harness("scenario_b_shortage")
    omar = get_user(h.conn, "u-202")
    steps = [ToolCall(tool="notify_user", args={
        "to_user": "u-301", "from_user": "u-202", "subject": "Lot on hold", "body": "FYI.",
    })]
    result = gate(h.conn, omar, steps, workflow=None)
    assert isinstance(result, Blocked)
    assert result.retryable is True
    assert "non-resolving" in result.reason


def test_free_form_plan_with_a_resolving_tool_alongside_notify_user_is_allowed(make_harness):
    h = make_harness("scenario_b_covers")
    omar = get_user(h.conn, "u-202")
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
    result = gate(h.conn, omar, steps, workflow=None)
    assert isinstance(result, Allowed)


def test_schedule_check_is_workflow_only(make_harness):
    """schedule_check's created_by_run must be the run's own id, never
    part of any context shown to a free-form plan; a real model, finding
    it in its free-form tool catalog, proposed it anyway with that field
    missing. Restricting it to the workflow removes the temptation
    entirely: it's no longer even in a free-form prompt's tool list.
    """

    h = make_harness("scenario_a")
    dana = get_user(h.conn, "u-101")
    steps = [ToolCall(tool="schedule_check", args={
        "run_at": "2026-09-08", "kind": "arrival_check", "payload": {}, "created_by_run": "run-1",
    })]
    result = gate(h.conn, dana, steps, workflow=None)
    assert isinstance(result, Blocked)
    assert "workflow-only" in result.reason


def test_cancel_task_is_workflow_only(make_harness):
    h = make_harness("scenario_a")
    dana = get_user(h.conn, "u-101")
    steps = [ToolCall(tool="cancel_task", args={"task_id": "T-X"})]
    result = gate(h.conn, dana, steps, workflow=None)
    assert isinstance(result, Blocked)
    assert "workflow-only" in result.reason


def test_workflow_only_tool_allowed_inside_its_own_workflow(make_harness):
    h = make_harness("scenario_a")
    dana = get_user(h.conn, "u-101")
    steps = [ToolCall(tool="create_po", args={
        "po_id": "PO-TEST", "part_id": "P-4471", "supplier_id": "S-Z", "qty": 100, "unit_price": 46.50,
        "needed_by": "2026-09-04", "created_by": "u-101",
    })]
    result = gate(h.conn, dana, steps, workflow="workflow:reroute_po")
    assert isinstance(result, Allowed)


def test_unknown_tool_blocks(make_harness):
    h = make_harness("scenario_a")
    dana = get_user(h.conn, "u-101")
    steps = [ToolCall(tool="teleport_part", args={})]
    result = gate(h.conn, dana, steps)
    assert isinstance(result, Blocked)
    assert "unknown tool" in result.reason


def test_invalid_args_block(make_harness):
    h = make_harness("scenario_a")
    dana = get_user(h.conn, "u-101")
    steps = [ToolCall(tool="create_po", args={"part_id": "P-4471"})]  # missing required fields
    result = gate(h.conn, dana, steps, workflow="workflow:reroute_po")
    assert isinstance(result, Blocked)
    assert "invalid args" in result.reason


def test_value_under_requesters_limit_approver_is_requester(make_harness):
    h = make_harness("scenario_a")
    dana = get_user(h.conn, "u-101")
    steps = [ToolCall(tool="create_po", args={
        "po_id": "PO-TEST", "part_id": "P-4471", "supplier_id": "S-Z", "qty": 100, "unit_price": 46.50,
        "needed_by": "2026-09-04", "created_by": "u-101",
    })]  # 4650.00, well under Dana's 25000 limit
    result = gate(h.conn, dana, steps, workflow="workflow:reroute_po")
    assert result == Allowed(approver_id="u-101", routed_reason=None)


def test_value_over_requesters_limit_routes_to_manager(make_harness):
    h = make_harness("scenario_a")
    dana = get_user(h.conn, "u-101")
    steps = [ToolCall(tool="create_po", args={
        "po_id": "PO-TEST", "part_id": "P-4471", "supplier_id": "S-Z", "qty": 700, "unit_price": 46.50,
        "needed_by": "2026-09-04", "created_by": "u-101",
    })]  # 32550.00, over Dana's 25000 but under Marcus's 100000
    result = gate(h.conn, dana, steps, workflow="workflow:reroute_po")
    assert isinstance(result, Allowed)
    assert result.approver_id == "u-100"
    assert result.routed_reason is not None


def test_value_over_everyones_limit_blocks(make_harness):
    h = make_harness("scenario_a")
    dana = get_user(h.conn, "u-101")
    steps = [ToolCall(tool="create_po", args={
        "po_id": "PO-TEST", "part_id": "P-4471", "supplier_id": "S-Z", "qty": 10000, "unit_price": 46.50,
        "needed_by": "2026-09-04", "created_by": "u-101",
    })]  # 465000.00, over even Marcus's 100000
    result = gate(h.conn, dana, steps, workflow="workflow:reroute_po")
    assert isinstance(result, Blocked)


def test_tools_without_value_are_unaffected_by_threshold_rule(make_harness):
    h = make_harness("scenario_b_covers")
    omar = get_user(h.conn, "u-202")
    steps = [ToolCall(tool="reallocate_lot", args={
        "prod_order_id": "4820", "part_id": "P-1180",
        "remove": [{"lot_id": "L-2093", "qty": 100}],
        "add": [{"lot_id": "L-2101", "qty": 70}, {"lot_id": "L-2115", "qty": 30}],
    })]
    result = gate(h.conn, omar, steps)
    # Omar has no approval_limits at all; a value-less plan must still
    # resolve to self-approval rather than failing the threshold walk.
    assert result == Allowed(approver_id="u-202", routed_reason=None)


def test_gate_makes_no_llm_calls():
    """Nothing in the gate imports an LLM client or the openai SDK: the
    decision is pure code over data already in the database.
    """

    import ast

    import harness.policy.gate as gate_module

    tree = ast.parse(Path(gate_module.__file__).read_text())
    imported_modules = [
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, (ast.Import, ast.ImportFrom))
        for alias in node.names
    ] + [node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom) and node.module]

    assert not any("openai" in m.lower() or "llm" in m.lower() for m in imported_modules)
