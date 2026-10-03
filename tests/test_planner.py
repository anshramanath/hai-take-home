"""Section 15.9: planner."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from harness.context.registry import gather_context
from harness.detection.base import AttentionItem
from harness.detection.registry import run_detectors
from harness.planning.llm import FakeLLMClient, LLMOutputInvalid
from harness.planning.models import NoAction, PlannerOutput
from harness.planning.planner import PlannerFailed, propose
from harness.planning.prompt import build_messages
from harness.world.users import get_user

PLANNING_ROOT = Path(__file__).resolve().parent.parent / "harness" / "planning"


def _scenario_a_item(conn):
    row = conn.execute("SELECT * FROM attention_items").fetchone()
    return AttentionItem(
        detector=row["detector"], dedupe_key=row["dedupe_key"], owner_id=row["owner_id"],
        summary=row["summary"], facts=json.loads(row["facts"]),
    )


def test_prompt_excludes_tools_the_user_lacks_scope_for_and_workflow_only_tools(make_harness):
    h = make_harness("scenario_a")
    run_detectors(h.conn, h.clock)
    item = _scenario_a_item(h.conn)
    omar = get_user(h.conn, "u-202")  # no erp:po:* scopes at all

    context = gather_context(h.conn, h.clock, omar, item)
    messages = build_messages(item, context, [], omar)
    payload = json.loads(messages[1]["content"])
    tool_names = {t["name"] for t in payload["available_tools"]}

    assert "create_po" not in tool_names  # workflow-only, never shown to free-form planner
    assert "cancel_po" not in tool_names
    assert "reduce_po" not in tool_names
    assert "flag_shortage" in tool_names  # Omar has purchasing:flag
    assert "reallocate_lot" in tool_names  # Omar has erp:lot:allocate


def test_prompt_workflow_catalog_comes_from_the_registry(make_harness):
    h = make_harness("scenario_a")
    run_detectors(h.conn, h.clock)
    item = _scenario_a_item(h.conn)
    dana = get_user(h.conn, "u-101")

    context = gather_context(h.conn, h.clock, dana, item)
    messages = build_messages(item, context, [], dana)
    payload = json.loads(messages[1]["content"])

    names = {w["name"] for w in payload["available_workflows"]}
    assert names == {"reroute_po"}
    assert "params_schema" in payload["available_workflows"][0]


def test_invalid_output_retries_once_then_fails_and_logs_planner_invalid(make_harness):
    h = make_harness("scenario_a")
    run_detectors(h.conn, h.clock)
    item = _scenario_a_item(h.conn)
    dana = get_user(h.conn, "u-101")
    context = gather_context(h.conn, h.clock, dana, item)

    llm = FakeLLMClient([LLMOutputInvalid("bad json"), LLMOutputInvalid("bad json again")])
    with pytest.raises(PlannerFailed):
        propose(h.conn, h.clock, llm, item, context, [], dana, run_id="run-1")

    assert len(llm.calls) == 2
    events = [r[0] for r in h.conn.execute("SELECT event FROM audit_log WHERE run_id = 'run-1'")]
    assert "planner.invalid" in events


def test_one_invalid_then_valid_output_succeeds_on_retry(make_harness):
    h = make_harness("scenario_a")
    run_detectors(h.conn, h.clock)
    item = _scenario_a_item(h.conn)
    dana = get_user(h.conn, "u-101")
    context = gather_context(h.conn, h.clock, dana, item)

    llm = FakeLLMClient([
        LLMOutputInvalid("bad json"),
        PlannerOutput(proposal=NoAction(kind="none", reasoning="on retry, nothing to do")),
    ])
    proposal = propose(h.conn, h.clock, llm, item, context, [], dana, run_id="run-1")

    assert isinstance(proposal, NoAction)
    assert len(llm.calls) == 2


def test_no_action_proposal_is_returned_without_error(make_harness):
    h = make_harness("scenario_a")
    run_detectors(h.conn, h.clock)
    item = _scenario_a_item(h.conn)
    dana = get_user(h.conn, "u-101")
    context = gather_context(h.conn, h.clock, dana, item)

    llm = FakeLLMClient([PlannerOutput(proposal=NoAction(kind="none", reasoning="nothing to do"))])
    proposal = propose(h.conn, h.clock, llm, item, context, [], dana, run_id="run-1")

    assert isinstance(proposal, NoAction)


def test_memory_hints_appear_in_the_prompt_labeled_as_hints(make_harness):
    h = make_harness("scenario_a")
    run_detectors(h.conn, h.clock)
    item = _scenario_a_item(h.conn)
    dana = get_user(h.conn, "u-101")
    context = gather_context(h.conn, h.clock, dana, item)

    memory_facts = [{"subject": "S-Y", "fact": "slipped PO-77812 from 9/4 to 9/8", "source_ids": ["M-001"]}]
    messages = build_messages(item, context, memory_facts, dana)
    payload = json.loads(messages[1]["content"])

    assert payload["memory_hints"] == memory_facts


def test_workflow_request_enum_rejects_a_name_outside_the_registry():
    """Not just prompt text: the schema itself constrains `workflow` to a
    Literal built from the registry, so a model proposing a name that
    isn't registered gets a validation error, triggers the retry, and
    fails cleanly rather than silently entering a bogus workflow.
    """

    from pydantic import ValidationError

    from harness.planning.planner import _planner_output_model

    output_model = _planner_output_model()
    with pytest.raises(ValidationError):
        output_model.model_validate({
            "proposal": {
                "kind": "workflow", "workflow": "not_a_real_workflow", "params": {},
                "reasoning": "x", "summary_for_user": "y",
            }
        })


def test_planner_output_model_falls_back_to_the_static_model_with_no_registered_workflows():
    """If nothing is registered, `workflow` cannot be narrowed to a Literal
    of valid names (Literal[()] isn't meaningful), so the static
    PlannerOutput is used as-is.
    """

    import harness.execution.engine as engine_module
    from harness.planning.planner import _planner_output_model

    saved_registry = dict(engine_module._REGISTRY)
    engine_module._REGISTRY.clear()
    try:
        assert _planner_output_model() is PlannerOutput
    finally:
        engine_module._REGISTRY.clear()
        engine_module._REGISTRY.update(saved_registry)


def test_planner_module_has_no_scenario_specific_strings():
    banned = ["P-4471", "PO-77812", "Supplier Z", "S-Z", "4812", "Dana", "Kestrel", "Meridian"]
    for path in PLANNING_ROOT.glob("*.py"):
        text = path.read_text()
        for term in banned:
            assert term not in text, f"{term!r} found in {path}"
