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

## Phase 4: detection, context, planner, Scenario A end to end

### What was built

- `harness/detection/`: `StockoutDetector` (section 8) — projects each production order's
  component balance day by day from today to the order's start, subtracting background
  usage, adding inbound POs landing in the window, subtracting other orders' competing
  demand on the same day. Raises `short` (projected balance below requirement) or
  `thin_margin` (covered only because of a PO landing within `MARGIN_DAYS` of the start).
  `detection/registry.py` runs every registered detector and treats a UNIQUE violation on
  `dedupe_key` as "already known."
- `harness/context/`: `ErpProvider`, `MailProvider`, `CalendarProvider` (section 9), each
  scope-gating before it filters for relevance, plus `context/registry.py` which gathers
  every provider's slice and audits exactly which record ids were returned per source.
- `harness/planning/prompt.py` and `planner.py`: builds the prompt from the attention item,
  gathered context, memory hints (empty until phase 5), and catalogs built live from the
  tool and workflow registries; `propose()` does the one-retry-then-fail dance section 10
  describes.
- `harness/planning/llm.py` extended with `LLMOutputInvalid`, `OpenAIClient`, and
  `ReplayClient`.
- `harness/memory/runs.py`: run bookkeeping (`runs.state`, status transitions) — the "what
  the current run knows" half of memory; persistent `memory_facts` is phase 5.
- `harness/app.py`: `handle_attention_item` (item -> context -> proposal -> gate/workflow
  entry), `tick`, `approve`, `reject`, and `default_llm_client` (OpenAIClient if
  `OPENAI_API_KEY` is set, else `ReplayClient` against a recorded fixture, else a clear
  error). `__main__.py` gained `tick`, `approve`, `reject` CLI commands.
- The executor now re-runs detectors immediately after any tool in
  `execution.catalog.ERP_WRITING_TOOLS` writes (section 8's "also after any tool writes to
  an ERP table"), not just on tick.

### Reasoning

- **`tick()` runs every "today"-scoped step and advances the clock last, not first.**
  Section 13 lists "advance clock" as tick's first action, but `escalate_pending` (built
  and tested in phase 2) reads as "unanswered at end of the day that's ending, check if the
  approver is out tomorrow" — a check meant to run while `today` is still that day, not
  after it has already turned into tomorrow. Advancing first would mean the very first
  tick after seeding processes tomorrow's date instead of today's (breaking the demo's
  "tick on 9/2; detection..." narrative), and would shift the OOO check by one day from
  what phase 2 already tested. Advancing last makes one call to `tick()` equal to "process
  today fully, then turn the page," which is both demo-narrative-correct and leaves
  `escalate_pending` untouched.
- **Workflow names are constrained in the schema itself, not just described in prompt
  text — and this was discovered empirically, not reasoned out in advance.** Early in this
  phase I treated "the Literal of workflow names... built from the registries at runtime"
  (section 7) as being about prompt *content* (an `available_workflows` list) rather than
  a schema-level constraint, since `WorkflowRequest.workflow` was a plain `str`. Testing
  against the real OpenAI API immediately exposed why that's not enough: gpt-4o-mini,
  given only a prose list of valid workflow names, twice invented workflow names
  (`FollowUpWithSupplier`, `follow_up_with_supplier`) that were never in the catalog. The
  fix was to rebuild the planner's output schema per call via `pydantic.create_model`,
  narrowing `WorkflowRequest.workflow` to `Literal[*registered_names]` — built from
  `harness.execution.engine`'s registry at call time, subclassed from the real
  `WorkflowRequest` so `isinstance` checks elsewhere still hold. `FakeLLMClient` had to
  change too: it now re-validates a scripted response against whatever model is actually
  requested instead of checking `isinstance`, since tests can't import a model that's
  constructed fresh on every call.
- **`OpenAIClient` builds its own request with `"strict": false` instead of using the SDK's
  `.parse()` convenience wrapper** — also found by testing against the real API, not
  anticipated. OpenAI's strict structured-output mode requires every object in the schema,
  including nested ones, to declare a closed set of properties. `ToolCall.args` and
  `WorkflowRequest.params` are genuinely open dicts (different tools and workflows take
  different arguments), which strict mode rejects outright (`400: 'additionalProperties'
  is required to be supplied and to be false`). Non-strict mode accepts the schema as-is;
  Pydantic's own `extra="forbid"` validation on the parsed response is what actually
  guarantees closed objects where they should be closed, so nothing is given up by not
  using strict mode at the API layer.
- **Two real bugs surfaced by the same real-API pass, both about import-order coupling.**
  First: `app.py` never imported `harness.execution.workflows`, so nothing guaranteed
  `reroute_po` was actually registered before `tick()` ran — the planner's workflow-name
  enum would silently fall back to unconstrained in that case. Fixed by importing the
  workflows package (for its registration side effect) directly in `app.py`. Second: the
  same gap existed for tests — `test_planner.py` passed when run after
  `test_workflow_engine.py` (which happens to import the workflows package) but failed in
  isolation. Fixed by moving that import into `tests/conftest.py`, which every test file
  loads regardless of run order. Both fixes are the same lesson: a registration side effect
  from importing a submodule is not something any one caller should be trusted to trigger
  by accident; the shared entry point (`app.py` for the app, `conftest.py` for tests) has
  to own it explicitly.
- **`WorkflowRequest.kind`, `ToolPlan.kind`, and `NoAction.kind` lost their default
  values** (`Literal["workflow"] = "workflow"` became `Literal["workflow"]`), also from
  real-API testing: with a default, pydantic's JSON schema omits `kind` from `required`,
  and a real model (observing the `$defs` title "NoAction" more than the buried `const:
  "none"`) returned `{"kind": "NoAction", ...}` — the class name, not the literal value.
  Making every `kind` field required forces the model to treat it as a real enum choice.
  No existing code was affected: every construction site already passed `kind=` explicitly.
- **Real-API validation was done by hand this phase, not automated** — section 15's rule
  that tests never touch the network stands; `FakeLLMClient`/`ReplayClient` cover every
  automated test. But a `.env`-loaded key was used, outside pytest, to run the actual
  planner and the actual workflow against gpt-4o-mini end to end (prompt -> workflow
  selection -> candidate filtering -> bounded supplier choice -> bounded notification draft
  -> approval -> execution), and separately through the real CLI (`tick` / `approve`). Both
  of the bugs above were found this way; neither would have been caught by scripted fakes
  alone, since a fake, by construction, never says something the schema didn't ask for.
  The actual recorded transcript and replay fixture capture are still deferred to phase 8,
  once Scenario B and the Tuesday follow-up exist to be part of one coherent run.

---

## Phase 5: scheduler, follow-up, memory, explain

### What was built

- `harness/scheduling/tasks.py`: a generic deferred-task runner. `run_due_tasks()` finds
  every `scheduled_tasks` row with `status='pending'` and `run_at <= today`, marks it
  `fired` immediately (before dispatch), then calls a handler registered by `kind`. Marking
  before dispatch is what makes a task fire exactly once even if `run_due_tasks` runs twice
  on the same day.
- `harness/scheduling/arrival_check.py`: the one handler registered so far, for
  `kind="arrival_check"`. Sums `erp_receipts` for the PO; if it covers the ordered qty,
  audits `arrival_check.confirmed` and writes a confirmed-outcome memory fact about the
  replacement supplier. If not, audits `arrival_check.missed` and raises a brand new
  attention item — same `stockout:{part_id}:{prod_order_id}:{po_id}` dedupe-key shape a
  detector would use, just keyed on the new PO — so the next tick's planning pass picks it
  up exactly like a fresh detection.
- `harness/memory/facts.py`: `write_fact()` (subject, fact text, source record ids,
  optional expiry) and `facts_for_prompt()` (every non-expired fact, handed to the planner
  as hints).
- `harness/world/receipts.py`: `record_receipt()` — a plain data write with no scope, no
  gate, no tool wrapper, because receiving a shipment is an external-world event (the
  warehouse scanning something in), not an agent action.
- `harness/audit/explain.py`: renders the audit log into the human-readable narrative
  section 13 asks for, one event type to one line each, with an unknown-event fallback so
  a future event type never breaks `explain` — it just renders as its raw JSON detail
  until someone adds a proper line for it.
- `app.py`'s `tick()` now runs due tasks first (within the "today" phase, before the clock
  advances — see phase 4's deviation #6) and plans for every `open` attention item, not
  just the ones `run_detectors` returned that tick, so an item the arrival-check handler
  just raised gets planned in the same tick it appeared. `handle_attention_item` now
  passes real `facts_for_prompt()` output to the planner and stashes the item's facts and
  gathered mail record ids into run state, for `approve()` to turn into a confirmed-outcome
  memory fact once (and only once) the workflow actually reaches `completed`. New CLI
  commands: `receive`, `explain`.

### Reasoning

- **A real off-by-one in the stockout detector's inbound-PO window, found by trying to
  actually run the Tuesday follow-up, not reasoned out in advance.** The window was
  `today < promised_date <= scheduled_start`. Driving the clock forward to exercise the
  arrival check (which lands exactly on the replacement PO's promised date, since
  detectors re-run after any ERP-writing tool call per phase 4) meant that by the time
  `today` reached that date, the window's strict `<` excluded the PO — not yet "received"
  by an actual receipt, but also no longer "upcoming" by the window's own definition. The
  detector read this as a fresh shortfall and tried to re-plan a second reroute mid-test,
  which is wrong: on the day a PO is promised, the ERP still reads it as on time, and the
  detector is explicitly supposed to trust ERP dates at face value (that's the whole
  premise of section 8 — "the ERP does not know about the slip; the email does"). Changed
  to `today <= promised_date <= scheduled_start`. A genuine fresh risk (the date passes
  with still no receipt) still fires correctly, just starting the day after, which is the
  semantically correct day for it to start mattering.
- **Attention items gain a `status` lifecycle (`open` -> `planned`), not just created once
  and left alone.** Needed once two different things could raise an item in the same tick
  (a detector, and now the arrival-check handler): without marking an item `planned`
  immediately, `tick()` would have had to track "which items did `run_detectors` just
  return" separately from "which items exist," and would have missed the arrival-check's
  item entirely (it isn't in `run_detectors`'s return value). Marking status before
  planning runs, not after, also means an item is handed to the planner at most once
  regardless of what the planner does with it — including a planner failure — mirroring
  the same "exactly once" property `run_due_tasks` gives scheduled tasks.
- **Memory facts are written in exactly two places, both confirmed outcomes, never at
  proposal time**: when `approve()` sees a workflow reach `completed` (not when it merely
  enters `awaiting_approval` — a proposal is not yet a confirmed outcome), and when the
  arrival check confirms a receipt. Rejected, failed, and halted runs never write a fact,
  on the same reasoning. The completion-time fact needs context (the mail evidence, the
  original supplier) that the approval record alone doesn't carry, which is why
  `handle_attention_item` now stashes it into `runs.state` at planning time for `approve()`
  to read back later — run memory and persistent memory meeting at exactly the point
  section 13 says they should.

---

## Phase 6: Scenario B

### What was built

- `harness/detection/quality_hold.py`: `QualityHoldDetector` — for each allocation on a lot
  with status `hold`, raises an item if the production order it's allocated to starts within
  3 days. Owner resolves to the lot's `hold_placed_by`, falling back to role `Quality
  Manager`, the same two-tier pattern `StockoutDetector` already used.
- `harness/context/quality.py`: `QualityProvider` — the held lot, its allocations, the
  affected production order, and every released lot of the same part with free quantity
  computed. Registered into `detection/registry.py` and `context/registry.py` alongside the
  existing ones.
- `harness/execution/runner.py`: the free-form tool runner, the piece deliberately deferred
  in phase 4. Executes an approved `ToolPlan`'s steps in whatever order the plan says
  (no fixed order to enforce, unlike the workflow engine), through the same
  `executor.execute()`/`compensate()` the workflow engine uses. On a step's failure,
  backs out completed steps in reverse. Wired into `app.py`'s `approve()` as the
  `else` branch alongside the existing workflow path.
- A fix to a phase 2 tool: `flag_shortage` no longer takes a caller-supplied `owner_id`;
  it resolves "a Purchasing Manager" itself, the same role-fallback every detector uses.

### Reasoning

- **Scenario B genuinely required zero edits to `planner.py`, `prompt.py` (beyond a
  generality fix, see below), `policy/gate.py`, or `audit/`** — verified with `git diff
  --stat` against those files after the phase, not just asserted. Only `detection/registry.py`
  and `context/registry.py` changed, to add the new detector/provider to their lists — both
  explicitly anticipated by section 3's framing of what Scenario B would need (section 3:
  "quality/lot data, a new detector, a new context provider... a different user with
  different scopes"). The "different user" (Omar, u-202) needed no changes either — it was
  seeded correctly back in phase 1, anticipating this.
- **`flag_shortage`'s `owner_id` moved from a caller-supplied argument to something the
  tool resolves itself.** Found while actually wiring the free-form path, not anticipated in
  advance: a free-form planner has no prompt content that would tell it Dana's internal
  `user_id` is `"u-101"` — asking it to supply `owner_id` was asking it to invent an
  unreachable fact. Section 12's own phrasing ("creates an attention item owned by a
  Purchasing Manager") already frames ownership resolution as the tool's job, not the
  caller's, so this is a correction of a phase 2 oversight, not a Scenario-B-specific
  special case.
- **Two real prompt-quality gaps, found only by actually running Scenario B against a real
  model, not by reasoning about the prompt in advance:**
  1. Given only `reroute_po` in its catalog and a quality-hold attention item, gpt-4o-mini
     proposed entering `reroute_po` anyway, inventing `original_po_id: "4820"` by repurposing
     the production order's own id. The fix was sharpening `reroute_po`'s own description
     (in `execution/workflows/reroute_po.py`, which is already scenario-A-specific, so this
     doesn't touch the "no scenario-specific wording" constraint on `planning/`) to state
     plainly what it requires (a real, existing PO) and what it explicitly excludes (a lot
     on hold, or any fix that's reallocating stock rather than ordering more) — a one-line
     "don't propose this" clause was not enough; an explicit contrast with the
     superficially-similar wrong case was.
  2. `QualityProvider` originally exposed both a released lot's raw `qty` and its computed
     `free_qty`. The model used the raw `qty` (the lot's full size) instead of `free_qty`
     (what's actually available) when building a `reallocate_lot` call — a real mistake,
     caught cleanly by `reallocate_lot`'s own precheck with zero bad writes, but avoidable
     at the source. Fixed by dropping the raw `qty` from that record entirely: the only
     number relevant to a reallocation decision is what's free, so showing the ambiguous one
     alongside it was inviting exactly this error.
- **The free-form runner's failure-path status is `"compensated"` even when zero steps had
  completed yet** (the first step itself failed precheck). This mirrors the workflow
  engine's own `_compensate_all`, which does the same thing unconditionally — kept
  consistent across both paths rather than introducing a "failed vs. compensated" distinction
  only one of them makes.

---

## Phase 7: failure cases and test completion

### What was built

- `tests/test_requirements.py` (section 15.14): one test per assignment requirement, named
  after it — `test_a1_...` through `test_a7_...` (the seven numbered behaviors from the
  assignment's section 2), three Part 1 pluggability tests (a swapped `LLMClient`, a dummy
  detector registered alongside the real ones, a dummy provider registered alongside the
  real ones — each proving the registry pattern, not just asserting it), five Part 2 tests
  (fixed step order enforced by `WorkflowRequest` having no `steps` field at all, a bounded
  step rejecting two invalid choices in a row, every action step declaring a compensation,
  resume-after-kill, version pinning), three Part 3 tests (a static grep proving
  `planning/`, `policy/gate.py`, `policy/approvals.py`, and `audit/` never mention lots or
  quality, Omar's and Dana's scopes genuinely differing, Scenario B running through the
  literal same `propose`/`gate` functions Scenario A uses), and two permission-model tests
  (providers return nothing to a scopeless user, `execute()` refuses a write without the
  required scope).
- A few small gaps in earlier phases' tests, closed rather than left for later: the
  Scenario A `explain` test now checks for the actual generated PO id and the actual
  scheduled-check date (not just supplier/PO names), closing out every item literally
  listed in section 15.2's checklist; a new memory test plants a false fact ("S-Z is not
  approved and has a 10-day lead time") and confirms the workflow's own ERP checks still
  find the real S-Z regardless — the second half of 15.9's "a memory fact contradicting
  live ERP data does not change gate or workflow results," which previously only had the
  first half ("facts appear in the prompt as hints") under direct test.

### Reasoning

- **Most of section 16's "failure cases" list was already under test before this phase
  started** — a byproduct of writing tests alongside the code that needed them in phases 2
  through 6, rather than deferring verification to a dedicated end. No qualifying supplier,
  over-limit routing, missing-scope blocks, crash-and-resume, duplicate-detection, tamper
  rejection (on both the workflow and free-form paths), and arrival-not-received re-entry
  all had tests already. Phase 7's actual new work was narrower than the phase name
  suggests: the 15.14 module, plus closing the handful of specific sub-claims (an exact PO
  id string, a date string, memory non-interference) that broader tests had covered in
  spirit but not verified to the letter.
- **`test_part3`'s static check scans for `lot` and `quality` as whole words** (`\blot\b`,
  not a bare substring), checked against the actual files before writing the assertion —
  a bare substring match would have false-positived on ordinary English ("a lot of") in a
  docstring, which very nearly happened: two incidental matches turned up during phase 6
  (an illustrative comment in `gate.py`, a docstring in `prompt.py`), both harmless prose
  written before or without reference to Scenario B, but both reworded at the time rather
  than left for this phase to trip over.
- **The coverage report**: every one of the five 90%+ target packages — `policy/`,
  `execution/`, `detection/`, `scheduling/`, `audit/` — is at exactly 100% line coverage,
  with zero lines to justify. `context/` and `memory/` (not explicit targets) are also at
  100%. The two remaining gaps are both already-documented and intentional: `world/seed.py`
  at 96% (two unreachable defensive `AssertionError` guards on fixture-name branches,
  flagged back in phase 1) and `planning/llm.py` at 72% (the real-network branches of
  `OpenAIClient`, which section 15's own rule — "no network calls in tests" — forbids
  exercising through pytest; validated by hand against the real API instead, repeatedly,
  across phases 4 and 6).

---

## Phase 8: demo, docs, and the recorded run

### What was built

- `harness/demo.py`: the `demo` command's five sections (Scenario A + follow-up, Scenario
  B's two variants, seven failure cases, `explain`). Scenario A uses whichever `LLMClient`
  the caller passes in (real API or replay); everything else uses a hand-scripted
  `FakeLLMClient` regardless of whether a key is set, since only Scenario A has a
  recorded-run requirement and section 15's "no network in tests" rule extends in spirit
  to the demo running unattended.
- `RecordingLLMClient` in `planning/llm.py`: wraps a real client and records every parsed
  response as a plain dict, in order. Used once, by hand, to generate the actual
  deliverables — not used by the harness at runtime, not touched by any test.
- `runs/scenario_a.txt` and `runs/scenario_a_responses.json`: generated by running
  Scenario A + follow-up + `explain` against the real API once, with the console recording
  itself (Rich's `Console(record=True)` strips ANSI automatically) and the
  `RecordingLLMClient` capturing the three real model responses the run actually produced.
- `tests/test_demo.py` (section 15.15): the demo, run through `ReplayClient` against that
  same recorded fixture, asserted to complete and contain every section header and a
  cross-section set of key facts.
- `README.md`, `MODEL.md`, `DESIGN.md`.

### Reasoning

- **Only Scenario A's portion of the demo needed to be real; the rest was scripted on
  purpose.** The assignment's recorded-run requirement names Scenario A specifically
  ("a recorded run of Scenario A"); Scenario B and the failure cases exist to demonstrate
  mechanics (reallocation, compensation, tamper-rejection, escalation routing) that don't
  need live-model authenticity to be convincing, and scripting them keeps the whole demo
  deterministic and network-free except for the one piece that's supposed to showcase real
  reasoning. This also sidestepped a real structural problem: a single replay fixture
  large enough to cover all five sections' LLM calls, in the exact order every section
  would ask for them, would have been fragile to maintain against any future change to any
  section — keeping replay scoped to just Scenario A's three calls avoids that entirely.
- **Each demo section gets its own temporary SQLite file.** Scenario B and the seven
  failure cases each need a different starting fixture; keeping them on separate
  connections (all inside one `TemporaryDirectory`, cleaned up automatically) means
  Scenario A's database is never touched after its own section finishes, so `explain` at
  the very end of the demo still reads the undisturbed Scenario A story, not whatever the
  last failure case happened to leave behind.
- **The exact same `enter_workflow()`-without-`propose()` bug from phase 7 recurred while
  writing the demo's failure-case section**, in a new file, independent of the earlier fix
  — a sign the mistake is an easy one to make (it reads as "obviously this needs the
  planner's wrapper type"), not that the earlier fix was somehow incomplete. Fixed the same
  way: `enter_workflow()` takes already-decided params directly and never calls
  `propose()`, so only the workflow's own bounded-step responses belong in its script, not
  a `PlannerOutput` wrapper around them.
- **`Harness` gained an explicit `close()`**, and `demo.py` calls it on every short-lived
  harness once a section is done with it. Found by running the test suite with warnings
  enabled rather than by inspection: the demo smoke test (the first thing to exercise
  `demo.py`'s many per-section `Harness` instances end to end) surfaced `ResourceWarning:
  unclosed database` for every one of them, plus one unrelated leak in `test_gate.py`
  itself (`open()` without a context manager, in a test written back in phase 2). Neither
  was a correctness bug — SQLite and the garbage collector both tolerate it — but a clean
  `pytest -W default` run with zero warnings is worth having, and an explicit `close()` is
  one line per section.

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
6. **`tick()` advances the clock last, not first**, contrary to section 13's literal
   ordering (phase 4) — keeps `escalate_pending`'s already-tested "OOO tomorrow" semantics
   correct and matches the demo narrative's "tick on 9/2; detection..." framing.
7. **The planner's output schema narrows `WorkflowRequest.workflow` to a `Literal` of
   currently-registered names, rebuilt per call**, rather than relying on prompt text alone
   (phase 4) — a real model otherwise invents workflow names outside the catalog.
8. **`OpenAIClient` requests structured output with `"strict": false`**, built by hand
   rather than via the SDK's `.parse()` helper (phase 4) — strict mode cannot represent
   `ToolCall.args` / `WorkflowRequest.params`'s intentionally open dicts.
9. **`ToolPlan.kind`, `WorkflowRequest.kind`, `NoAction.kind` are required fields with no
   default** (changed from phase 2's `= "plan"` etc.) (phase 4) — an optional discriminator
   field let a real model substitute the class name for the literal value.
10. **The stockout detector's inbound-PO window is `today <= promised_date <=
    scheduled_start`** (changed from a strict `today <`) (phase 5) — on the day a PO is
    promised the ERP still reads it as on time; excluding it at exactly that boundary
    caused a spurious fresh shortfall the moment the clock reached it with no receipt yet
    recorded.
11. **`attention_items` gained a status transition (`open` -> `planned`)**, not specified
    in the schema's original field list (phase 5) — needed once both a detector and the
    arrival-check handler could raise an item in the same tick; `tick()` plans every `open`
    item rather than only the ones its own `run_detectors` call returned.
12. **`flag_shortage` resolves its own `owner_id`** instead of taking it as a caller-supplied
    argument (phase 6) — a free-form planner has no way to know an internal `user_id` for
    "the Purchasing Manager" from context alone.
13. **`reroute_po`'s workflow description explicitly excludes quality-hold-shaped
    problems**, not just positively describing when it applies (phase 6) — a real model
    otherwise force-fits a superficially-similar attention item into the one workflow it's
    shown, inventing a parameter value to make it fit.
14. **`QualityProvider` exposes only a released lot's `free_qty`, not its raw total `qty`**
    (phase 6) — showing both invited a real model to reallocate against the wrong number.
15. **The demo's Scenario B and failure-case sections always use a hand-scripted
    `FakeLLMClient`, regardless of whether a real API key is set** (phase 8) — only
    Scenario A has a recorded-run requirement; scripting the rest keeps a single replay
    fixture small and keeps the whole demo deterministic end to end.
