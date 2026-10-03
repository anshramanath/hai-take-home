"""The declarative workflow engine (section 12). A WorkflowDefinition is a
fixed, ordered tuple of Steps; the engine runs them in that order and
never lets anything (including the LLM, inside a bounded "llm" step)
reorder, skip, or add to them.

The boundary between "runs freely" and "needs approval" is structural, not
hardcoded per workflow: the engine runs consecutive "check"/"llm" steps
without stopping, and halts at the first "action" step until the instance's
state carries `_approved`. That rule falls straight out of invariant 2
(every write needs approval) with no workflow-specific logic in the engine
itself.

Once approved, action steps never recompute anything: they read their args
back from the approval's own frozen, hash-verified plan_json
(`_approved_plan_steps` in state), never from a fresh calculation. This is
what makes the hash check meaningful rather than theater, and is what
invariant 3 ("zero LLM calls between approval and execution") actually
rests on for the non-LLM, code-computed parts of a step's args too:
nothing is computed after approval, full stop.

State is persisted (current_step, state JSON) after every step, so
`resume_all()` can pick up a workflow instance exactly where a killed
process left it.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from dataclasses import dataclass
from typing import Any, Callable

from harness.audit.log import log as audit_log
from harness.execution.catalog import get_tool
from harness.execution.executor import compensate as compensate_tool
from harness.execution.executor import execute as execute_tool
from harness.execution.tools import ToolContext
from harness.planning.llm import LLMClient
from harness.planning.models import ToolCall
from harness.policy.approvals import create_approval, get_approval, verify_plan_hash
from harness.policy.gate import Allowed, Blocked, gate
from harness.scheduling.clock import Clock
from harness.world.users import User

ACTION_ACTOR_PREFIX = "workflow"


class UnknownWorkflowDefinition(Exception):
    pass


class UnknownWorkflowInstance(Exception):
    pass


class SimulatedCrash(Exception):
    """Raised by _run() when a step's name matches crash_after, right after
    that step's completion has been persisted and committed. Exists only
    for tests that simulate a killed process.
    """

    def __init__(self, step_name: str):
        super().__init__(f"simulated crash after step {step_name!r}")
        self.step_name = step_name


class StepHalted(Exception):
    """Raised by a check/llm step's fn to stop the instance cleanly: this
    is a legitimate outcome (no qualifying supplier, an LLM that could not
    settle on a valid choice), not a bug. No compensation runs, since
    nothing has written anything yet by the time any check/llm step raises
    this.
    """

    def __init__(self, status: str, reason: str):
        super().__init__(reason)
        self.status = status
        self.reason = reason


@dataclass(frozen=True)
class StepContext:
    conn: sqlite3.Connection
    clock: Clock
    state: dict[str, Any]
    run_id: str
    instance_id: str
    requester_id: str
    llm_client: LLMClient


@dataclass(frozen=True)
class Step:
    name: str
    kind: str  # "check" | "llm" | "action"
    fn: Callable[[StepContext], dict[str, Any]]
    compensate: str | None = None


@dataclass(frozen=True)
class WorkflowDefinition:
    name: str
    version: int
    description: str
    params_model: type
    steps: tuple[Step, ...]
    build_plan_steps: Callable[[sqlite3.Connection, Clock, dict[str, Any], str], list[ToolCall]]
    applies_to_detectors: tuple[str, ...]
    """Which detector names raise the kind of attention item this workflow
    can resolve. A real model, shown this workflow as an option regardless
    of what raised the item, will sometimes repurpose an unrelated id to
    force-fit it onto a problem it was never meant for. Prompt wording
    describing when a workflow applies helps but isn't reliable by itself;
    this makes "does this workflow even apply to this kind of item" a
    structural question the planner answers before the model is ever shown
    the workflow at all, not a judgment call left to the model every time.
    Declared per workflow (here, not in the planner) so invariant 10
    holds: the planner's own filtering logic never names a specific
    detector or workflow."""


_REGISTRY: dict[tuple[str, int], WorkflowDefinition] = {}


def register(definition: WorkflowDefinition) -> None:
    _REGISTRY[(definition.name, definition.version)] = definition


def get_definition(name: str, version: int) -> WorkflowDefinition:
    try:
        return _REGISTRY[(name, version)]
    except KeyError:
        raise UnknownWorkflowDefinition(f"{name} v{version}") from None


def latest_version(name: str) -> int:
    versions = [version for (registered_name, version) in _REGISTRY if registered_name == name]
    if not versions:
        raise UnknownWorkflowDefinition(name)
    return max(versions)


def registered_workflow_names() -> list[str]:
    """Every distinct workflow name with at least one registered version,
    sorted for a stable prompt."""

    return sorted({name for (name, _version) in _REGISTRY})


def registered_workflow_names_for_detector(detector: str) -> list[str]:
    """Only the names of workflows that declare `detector` in their own
    `applies_to_detectors`. The one place that knowledge is used; it reads
    a field each workflow declares about itself, so this stays generic
    over whatever detector name it's given, never naming one.
    """

    names = set()
    for name in registered_workflow_names():
        definition = get_definition(name, latest_version(name))
        if detector in definition.applies_to_detectors:
            names.add(name)
    return sorted(names)


def workflow_catalog_for_prompt(detector: str | None = None) -> list[dict[str, Any]]:
    """What the planner shows the model for each registered workflow (at
    its latest version): name, description, and the exact params schema it
    must supply — built from the registry at runtime, never hardcoded
    (section 7). A bare name is not enough for the model to know when a
    workflow applies or what parameters it takes.

    When `detector` is given, only workflows that declare it are shown at
    all: a model can't force-fit a workflow it never sees as an option.
    """

    names = registered_workflow_names_for_detector(detector) if detector is not None else registered_workflow_names()
    catalog = []
    for name in names:
        definition = get_definition(name, latest_version(name))
        catalog.append({
            "name": definition.name,
            "description": definition.description,
            "params_schema": definition.params_model.model_json_schema(),
        })
    return catalog


def approved_args(state: dict[str, Any], tool_name: str) -> dict[str, Any]:
    for entry in state["_approved_plan_steps"]:
        if entry["tool"] == tool_name:
            return entry["args"]
    raise KeyError(f"no approved plan step found for tool {tool_name}")


def run_approved_action(
    ctx: StepContext, tool_name: str, step_name: str, *, overrides: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Shared body for every action step: look up this step's args from the
    approved, hash-verified plan (never recomputed), execute through the
    one shared executor, and append a compensation-log entry so a later
    failure can be backed out in reverse.

    `overrides` is not recomputing a decision: it substitutes a system fact
    an earlier step's own result produced (F3's `promised_date`, which the
    supplier system only hands back at order-placement time) into a
    placeholder the frozen plan carried instead of a value nobody could
    have approved ahead of time. Every field a human actually approved
    still comes only from the frozen plan.
    """

    tool = get_tool(tool_name)
    raw_args = dict(approved_args(ctx.state, tool_name))
    if overrides:
        raw_args.update(overrides)
    args = tool.input_schema.model_validate(raw_args)
    tool_ctx = ToolContext(run_id=f"{ctx.run_id}:{ctx.instance_id}", step=step_name, today=ctx.clock.today())
    result = execute_tool(
        ctx.conn, ctx.clock, tool, args, tool_ctx,
        run_id=ctx.run_id, actor=f"{ACTION_ACTOR_PREFIX}:reroute_po", requester_id=ctx.requester_id,
    )
    entry = {"step": step_name, "tool": tool_name, "args": json.loads(args.model_dump_json()), "result": result}
    return {"_compensation_log": ctx.state.get("_compensation_log", []) + [entry]}


# ---------------------------------------------------------------------------
# Instance persistence


def get_instance(conn: sqlite3.Connection, instance_id: str) -> sqlite3.Row:
    row = conn.execute(
        "SELECT * FROM workflow_instances WHERE instance_id = ?", (instance_id,)
    ).fetchone()
    if row is None:
        raise UnknownWorkflowInstance(instance_id)
    return row


def _persist(conn: sqlite3.Connection, instance_id: str, current_step: int, state: dict[str, Any]) -> None:
    conn.execute(
        "UPDATE workflow_instances SET current_step = ?, state = ? WHERE instance_id = ?",
        (current_step, json.dumps(state), instance_id),
    )


def _set_status(conn: sqlite3.Connection, instance_id: str, status: str) -> None:
    conn.execute("UPDATE workflow_instances SET status = ? WHERE instance_id = ?", (status, instance_id))


def set_instance_status(conn: sqlite3.Connection, instance_id: str, status: str) -> None:
    """Public wrapper so a caller outside this module (the orchestrator,
    marking an instance `rejected`) can force a terminal status without
    reaching into a private helper.
    """

    _set_status(conn, instance_id, status)
    conn.commit()


def start(
    conn: sqlite3.Connection,
    clock: Clock,
    definition: WorkflowDefinition,
    params: dict[str, Any],
    *,
    run_id: str,
    requester_id: str,
) -> str:
    validated = definition.params_model.model_validate(params)
    instance_id = f"WF-{uuid.uuid4().hex[:8].upper()}"
    state = validated.model_dump()
    state["_requester_id"] = requester_id
    conn.execute(
        "INSERT INTO workflow_instances (instance_id, run_id, definition, version, current_step, "
        "state, status) VALUES (?, ?, ?, ?, 0, ?, 'running')",
        (instance_id, run_id, definition.name, definition.version, json.dumps(state)),
    )
    conn.commit()
    return instance_id


# ---------------------------------------------------------------------------
# Running steps


def _compensate_all(
    conn: sqlite3.Connection, clock: Clock, instance_id: str, run_id: str, state: dict[str, Any], requester_id: str
) -> None:
    for entry in reversed(state.get("_compensation_log", [])):
        tool = get_tool(entry["tool"])
        args = tool.input_schema.model_validate(entry["args"])
        tool_ctx = ToolContext(run_id=f"{run_id}:{instance_id}", step=entry["step"], today=clock.today())
        compensate_tool(
            conn, clock, tool, args, entry["result"], tool_ctx,
            run_id=run_id, actor=f"{ACTION_ACTOR_PREFIX}:reroute_po", requester_id=requester_id,
        )
    _set_status(conn, instance_id, "compensated")
    conn.commit()


def _run(
    conn: sqlite3.Connection,
    clock: Clock,
    llm_client: LLMClient,
    instance_id: str,
    *,
    crash_after: str | None = None,
) -> sqlite3.Row:
    row = get_instance(conn, instance_id)
    if row["status"] != "running":
        return row

    definition = get_definition(row["definition"], row["version"])
    state: dict[str, Any] = json.loads(row["state"])
    run_id = row["run_id"]
    requester_id = state["_requester_id"]
    current_step = row["current_step"]

    while current_step < len(definition.steps):
        step = definition.steps[current_step]

        if step.kind == "action" and not state.get("_approved"):
            _set_status(conn, instance_id, "awaiting_approval")
            conn.commit()
            return get_instance(conn, instance_id)

        ctx = StepContext(
            conn=conn, clock=clock, state=state, run_id=run_id, instance_id=instance_id,
            requester_id=requester_id, llm_client=llm_client,
        )
        audit_log(
            conn, clock, run_id=run_id, actor="workflow", event="workflow.step_started",
            detail={"instance_id": instance_id, "definition": definition.name, "version": definition.version,
                    "step": step.name, "kind": step.kind},
        )
        conn.commit()

        try:
            updates = step.fn(ctx)
        except StepHalted as halt:
            _set_status(conn, instance_id, halt.status)
            audit_log(
                conn, clock, run_id=run_id, actor="workflow", event="workflow.halted",
                detail={"instance_id": instance_id, "step": step.name, "status": halt.status, "reason": halt.reason},
            )
            conn.commit()
            return get_instance(conn, instance_id)
        except Exception as exc:
            audit_log(
                conn, clock, run_id=run_id, actor="workflow", event="workflow.step_failed",
                detail={"instance_id": instance_id, "step": step.name, "error": str(exc)},
            )
            conn.commit()
            _compensate_all(conn, clock, instance_id, run_id, state, requester_id)
            return get_instance(conn, instance_id)

        state.update(updates)
        current_step += 1
        _persist(conn, instance_id, current_step, state)
        audit_log(
            conn, clock, run_id=run_id, actor="workflow", event="workflow.step_completed",
            detail={
                "instance_id": instance_id, "step": step.name,
                "updates": {k: v for k, v in updates.items() if not k.startswith("_")},
            },
        )
        conn.commit()

        if crash_after == step.name:
            raise SimulatedCrash(step.name)

    _set_status(conn, instance_id, "completed")
    conn.commit()
    return get_instance(conn, instance_id)


def enter_workflow(
    conn: sqlite3.Connection,
    clock: Clock,
    llm_client: LLMClient,
    definition: WorkflowDefinition,
    params: dict[str, Any],
    *,
    run_id: str,
    requester: User,
    crash_after: str | None = None,
) -> sqlite3.Row:
    """Start an instance and run it up to (and including) requesting
    approval, if it gets that far. The planner decided to enter this
    workflow and supplied `params`; everything from here is the
    definition's, not the model's.
    """

    instance_id = start(conn, clock, definition, params, run_id=run_id, requester_id=requester.user_id)
    row = _run(conn, clock, llm_client, instance_id, crash_after=crash_after)

    if row["status"] != "awaiting_approval":
        return row

    state = json.loads(row["state"])
    plan_steps = definition.build_plan_steps(conn, clock, state, run_id)
    result = gate(conn, requester, plan_steps, workflow=f"{ACTION_ACTOR_PREFIX}:{definition.name}")

    if isinstance(result, Blocked):
        audit_log(
            conn, clock, run_id=run_id, actor="gate", event="gate.blocked",
            detail={"instance_id": instance_id, "reason": result.reason},
        )
        _set_status(conn, instance_id, "failed")
        conn.commit()
        return get_instance(conn, instance_id)

    assert isinstance(result, Allowed)
    audit_log(
        conn, clock, run_id=run_id, actor="gate", event="gate.allowed",
        detail={"instance_id": instance_id, "approver_id": result.approver_id, "routed_reason": result.routed_reason},
    )
    conn.commit()

    approval_id = create_approval(
        conn, clock, run_id=run_id, requester=requester, steps=plan_steps,
        approver_id=result.approver_id, routed_reason=result.routed_reason,
        workflow=f"{ACTION_ACTOR_PREFIX}:{definition.name}",
    )
    state["_approval_id"] = approval_id
    _persist(conn, instance_id, row["current_step"], state)
    conn.commit()
    return get_instance(conn, instance_id)


def resume_after_approval(
    conn: sqlite3.Connection,
    clock: Clock,
    llm_client: LLMClient,
    instance_id: str,
    *,
    crash_after: str | None = None,
) -> sqlite3.Row:
    """Call once the approval for this instance has been granted. Verifies
    the approval is actually approved and its plan still matches its
    recorded hash, then runs the action steps using exactly that plan.
    """

    row = get_instance(conn, instance_id)
    state: dict[str, Any] = json.loads(row["state"])
    approval_id = state.get("_approval_id")
    if approval_id is None:
        raise ValueError(f"instance {instance_id} has no pending approval")

    approval = get_approval(conn, approval_id)
    if approval["status"] != "approved":
        raise ValueError(f"approval {approval_id} is not approved (status={approval['status']})")

    if not verify_plan_hash(approval["plan_json"], approval["plan_hash"]):
        audit_log(
            conn, clock, run_id=row["run_id"], actor="workflow", event="workflow.halted",
            detail={"instance_id": instance_id, "reason": "approved plan does not match its recorded hash"},
        )
        _set_status(conn, instance_id, "failed")
        conn.commit()
        return get_instance(conn, instance_id)

    plan = json.loads(approval["plan_json"])
    state["_approved_plan_steps"] = plan["steps"]
    state["_approved"] = True
    conn.execute(
        "UPDATE workflow_instances SET status = 'running', state = ? WHERE instance_id = ?",
        (json.dumps(state), instance_id),
    )
    conn.commit()
    return _run(conn, clock, llm_client, instance_id, crash_after=crash_after)


def resume_all(conn: sqlite3.Connection, clock: Clock, llm_client: LLMClient) -> list[str]:
    """Resume every instance left mid-sequence (status='running') by a
    killed process. Called on startup and on every tick.
    """

    rows = conn.execute("SELECT instance_id FROM workflow_instances WHERE status = 'running'").fetchall()
    resumed = []
    for (instance_id,) in rows:
        _run(conn, clock, llm_client, instance_id)
        resumed.append(instance_id)
    return resumed
