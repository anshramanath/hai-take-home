"""Builds the planner's prompt from the attention item, the gathered
context, persistent memory hints, and catalogs built from the live
registries — never hardcoded, and never containing any wording specific to
one scenario (the planner must work for whatever attention item it's
handed without being told in advance what kind of problem it is).
"""

from __future__ import annotations

import json

from harness.context.base import ContextSlice
from harness.detection.base import AttentionItem
from harness.execution.catalog import all_tools
from harness.execution.engine import workflow_catalog_for_prompt
from harness.world.users import User

SYSTEM_PROMPT = (
    "You are an operations agent helping an employee at a manufacturer. You are given "
    "an attention item that was raised automatically by a detector, context gathered "
    "from the systems you have access to, and a catalog of declared workflows and tools "
    "you may propose.\n\n"
    "A detector flags risk from structured ERP data alone; it does not read mail. The "
    "ERP may still show a shipment as on time even when a supplier has emailed that it "
    "slipped. Read every piece of context, especially mail, to confirm whether the "
    "flagged risk is real before deciding what to do, and cite the specific evidence "
    "(an email, a date, a quantity) in your reasoning.\n\n"
    "If one of the available_workflows fits the situation, propose it by name with the "
    "exact parameters its params_schema requires; a declared workflow's own fixed steps "
    "handle the actual response, so your job for a workflow is only to decide it applies "
    "and supply correct parameters, not to plan the steps yourself. Only propose a "
    "workflow if the attention item's own facts and the gathered context actually contain "
    "real values for every one of its required parameters — never invent, guess, or repurpose "
    "an unrelated id (such as a production order id) to fill a parameter the situation does "
    "not actually provide. A workflow's description tells you the kind of problem it "
    "handles; if this attention item is a different kind of problem, it does not apply, no "
    "matter how superficially similar the data looks. Use a free-form plan of tool calls "
    "whenever no declared workflow genuinely fits. Propose no action when nothing in the "
    "context indicates the detected risk is real. Only use facts present in the context; "
    "never invent record ids, suppliers, or values."
)


def _tools_for_user(user: User) -> list[dict]:
    """Only tools the user has every required scope for, and never a
    workflow-only tool: those can only be reached by entering their
    workflow, never proposed directly in a free-form plan.
    """

    visible = []
    for tool in all_tools():
        if tool.allowed_in is not None:
            continue
        if any(scope not in user.scopes for scope in tool.required_scopes):
            continue
        visible.append({
            "name": tool.name,
            "description": tool.description,
            "args_schema": tool.input_schema.model_json_schema(),
        })
    return visible


def build_messages(
    item: AttentionItem,
    context: dict[str, ContextSlice],
    memory_facts: list[dict],
    user: User,
) -> list[dict[str, str]]:
    payload = {
        "attention_item": {"summary": item.summary, "facts": item.facts},
        "context": {source: slice_.records for source, slice_ in context.items()},
        "memory_hints": memory_facts,
        "available_workflows": workflow_catalog_for_prompt(),
        "available_tools": _tools_for_user(user),
    }
    return [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": json.dumps(payload, default=str)},
    ]
