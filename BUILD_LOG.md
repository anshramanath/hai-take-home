# Build log

This is a running engineering record of what has been built, in what order, and why. It
is not one of the four required deliverables (`README.md`, `MODEL.md`, `DESIGN.md`, the
recorded run) — it exists so the reasoning behind each decision survives independently of
chat history, and so writing those four docs in the final phase is a matter of
summarizing this record rather than reconstructing it from scratch.

Each phase section covers: what was built, why it was built that way, and any deviation
from `CLAUDE.md` with the reasoning behind it. Deviations are also collected in one list
at the end of this file.

---

## Phase 1: skeleton (world, clock, CLI)

### What was built

- `harness/world/schema.sql`: every table from the spec's data model (section 5) in one
  SQLite file, fake-company tables and harness-state tables together. `audit_log` gets two
  `RAISE(ABORT, ...)` triggers, on `UPDATE` and `DELETE`, so append-only is enforced by
  SQLite itself, not by application code remembering not to call those statements.
- `harness/world/seed.py`: `seed(conn, fixture)` across seven named fixtures:
  - `scenario_a` plus four failure variants (`scenario_a_no_supplier`,
    `scenario_a_over_limit`, `scenario_a_backup_low_limit`, `scenario_a_no_arrival`), each a
    small mutation applied after the shared base data is inserted.
  - `scenario_b_covers` / `scenario_b_shortage`, differing only in whether the spare lot
    quantity is enough to cover the held lot's allocation.
  - All five users (with real scopes and `approval_limits`) are seeded for every fixture,
    not just the ones relevant to that scenario, so a user from one scenario can still be
    looked up from the other (needed once Scenario B's quality manager coexists with
    Scenario A's purchasing users in the same demo run).
- `harness/scheduling/clock.py`: `today()` / `advance(n)` / `set(date)`, reading only from
  the `clock` table. No file anywhere calls `datetime.now()`, `date.today()`, or
  `time.time()` — a grep-based test (`test_no_wall_clock_calls_outside_clock_module`)
  enforces this as a standing invariant, not just a one-time check.
- `harness/app.py` / `harness/__main__.py`: a thin `Harness` object wrapping the SQLite
  connection and the clock, and a Typer CLI with `reset --fixture <name>` and `status`.
- `tests/conftest.py`: a `make_harness` factory fixture. Every call gets its own fresh temp
  SQLite file, seeded and clocked, so tests never share state (an explicit rule in section
  15 of the spec).

### Reasoning

- **Seed data is designed to be adversarial, not just illustrative.** Scenario A has two
  trap suppliers (S-Q: cheaper but not approved for the part; S-W: approved but too slow)
  and five noise mail messages, one of which (M-005) is addressed to Marcus, not Dana, and
  exists specifically to prove mailbox scoping once providers exist in phase 4. None of
  this is needed to make the happy path work; all of it exists to make the later tests
  mean something.
- **The four `scenario_a_*` failure variants are mutations, not separate fixtures built
  from scratch**, so they can never drift from the base scenario in some unrelated way —
  the only difference between `scenario_a` and `scenario_a_no_supplier` is one UPDATE
  statement on `erp_suppliers`.
- **`scenario_a_no_arrival` is byte-identical to `scenario_a`.** There is no seed-level
  difference between "the shipment arrives" and "it doesn't" — that distinction is
  entirely about whether a later test calls the not-yet-built receipt-recording helper, not
  about different starting data.

---

## Phase 2: audit, tool catalog, gate, approvals

### What was built

- `harness/audit/log.py`: `log()` inserts one row (actor, event, JSON detail, clock-driven
  `ts`) without committing — callers that are also writing business data commit once, so
  the audit entry and the write it describes land together or not at all.
- `harness/planning/models.py`, pulled forward from phase 4: `ToolCall`, `ToolPlan`,
  `WorkflowRequest`, `NoAction`, all `extra="forbid"`. The gate needed a concrete shape to
  evaluate before the rest of the planner (prompt building, LLM clients) existed, and a
  `ToolPlan` is exactly what both a free-form proposal and a frozen workflow plan reduce
  to by the time anything needs approval.
- `harness/execution/tools.py` / `args.py` / `catalog.py`: the `Tool` contract (section 7)
  and all 11 registered tools — `create_po`, `cancel_po`, `reduce_po`, `restore_po`,
  `notify_user`, `send_correction`, `schedule_check`, `cancel_task`, `reallocate_lot`,
  `flag_shortage`, `withdraw_flag` — each with a Pydantic schema, required scopes, a
  precheck, and a declared compensation.
- `harness/execution/executor.py`: the single place `Tool.run` is ever called. One SQLite
  transaction covers the idempotency check, a live scope re-check, the precheck, the
  write, and the `executed_actions` bookkeeping insert; any exception rolls all of it back
  and still leaves an audit entry describing what happened.
- `harness/policy/gate.py`: `gate()` is pure — no DB writes, no LLM import — over a flat
  list of `ToolCall`s: unknown tool, bad args, workflow-only tool used free-form, missing
  scopes, then a walk up `manager_id` for the first approver whose `po_create_max` covers
  the plan's total value.
- `harness/policy/approvals.py`: `create_approval` (canonical JSON, SHA-256 hash),
  `decide` (only the current approver, one decision, ever), `verify_plan_hash`, and
  `escalate_pending` (the end-of-day OOO-to-backup rule, itself walking the backup's own
  manager chain if the backup's limit is too low).

### Reasoning

- **`Tool.compensation_args` is an addition beyond the literal section 7 contract.**
  `compensate: str | None` names which tool undoes this one, but something has to turn
  "undo this specific call" into that tool's actual args. Rather than special-case that
  per tool inside the executor, each tool that declares a compensation also declares, as a
  small closure, how to build the compensating call from its own `(args, result)`.
- **`reallocate_lot`'s args are `remove: list[...]` / `add: list[...]`, not the appendix's
  single `from_lot: str`.** The tool's own compensation is "reallocate back," and with a
  single source lot there is no way to express the reverse of a split (two lots back into
  one) through the same schema. With symmetric lists, compensation is just "call the same
  tool with the two lists swapped" — no separate compensation tool needed, and forward
  usage still only ever populates `remove` with one entry.
- **Compensations skip the original tool's precheck** (`execute(..., skip_precheck=True)`
  from `compensate()`). This was discovered to be *necessary*, not stylistic: reversing a
  reallocation means re-allocating demand back onto a lot that is on hold, which
  `reallocate_lot`'s own "target must be released" precheck would otherwise reject,
  making the tool permanently unable to undo itself. Scope checks and idempotency still
  apply to compensations; only the forward-looking business-rule precheck is skipped, on
  the reasoning that undoing a previously-approved action is cleanup, not a new business
  decision subject to the same gate.
- **`reduce_po`'s `new_qty < 0` is rejected by the Pydantic schema (`Field(ge=0)`), not by
  precheck**, even though the spec's table lists the whole "`>= 0` and `< current`" rule
  under precheck. The half that needs a DB read (`< current qty`) stays in precheck; the
  half that doesn't is enforced at the cheapest possible layer.
- **Gate decisions are not yet audited.** `gate()` stays pure by design, and nothing calls
  it as part of a real end-to-end run yet (that orchestration arrives with the planner in
  phase 4, and partially in phase 3's `enter_workflow`). `gate.allowed` / `gate.blocked`
  audit events exist starting in phase 3, logged by the caller, not by `gate()` itself.

---

## Phase 3: workflow engine

### What was built

- `harness/planning/llm.py`, pulled forward from phase 4: the `LLMClient` protocol and
  `FakeLLMClient` (scripted responses, raises loudly if exhausted or if a scripted
  response doesn't match the requested model). The real `OpenAIClient` and `ReplayClient`,
  which need actual prompt text and API wiring, are still phase 4's job — but the
  workflow's two bounded LLM steps need *some* client interface to exist now.
- `harness/execution/engine.py`: the declarative engine. `WorkflowDefinition` and `Step`
  match the section 7 contract. The engine runs consecutive `check`/`llm` steps without
  stopping and halts at the first `action` step until `state["_approved"]` is set — this
  is a structural rule (`step.kind == "action" and not state.get("_approved")`), not
  per-workflow logic, so "stop here for approval" never needs to be hardcoded per
  definition. `enter_workflow()` (start, run pre-approval steps, gate, create approval),
  `resume_after_approval()` (verify approved and hash-matched, then run the action steps),
  `resume_all()` (pick up every instance a killed process left at `status='running'`).
  Failure during an action step walks the `_compensation_log` in reverse through the same
  `executor.compensate()` from phase 2.
- `harness/execution/workflows/reroute_po.py`: the concrete 8-step definition from section
  12 — two code checks (supplier approved for the part; lead time meets the need date),
  two bounded LLM steps (choose from the pre-filtered candidates with a justification;
  draft notification text only), then the four approved actions.

### Reasoning

- **Action steps never recompute anything; they read their args back from the approval's
  own frozen `plan_json`.** This was a deliberate design turn partway through the phase:
  the first draft had each action step's `fn` recompute its own args (unit price, promised
  date, etc.) fresh at execution time, using the same helper the approval-building step
  used. That meant the hash check in `resume_after_approval` was checking that the
  approval record hadn't been tampered with, but not actually *using* the approved values
  for anything — if the clock had advanced between approval and execution, a freshly
  recomputed `promised_date` could silently differ from what was approved. Storing
  `_approved_plan_steps` in state (parsed once from the approval's verified `plan_json`)
  and having every action step read from it via `approved_args()` makes "zero computation
  between approval and execution" (invariant 3) actually true for every field, not just
  the LLM-derived ones.
- **This forced `CreatePoArgs` to gain a required `po_id` field** (a phase 2 file,
  revisited in phase 3). The new PO's id has to be part of the frozen, approved plan
  because the notification text and the arrival-check's payload both need to reference it,
  and by the rule above, nothing may be computed after approval — including an id. `po_id`
  is generated once, in `build_plan_steps()`, before the approval exists; `create_po`'s
  `run()` just uses what it's given rather than generating its own.
- **`DraftNotificationResponse` has exactly one field: `body`.** Section 12 says the
  recipient and the facts are "filled by code" — rather than giving the model fields for
  those and then validating/overriding them, there is structurally no field for the model
  to set them with in the first place. A test confirms a drafted body that tries to plant a
  wrong PO number in its free text still can't change who gets notified or what the
  code-appended facts say.
- **A supplier choice that's invalid twice in a row maps to workflow status `failed`, not
  `halted_no_supplier`.** `halted_no_supplier` is reserved for "no candidate survived the
  code checks" (steps 1-2) — a different root cause than "candidates existed but the model
  couldn't settle on a valid one," which is a model/process failure, not a business
  condition.
- **One piece of dead code was written and then deleted in the same phase**: an early
  draft of `enter_workflow` guarded against being called twice on an instance that already
  had `_approval_id` set. Since `enter_workflow` always creates a brand new instance via
  `start()`, that branch could never actually be reached through the public API — it was
  removed rather than kept "just in case," per the standing instruction not to write
  defensive code for scenarios that can't happen.

---

## Deviations from `CLAUDE.md`, collected

None of these touch section 2 (invariants) or section 3 (locked decisions) — they're
implementation-level choices the spec didn't pin down, flagged here rather than made
silently:

1. **`reallocate_lot` args generalized** from the appendix's `from_lot: str,
   allocations: [...]` to symmetric `remove: list[...]` / `add: list[...]`, so the tool
   can compensate for itself by swapping the two lists (phase 2).
2. **Compensation calls skip the original tool's precheck** (phase 2) — necessary for
   `reallocate_lot` to be able to undo an allocation back onto a held lot.
3. **`reduce_po`'s `new_qty < 0` enforced by the Pydantic schema, not precheck** (phase 2)
   — only the DB-dependent half of that rule needs to live in precheck.
4. **`CreatePoArgs` requires a caller-supplied `po_id`** rather than generating one inside
   `run()` (introduced in phase 3, applied retroactively to the phase 2 tool). Required so
   the workflow's frozen, approved plan can reference the new PO's id before it exists.
5. **Action steps inside a workflow read their args from the approved plan, never
   recompute them** (phase 3) — makes the approval hash check load-bearing rather than
   cosmetic.
