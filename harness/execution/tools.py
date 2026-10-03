"""The Tool contract (section 7). A Tool is metadata plus three callables:
what it costs in dollars (if anything), what must be true before it runs,
and what it does. The executor (executor.py) is the only thing that ever
calls `run`; nothing else, including the LLM, gets a handle to it.

`compensation_args` is one addition beyond the literal section 7 contract:
`compensate` names which tool undoes this one, but something has to turn
"undo this specific call" into that tool's args. Rather than hide that
logic inside the executor (which would need a special case per tool),
each tool that declares a compensation also declares how to build the
compensating call from its own (args, result).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Any, Callable, Type

from pydantic import BaseModel


class PrecheckFailed(Exception):
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class ToolContext:
    """Everything a tool's run/precheck needs besides the args and the db.
    `step` identifies which step of which run this call belongs to, so the
    default idempotency key is stable across retries and resumes.
    """

    run_id: str
    step: str
    today: date


def default_idempotency_key(args: BaseModel, ctx: ToolContext) -> str:
    return f"{ctx.run_id}:{ctx.step}"


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    input_schema: Type[BaseModel]
    required_scopes: tuple[str, ...]
    writes: bool
    run: Callable[[Any, BaseModel, ToolContext], dict]
    allowed_in: tuple[str, ...] | None = None
    # False for a tool that only ever informs someone (notify_user), never
    # changes state that resolves an open problem by itself. The gate uses
    # this to refuse a free-form plan that consists entirely of such tools.
    resolves: bool = True
    value: Callable[[BaseModel], float] | None = None
    precheck: Callable[[Any, BaseModel], None] | None = None
    idempotency_key: Callable[[BaseModel, ToolContext], str] = default_idempotency_key
    compensate: str | None = None
    compensation_args: Callable[[BaseModel, dict], BaseModel] | None = None
