"""The gate (section 11): pure functions over a proposal, tool metadata, and
users. Nothing here calls an LLM, and nothing here is advisory, it is what
actually blocks a write.

`gate()` takes a flat, ordered list of ToolCall steps, since that is what
every proposal eventually reduces to by the time something needs approval:
a free-form ToolPlan's steps directly, or the tool calls a declared
workflow assembles internally before it asks for approval (section 12,
steps 5-8 of reroute_po). The one difference between the two paths is the
`workflow` argument: None means free-form, so any tool with `allowed_in`
set is rejected outright; a workflow name means the caller is inside that
workflow, so a tool scoped to it is allowed.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from harness.execution.catalog import UnknownTool, get_tool
from harness.execution.tools import Tool
from harness.planning.models import ToolCall
from harness.world.users import User, get_user, missing_scopes

APPROVAL_LIMIT_KEY = "po_create_max"


@dataclass(frozen=True)
class Blocked:
    reason: str
    # True only for a rejection a fresh proposal could plausibly fix (see
    # the all-non-resolving-tools rule below) -- the caller's signal that
    # one re-plan is worth trying before giving up and reporting to a
    # human, per the locked decision allowing at most one such retry.
    retryable: bool = False


@dataclass(frozen=True)
class Allowed:
    approver_id: str
    routed_reason: str | None


GateResult = Blocked | Allowed


def approver_chain(conn: sqlite3.Connection, start: User) -> list[User]:
    """The requester, then their manager, then their manager's manager, and
    so on. Stops at the first missing or already-seen manager_id so a bad
    seed can't loop forever.
    """

    chain = [start]
    seen = {start.user_id}
    current = start
    while current.manager_id and current.manager_id not in seen:
        manager = get_user(conn, current.manager_id)
        chain.append(manager)
        seen.add(current.manager_id)
        current = manager
    return chain


def qualifying_approver(conn: sqlite3.Connection, start: User, value: float) -> User | None:
    """The first person in start's chain (start included) whose
    po_create_max covers value, or None if nobody in the chain does.
    """

    for candidate in approver_chain(conn, start):
        limit = (candidate.approval_limits or {}).get(APPROVAL_LIMIT_KEY)
        if limit is not None and limit >= value:
            return candidate
    return None


def plan_value(steps: list[ToolCall]) -> float:
    """Sum of every step's dollar value, for steps whose tool declares one.
    A tool with no `value` contributes nothing and so never triggers the
    threshold rule.
    """

    total = 0.0
    for step in steps:
        tool = get_tool(step.tool)
        if tool.value is not None:
            args = tool.input_schema.model_validate(step.args)
            total += tool.value(args)
    return total


def gate(
    conn: sqlite3.Connection,
    requester: User,
    steps: list[ToolCall],
    *,
    workflow: str | None = None,
) -> GateResult:
    total_value = 0.0
    tools: list[Tool] = []

    for step in steps:
        try:
            tool: Tool = get_tool(step.tool)
        except UnknownTool:
            return Blocked(f"unknown tool: {step.tool}")

        try:
            validated_args = tool.input_schema.model_validate(step.args)
        except Exception as exc:
            return Blocked(f"invalid args for {tool.name}: {exc}")

        if tool.allowed_in is not None:
            if workflow is None or workflow not in tool.allowed_in:
                return Blocked(
                    f"{tool.name} is workflow-only (allowed in {tool.allowed_in}) and "
                    "cannot run in a free-form plan"
                )

        missing = missing_scopes(requester, tool.required_scopes)
        if missing:
            return Blocked(f"{requester.user_id} lacks scope {missing[0]} for {tool.name}")

        if tool.value is not None:
            total_value += tool.value(validated_args)

        tools.append(tool)

    if workflow is None and tools and not any(t.resolves for t in tools):
        # A declared workflow's own fixed steps always include a real
        # resolving action by construction, so this only matters for
        # free-form: a plan whose every step only informs someone
        # (notify_user) never actually addresses the attention item.
        # Retryable, since a fresh proposal naming an actual resolving
        # tool is a plausible fix a re-plan could produce.
        return Blocked(
            "this plan consists entirely of non-resolving actions (e.g. notify_user) "
            "and does not address the attention item itself; propose NoAction if "
            "nothing should be done, or include the tool that actually resolves it",
            retryable=True,
        )

    if total_value > 0:
        approver = qualifying_approver(conn, requester, total_value)
        if approver is None:
            return Blocked(f"no approver in {requester.user_id}'s chain covers value {total_value}")
        routed_reason = None
        if approver.user_id != requester.user_id:
            routed_reason = (
                f"value {total_value} exceeds {requester.user_id}'s limit; "
                f"routed to {approver.user_id}"
            )
        return Allowed(approver_id=approver.user_id, routed_reason=routed_reason)

    return Allowed(approver_id=requester.user_id, routed_reason=None)
