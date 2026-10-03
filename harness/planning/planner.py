"""The free-form planner (section 10): attention item + context + memory
hints -> one proposal. One retry on invalid output, then the run fails and
reports why. Contains no scenario-specific wording; everything it shows
the model comes from the live registries and the gathered context.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Annotated, Literal, Union

from pydantic import BaseModel, ConfigDict, Field, ValidationError, create_model

from harness.audit.log import log as audit_log
from harness.context.base import ContextSlice
from harness.detection.base import AttentionItem
from harness.execution.engine import registered_workflow_names_for_detector
from harness.planning.llm import LLMClient, LLMOutputInvalid
from harness.planning.models import NoAction, Proposal, ToolPlan, WorkflowRequest
from harness.planning.prompt import build_messages
from harness.scheduling.clock import Clock
from harness.world.users import User


class PlannerFailed(Exception):
    pass


def _retry_instruction(exc: LLMOutputInvalid) -> str:
    """A directive, plain-language description of what was wrong, for the
    one retry message. The default would be `str(exc)`: a dense,
    stack-trace-shaped Pydantic error (field paths, a `For further
    information visit <url>` line) that a real model is more likely to
    skim past than act on, observed directly: a retry relaying the raw
    error still dropped the same field again. `OpenAIClient` raises
    `LLMOutputInvalid(...) from exc`, so the original `ValidationError` is
    reachable as `__cause__`; naming exactly which field was missing or
    wrong, in an instruction rather than a report, is what the retry is
    actually for. Falls back to the plain message when the cause isn't a
    structured validation error (a scripted test exception, a refusal, a
    network error) -- nothing more specific to say in those cases.
    """

    cause = exc.__cause__
    if not isinstance(cause, ValidationError):
        return str(exc)

    missing = []
    wrong = []
    for error in cause.errors():
        field = ".".join(str(part) for part in error["loc"])
        if error["type"] == "missing":
            missing.append(field)
        else:
            wrong.append(f"{field} ({error['msg']})")

    parts = []
    if missing:
        parts.append(f"You left out required field(s): {', '.join(missing)}.")
    if wrong:
        parts.append(f"These fields were wrong: {', '.join(wrong)}.")
    parts.append("Every field the schema marks as required must be present and correctly typed.")
    return " ".join(parts)


def _planner_output_model(detector: str) -> type[BaseModel]:
    """PlannerOutput, rebuilt per call with `WorkflowRequest.workflow`
    narrowed to a Literal of whatever is actually registered *and
    applicable to this item's detector* right now. Prompt text alone is
    not enough on either count: a model shown the catalog as prose still
    invents workflow names outside it (observed against a real model), and
    separately, a model shown a workflow that doesn't apply to this kind
    of item will still sometimes propose it anyway, force-fitting an
    invented or repurposed parameter rather than recognizing it as a
    non-match (observed directly, against a real model, for an attention
    item `reroute_po` was never meant to handle). A schema-level enum is
    what section 7 means by "built from the registries at runtime," and
    narrowing it by applicability is what actually prevents the model from
    naming a workflow it was never shown as an option. The narrowed
    subclass still `isinstance`-checks as WorkflowRequest everywhere else.
    """

    names = tuple(registered_workflow_names_for_detector(detector))
    if not names:
        # No declared workflow applies to this kind of item at all: drop
        # WorkflowRequest from the proposal union entirely, rather than
        # falling back to the unconstrained static PlannerOutput, whose
        # WorkflowRequest.workflow is a bare `str` the model could fill
        # with anything, including a tool's own name, which would pass
        # this validation only to crash the caller later (get_definition
        # raising) instead of failing cleanly here, where a bad proposal
        # is supposed to be caught.
        return create_model(
            "PlannerOutput",
            __config__=ConfigDict(extra="forbid"),
            proposal=(Annotated[Union[ToolPlan, NoAction], Field(discriminator="kind")], ...),
        )

    narrowed_request = create_model(
        "WorkflowRequest", __base__=WorkflowRequest, workflow=(Literal[names], ...),
    )
    return create_model(
        "PlannerOutput",
        __config__=ConfigDict(extra="forbid"),
        proposal=(Annotated[Union[ToolPlan, narrowed_request, NoAction], Field(discriminator="kind")], ...),
    )


def propose(
    conn: sqlite3.Connection,
    clock: Clock,
    llm_client: LLMClient,
    item: AttentionItem,
    context: dict[str, ContextSlice],
    memory_facts: list[dict],
    user: User,
    *,
    run_id: str | None = None,
) -> Proposal:
    messages = build_messages(item, context, memory_facts, user)
    output_model = _planner_output_model(item.detector)

    try:
        output = llm_client.complete(messages, output_model)
    except LLMOutputInvalid as exc:
        retry_messages = messages + [{
            "role": "user",
            "content": f"Your previous response was invalid. {_retry_instruction(exc)} "
                       "Respond again with a single complete, valid object.",
        }]
        try:
            output = llm_client.complete(retry_messages, output_model)
        except LLMOutputInvalid as exc2:
            audit_log(
                conn, clock, run_id=run_id, actor="planner", event="planner.invalid",
                detail={"first_error": str(exc), "second_error": str(exc2)},
            )
            conn.commit()
            raise PlannerFailed(str(exc2)) from exc2

    proposal = output.proposal
    audit_log(
        conn, clock, run_id=run_id, actor="planner", event="planner.proposed",
        detail={
            "kind": proposal.kind,
            "reasoning": proposal.reasoning,
            "proposal": json.loads(proposal.model_dump_json()),
        },
    )
    conn.commit()
    return proposal
