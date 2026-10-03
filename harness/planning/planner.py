"""The free-form planner (section 10): attention item + context + memory
hints -> one proposal. One retry on invalid output, then the run fails and
reports why. Contains no scenario-specific wording; everything it shows
the model comes from the live registries and the gathered context.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Annotated, Literal, Union

from pydantic import BaseModel, ConfigDict, Field, create_model

from harness.audit.log import log as audit_log
from harness.context.base import ContextSlice
from harness.detection.base import AttentionItem
from harness.execution.engine import registered_workflow_names
from harness.planning.llm import LLMClient, LLMOutputInvalid
from harness.planning.models import NoAction, PlannerOutput, Proposal, ToolPlan, WorkflowRequest
from harness.planning.prompt import build_messages
from harness.scheduling.clock import Clock
from harness.world.users import User


class PlannerFailed(Exception):
    pass


def _planner_output_model() -> type[BaseModel]:
    """PlannerOutput, rebuilt per call with `WorkflowRequest.workflow`
    narrowed to a Literal of whatever is actually registered right now.
    Prompt text alone is not enough: a model shown the catalog as prose
    still invents workflow names outside it (observed against a real
    model); a schema-level enum is what section 7 means by "built from the
    registries at runtime," and it's what actually prevents the model from
    entering a workflow that doesn't exist. The narrowed subclass still
    `isinstance`-checks as WorkflowRequest everywhere else.
    """

    names = tuple(registered_workflow_names())
    if not names:
        return PlannerOutput

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
    output_model = _planner_output_model()

    try:
        output = llm_client.complete(messages, output_model)
    except LLMOutputInvalid as exc:
        retry_messages = messages + [{
            "role": "user",
            "content": f"Your previous output was invalid: {exc}. Respond again with valid output.",
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
