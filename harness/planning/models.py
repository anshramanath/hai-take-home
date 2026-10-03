"""The planner's output contracts (section 7 of the spec). Defined now,
ahead of the rest of planning/ (prompt building, LLM clients land in phase
4), because the gate needs something concrete to evaluate: a `ToolPlan` is
exactly what a free-form proposal looks like once validated, and it is also
the shape a declared workflow freezes into before requesting approval.

`extra="forbid"` on every model matters: it is what makes "the model cannot
change step order" enforceable later. `WorkflowRequest` has no `steps`
field at all, so a workflow proposal can request parameters but never
dictate step order; `ToolPlan.steps` is a flat list the gate and executor
run in order, with no way to smuggle in extra fields a step wasn't declared
with.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class ToolCall(BaseModel):
    """One step of a free-form plan, or one step of a frozen, approved plan."""

    model_config = ConfigDict(extra="forbid")

    tool: str
    args: dict[str, Any]


class ToolPlan(BaseModel):
    """The free-form planner's proposal: a whole ordered plan, up front."""

    model_config = ConfigDict(extra="forbid")

    kind: Literal["plan"]
    steps: list[ToolCall]
    reasoning: str
    summary_for_user: str


class WorkflowRequest(BaseModel):
    """The planner decides a declared workflow applies and supplies its
    parameters. It does not, and cannot, specify steps; the workflow
    definition owns step order entirely.
    """

    model_config = ConfigDict(extra="forbid")

    kind: Literal["workflow"]
    workflow: str
    params: dict[str, Any]
    reasoning: str
    summary_for_user: str


class NoAction(BaseModel):
    """The planner judges that nothing should be done."""

    model_config = ConfigDict(extra="forbid")

    kind: Literal["none"]
    reasoning: str


Proposal = ToolPlan | WorkflowRequest | NoAction


class PlannerOutput(BaseModel):
    """The one schema actually sent as the LLM's response_format: a
    discriminated union over the three proposal kinds, keyed on each
    variant's `kind` literal, so the model must commit to exactly one.
    """

    model_config = ConfigDict(extra="forbid")

    proposal: Annotated[ToolPlan | WorkflowRequest | NoAction, Field(discriminator="kind")]
