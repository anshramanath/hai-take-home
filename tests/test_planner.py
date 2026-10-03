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
    assert "schedule_check" not in tool_names  # workflow-only: created_by_run isn't derivable
    assert "cancel_task" not in tool_names
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


def test_reroute_po_is_never_offered_for_an_item_it_does_not_apply_to(make_harness):
    """A real model, shown reroute_po regardless of what kind of item it
    was given, sometimes proposes it anyway for a problem it was never
    meant to solve, inventing or repurposing an id to force-fit it; prompt
    wording alone did not reliably stop this. `reroute_po` declares which
    detectors it applies to; an item from any other detector must not see
    it in the prompt at all, and must not be able to name it even at the
    schema level.
    """

    from pydantic import ValidationError

    from harness.planning.planner import _planner_output_model

    h = make_harness("scenario_b_covers")
    run_detectors(h.conn, h.clock)
    item = _scenario_a_item(h.conn)  # same helper; grabs the one raised item
    assert item.detector == "quality_hold"
    omar = get_user(h.conn, "u-202")

    context = gather_context(h.conn, h.clock, omar, item)
    messages = build_messages(item, context, [], omar)
    payload = json.loads(messages[1]["content"])
    assert payload["available_workflows"] == []

    # No applicable workflow at all: the schema excludes "workflow" as a
    # kind entirely, not just narrows it to an empty Literal.
    output_model = _planner_output_model(item.detector)
    with pytest.raises(ValidationError):
        output_model.model_validate({
            "proposal": {
                "kind": "workflow", "workflow": "reroute_po", "params": {},
                "reasoning": "x", "summary_for_user": "y",
            }
        })


def test_reroute_po_is_still_offered_on_re_entry_after_a_missed_arrival():
    """The re-entry item from a missed arrival carries detector
    'arrival_check', not 'stockout'; reroute_po must still apply to it, or
    a second reroute could never be proposed.
    """

    from harness.execution.engine import registered_workflow_names_for_detector

    assert "reroute_po" in registered_workflow_names_for_detector("arrival_check")
    assert "reroute_po" in registered_workflow_names_for_detector("stockout")
    assert "reroute_po" not in registered_workflow_names_for_detector("quality_hold")


def test_retry_instruction_names_the_specific_missing_or_wrong_fields():
    """The default would relay the raw, stack-trace-shaped Pydantic error
    as the retry message; a real model given exactly that still dropped
    the same field again on the retry. Naming the field directly, as an
    instruction rather than a relayed error, is the actual fix this
    targets.
    """

    from harness.planning.models import ToolPlan
    from harness.planning.planner import _retry_instruction

    try:
        ToolPlan.model_validate({"kind": "plan", "reasoning": "x"})  # steps missing entirely
    except Exception as exc:
        cause = exc
    wrapped = LLMOutputInvalid(str(cause))
    wrapped.__cause__ = cause

    message = _retry_instruction(wrapped)
    assert "steps" in message
    assert "left out required field" in message

    try:
        ToolPlan.model_validate({"kind": "plan", "steps": "not a list", "reasoning": "x"})  # wrong type
    except Exception as exc:
        cause = exc
    wrapped = LLMOutputInvalid(str(cause))
    wrapped.__cause__ = cause

    message = _retry_instruction(wrapped)
    assert "steps" in message
    assert "were wrong" in message


def test_retry_instruction_falls_back_to_the_plain_message_for_non_validation_causes():
    """A scripted test exception, a refusal, or a network error has no
    structured field-level detail to extract; nothing more specific to
    say than the plain message in those cases.
    """

    from harness.planning.planner import _retry_instruction

    exc = LLMOutputInvalid("model returned no content")  # no __cause__ at all
    assert _retry_instruction(exc) == "model returned no content"


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

    output_model = _planner_output_model("stockout")
    with pytest.raises(ValidationError):
        output_model.model_validate({
            "proposal": {
                "kind": "workflow", "workflow": "not_a_real_workflow", "params": {},
                "reasoning": "x", "summary_for_user": "y",
            }
        })


def test_planner_output_model_excludes_workflow_entirely_with_no_registered_workflows():
    """If nothing is registered, `workflow` cannot be narrowed to a Literal
    of valid names (Literal[()] isn't meaningful). Falling back to the
    unconstrained static PlannerOutput would make WorkflowRequest.workflow
    a bare `str` the model could fill with anything, passing validation
    here only to crash the caller later when nothing matches an actual
    definition; excluding WorkflowRequest from the union entirely fails
    validation right here instead, cleanly.
    """

    import harness.execution.engine as engine_module
    from pydantic import ValidationError

    from harness.planning.planner import _planner_output_model

    saved_registry = dict(engine_module._REGISTRY)
    engine_module._REGISTRY.clear()
    try:
        output_model = _planner_output_model("stockout")
        assert output_model is not PlannerOutput
        with pytest.raises(ValidationError):
            output_model.model_validate({
                "proposal": {
                    "kind": "workflow", "workflow": "anything", "params": {},
                    "reasoning": "x", "summary_for_user": "y",
                }
            })
        # ToolPlan and NoAction still validate normally.
        output_model.model_validate({"proposal": {"kind": "none", "reasoning": "x"}})
    finally:
        engine_module._REGISTRY.clear()
        engine_module._REGISTRY.update(saved_registry)


def test_each_proposal_variants_schema_title_matches_its_own_kind_value():
    """Under non-strict structured output (required elsewhere by the open
    dict fields on ToolPlan/WorkflowRequest), the `const` on `kind` is
    documentary, not enforced by the API. A real model has substituted a
    variant's own schema title for the literal it should have copied from
    `const` (`{"kind": "NoAction", ...}` instead of `{"kind": "none", ...}`).
    Making every variant's title identical to its own kind value removes
    the ambiguity regardless of which variant the model confuses.
    """

    from harness.planning.models import NoAction, ToolPlan, WorkflowRequest

    for model, expected_kind in [(ToolPlan, "plan"), (WorkflowRequest, "workflow"), (NoAction, "none")]:
        schema = model.model_json_schema()
        assert schema["title"] == expected_kind
        assert schema["properties"]["kind"]["const"] == expected_kind


def test_summary_for_user_defaults_rather_than_failing_when_the_model_gets_it_wrong():
    """A real model has both written the shorter `summary` instead of
    `summary_for_user`, and on a separate occasion dropped the field
    entirely, on both an original attempt and its one retry, failing the
    run over a field nothing downstream actually reads: `summary_for_user`
    is archived into the audit log's record of the proposal and never
    consulted by the gate, the workflow engine, execution, or `explain`.
    Rather than chase every way a model might get this one field wrong,
    it now defaults: to `summary` if that's what was written (closest to
    what was actually meant), else to `reasoning` itself (already a short,
    grounded justification in this build's own examples). The schema
    shown to the model is unchanged either way: `model_json_schema()`
    always asks for the real name, never an alias, and never marks it
    optional, so a well-behaved model is still told to supply it normally.
    """

    from harness.planning.models import ToolPlan, WorkflowRequest

    assert "summary" not in ToolPlan.model_json_schema()["properties"]
    assert "summary_for_user" in ToolPlan.model_json_schema()["required"]

    # The model did it right: its own value is used, not the fallback.
    assert ToolPlan.model_validate(
        {"kind": "plan", "steps": [], "reasoning": "x", "summary_for_user": "real text"}
    ).summary_for_user == "real text"

    # The model used "summary" instead: that value is used.
    assert ToolPlan.model_validate(
        {"kind": "plan", "steps": [], "reasoning": "x", "summary": "wrong key, right intent"}
    ).summary_for_user == "wrong key, right intent"

    # The model dropped it entirely: falls back to reasoning, same for WorkflowRequest.
    assert ToolPlan.model_validate(
        {"kind": "plan", "steps": [], "reasoning": "dropped the field entirely"}
    ).summary_for_user == "dropped the field entirely"
    assert WorkflowRequest.model_validate(
        {"kind": "workflow", "workflow": "reroute_po", "params": {}, "reasoning": "dropped here too"}
    ).summary_for_user == "dropped here too"

    # Both keys present at once: summary_for_user wins; "summary" is
    # always dropped rather than ever surfacing as a forbidden extra field.
    assert ToolPlan.model_validate({
        "kind": "plan", "steps": [], "reasoning": "x",
        "summary_for_user": "real", "summary": "ignored",
    }).summary_for_user == "real"

    # Re-validating an already-constructed instance (not a dict) passes
    # straight through rather than erroring on the dict-only lookups above.
    original = ToolPlan.model_validate({"kind": "plan", "steps": [], "reasoning": "x", "summary_for_user": "y"})
    assert ToolPlan.model_validate(original).summary_for_user == "y"


def test_planner_module_has_no_scenario_specific_strings():
    banned = ["P-4471", "PO-77812", "Supplier Z", "S-Z", "4812", "Dana", "Kestrel", "Meridian"]
    for path in PLANNING_ROOT.glob("*.py"):
        text = path.read_text()
        for term in banned:
            assert term not in text, f"{term!r} found in {path}"
