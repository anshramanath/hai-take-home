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

from pydantic import BaseModel, ConfigDict, Field, model_validator


def _default_summary_for_user(data: Any) -> Any:
    """`summary_for_user` is part of the proposal shape but is never read
    anywhere once written: it lands in the audit log's record of the
    proposal and nowhere else (`reasoning` is what `explain` actually
    renders, and nothing in the gate, approvals, or execution path
    consumes either). A real model has dropped it entirely and, on a
    separate occasion, written the shorter `summary` instead, failing
    validation both times over a field nothing downstream depends on.
    Rather than chase every way a model might misname or omit it, this
    makes it default to whatever is closest to what was actually meant:
    the model's own `summary`, if that's what it wrote, otherwise its
    `reasoning`, which is already a short, grounded justification in this
    build's own examples. Fields the gate, the workflow engine, or
    execution actually consume (`kind`, `steps`, `reasoning`, `workflow`,
    `params`) stay strictly required with no such fallback.
    """

    if not isinstance(data, dict):
        return data
    alternate = data.pop("summary", None)  # never a real field; always drop it
    if not data.get("summary_for_user"):
        data["summary_for_user"] = alternate or data.get("reasoning", "")
    return data


class ToolCall(BaseModel):
    """One step of a free-form plan, or one step of a frozen, approved plan."""

    model_config = ConfigDict(extra="forbid")

    tool: str
    args: dict[str, Any]


class ToolPlan(BaseModel):
    """The free-form planner's proposal: a whole ordered plan, up front."""

    # `title` matches the model's own `kind` value, not the Python class
    # name: under non-strict structured output (required elsewhere by the
    # open `dict` fields on this and `WorkflowRequest`), the schema's
    # `const` on `kind` is documentary, not enforced by the API itself, and
    # a real model has substituted a variant's own schema title for the
    # literal it was supposed to copy from `const` (observed directly, for
    # `NoAction` emitting `{"kind": "NoAction", ...}`). Making the title
    # and the required value identical for every variant removes that
    # failure mode regardless of which one the model confuses.
    model_config = ConfigDict(extra="forbid", title="plan")

    kind: Literal["plan"]
    steps: list[ToolCall]
    reasoning: str
    summary_for_user: str

    _default_summary = model_validator(mode="before")(_default_summary_for_user)


class WorkflowRequest(BaseModel):
    """The planner decides a declared workflow applies and supplies its
    parameters. It does not, and cannot, specify steps; the workflow
    definition owns step order entirely.
    """

    model_config = ConfigDict(extra="forbid", title="workflow")

    kind: Literal["workflow"]
    workflow: str
    params: dict[str, Any]
    reasoning: str
    summary_for_user: str

    _default_summary = model_validator(mode="before")(_default_summary_for_user)


class NoAction(BaseModel):
    """The planner judges that nothing should be done."""

    model_config = ConfigDict(extra="forbid", title="none")

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
