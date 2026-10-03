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

## Phase 9: post-build review (FIXES.md, three tiers)

### What prompted it

After phase 8, the four deliverables were fed to a separate review process (Claude, fed
`BUILD_LOG.md` and the three docs), which produced `FIXES.md` — a list of suspected bugs
and unverified claims. That first version carried its own "Decisions" framework (D1-D7)
that would have changed tested behavior (approval authority, a new approval-expiry
status) without being asked. A leaner, revised `FIXES (1).md` replaced it: real bugs and
unmet requirements only (Tier 1), tests proving existing claims (Tier 2), and a docs pass
(Tier 3), with an explicit rule — no new behavior, and stop and ask rather than build one
in if a test seems to need it.

### Tier 1: fixes

Each item was verified against the actual code before any fix, per the lean doc's own
rule ("some items may already be handled"). Five were real:

- **F1.** Workflow params (`original_po_id`, `prod_order_id`) are now validated against
  live ERP data at step 1, not just described in the workflow's prompt text — a
  `_validate_params` check added to `reroute_po.py`, halting with `halted_invalid_params`
  before any LLM call. This is the code-level version of phase 6's prompt-wording fix for
  the same real failure (a model repurposing a production order id as a PO id).
- **F2.** `qty` is now bounded by the original PO's open quantity, checked before
  approval (previously only `qty > 0` was enforced, by the params schema). Fixed
  `scenario_a_over_limit`, which asked for a 700-unit reroute from a PO that only had 400
  open; raised the seed PO's own quantity to 800 so the variant is internally consistent.
  `unit_price` was already code-sourced; no fix needed there, just a confirming test.
- **F3.** `promised_date` moved from a value frozen into the plan at approval time to a
  fact `create_po` computes at execution (today plus the chosen supplier's lead time),
  refusing the write if it misses `needed_by`. Collected in deviation 5 below, since it
  refines that deviation's own claim.
- **F4.** Memory facts gained `visible_to_scope`, and `facts_for_prompt()` now takes the
  requesting user and filters by it, matching the permission model every provider already
  enforces.
- **F5.** A process killed between `decide()` recording an approval and the execution that
  follows it previously stranded that approval forever (`resume_all` only ever picked up
  `running` workflow instances, never an approved-but-unexecuted free-form plan or a
  workflow instance still sitting at `awaiting_approval`). Added
  `resume_pending_executions()`, wired into `tick()`, covering both paths.

Two more were found to already be correct, needing only a confirming test: F6 (MODEL.md's
claim about supplier S-N was wrong, not the code: `ErpProvider` already excludes it for an
unrelated part) and F8 (fresh `run_id`/`instance_id` uuids on every re-entry already make
idempotency keys collide-proof, by construction).

F7, as the review document stated it, was wrong on the merits: it asked the demo to tick
forward to literally reach 2026-09-08 regardless of when the replacement PO's own ETA
landed. "Tuesday" in the assignment's worked example is that example's own stand-in for
"whenever the replacement arrives," not an independent calendar target this harness's own
numbers need to hit; padding the demo out to a fixed date it has no other reason to reach
would read as having missed that point, not honored it. Confirmed with the person who
wrote `CLAUDE.md` before writing any code for it. The demo stops once the arrival check
fires at the replacement's own promised date, same as before this review started.

### Tier 2: tests that prove existing claims

Eleven items (T1-T11), each either confirming an existing behavior with a new test or
finding a small gap. Nine were already true: zero LLM calls after approval on both
paths, every changed row traceable to an audited action, hash canonicalization across
dict-key order and numeric-literal form, four static architecture boundaries (`explain`
reads only `audit_log`; `policy/`/`execution/` never reference memory; `approvals.py`
doesn't import `context/`; every schema table is used outside `world/`), the
execution-time re-check catching a supplier revoked after approval, Scenario B's exact
scoping (the noise lot-tracked part never surfaces anywhere; the covers split leaves the
other lot's other allocation untouched), mail bodies never reaching `audit_log`, and every
test name the docs cite actually existing.

Two were real, both small:

- **T5.** `approval.escalated`'s audit detail had a prose reason but not the specific
  calendar event id it was based on. Added `ooo_event_id` to the detail.
- **T7.** Found while testing "crash during compensation": `executor.compensate()` logged
  `action.compensated` even when the underlying write was skipped as already-idempotent
  (a retried compensation loop, after a crash mid-loop, replays entries that already
  succeeded). The data itself was never written twice, idempotency already prevented
  that, but the audit log claimed two reversals when only one happened. Fixed by checking
  `executed_actions` before logging, not just before writing.

T8 needed a new seed fixture, `scenario_a_prompt_injection`: one email, from the real
relevant supplier contact so `MailProvider`'s relevance rule legitimately surfaces it,
whose body tries to steer the agent directly. The point isn't proving a real model resists
it (a scripted test can't prove that); it's proving that even a proposal matching the
injected ask exactly (the same quantity, the same unapproved supplier) still gets rejected
by the same code-level checks that would reject any other bad proposal.

### Tier 3: docs

All four docs rewritten for accuracy against the code as it now stands after Tiers 1-2,
and for prose style (`CLAUDE.md`'s own "no em dashes or arrow characters" rule, which the
original phase 8 drafts of `README.md`, `MODEL.md`, and `DESIGN.md` had not actually
followed, found only when this review went looking). Two real `->` characters were also
found and fixed in actual CLI/`explain` output (`harness/demo.py`, `harness/audit/explain.py`),
not just doc prose.

Deviation 5 (below) was updated in place for F3, rather than added as a new entry, since
it refines the same claim rather than contradicting it. `README.md` gained a "Known
limitations" section, separate from "What I cut": things the current build deliberately
doesn't handle (escalation checking only "tomorrow," no approval expiry, rejection not
notifying production, and others), each with one line of reasoning and one line of what
I'd do next. These are the ideas from the original review that would have needed new
behavior to fix; per the lean review doc's own rule, they're documented, not built.

---

## Phase 10: Scenario B against the real API

### What prompted it

Asked directly: does everything actually work against a real model, not just
`FakeLLMClient`? `demo.py` always scripts Scenario B and the seven failure cases with a
hand-scripted fake client by design (only Scenario A has a recorded-run requirement), so
Scenario B's free-form path had only ever been exercised against a model that cannot
surprise anyone. Driving it against the real API directly (`gpt-4o-mini`, the same model
the rest of the build uses) surfaced three real bugs, none of them visible through
`demo.py` or through any test using `FakeLLMClient`, since a fake client only ever returns
exactly what it's told to.

### What was found and fixed

- **A real model still force-fit `reroute_po` onto a quality-hold item**, despite
  `reroute_po`'s own description explicitly excluding that case (the phase 6 fix). Prompt
  wording reduced this but did not eliminate it. Fixed structurally: `WorkflowDefinition`
  gained `applies_to_detectors`, declaring which detector names raise the kind of item a
  workflow can resolve (`reroute_po` declares `("stockout", "arrival_check")`, the second
  because a missed-arrival re-entry item carries that detector name, not `"stockout"`, and
  still needs to be able to propose a second reroute). `workflow_catalog_for_prompt()` and
  the planner's schema-narrowing both filter by it now, so an inapplicable workflow is
  neither shown in the prompt nor nameable at the schema level, regardless of what the
  model's reasoning concludes. The filtering logic itself names no detector or workflow,
  only reads a field each workflow declares about itself, so invariant 10 still holds.
- **A real model proposed `schedule_check` in a free-form plan with `created_by_run`
  missing.** That field has to be the run's own id, which is never part of any context a
  free-form plan is shown, so a model acting in good faith has no way to supply it
  correctly. Fixed by restricting `schedule_check` and its compensation `cancel_task` to
  `reroute_po`, the same `allowed_in` mechanism the PO tools already use. Nothing in
  Scenario B's own design ever needed a scheduled check; this was an available-by-default
  oversight from phase 2, not a Scenario-B-specific gap.
- **A real model substituted a proposal variant's own schema title for its `kind` value**:
  `{"kind": "NoAction", ...}` instead of the required `{"kind": "none", ...}`, confirmed by
  inspecting the generated schema directly, the `kind` field's `const` constraint is
  correct, but it isn't enforced by the API under non-strict structured output (required
  elsewhere by `ToolCall.args` / `WorkflowRequest.params`'s open dicts), so a model
  confusing the two isn't caught before the harness sees it. This is the same root cause
  phase 4's "required field, no default" fix addressed, just not the full extent of it: a
  required field forces the model to write something, not to write the *correct* literal.
  Fixed by giving `ToolPlan`, `WorkflowRequest`, and `NoAction` each a `model_config.title`
  equal to their own `kind` value, so the schema's title and the correct answer are
  identical no matter which the model copies.
- **A real model has both written `summary` instead of `summary_for_user`, and, separately,
  dropped the field entirely**, on both an original attempt and its one retry. The first
  fix tried here was a `validation_alias` accepting either name; a cleaner fix followed
  once it was clear `summary_for_user` is never actually read anywhere once written (it
  lands in the audit log's record of the proposal and nowhere else, `explain` renders
  `reasoning`, not this). Rather than widen what's accepted for one specific field name,
  `ToolPlan` and `WorkflowRequest` now default it in a `model_validator(mode="before")`:
  to `summary`, if that's what the model wrote, else to `reasoning` itself, which is
  already a short, grounded justification in this build's own examples. The schema shown
  to the model is unchanged either way, `model_json_schema()` always asks for the real
  name as required, never an alias, never optional; this only changes what happens when a
  model doesn't comply. The line this draws is deliberate: fields the gate, the workflow
  engine, or execution actually consume (`kind`, `steps`, `reasoning`, `workflow`,
  `params`) stay strictly required with no such fallback, only the one field nothing
  downstream reads gets this treatment.
- **The retry message was a relayed stack trace, not an instruction.** On the observed
  `summary_for_user` omission, the retry fired with `f"Your previous output was invalid:
  {exc}."`, where `{exc}` is the raw, stringified Pydantic `ValidationError` (field paths,
  a dead-looking `For further information visit <url>` line) and the model dropped the
  same field again. `OpenAIClient` raises `LLMOutputInvalid(...) from exc`, so the
  original `ValidationError` is reachable as `__cause__`; the retry message now names
  exactly which field(s) were missing or wrong in plain, imperative language instead of
  relaying the exception, falling back to the plain message only when the cause isn't a
  structured validation error (a scripted test exception, a refusal, a network error).
- **A system-prompt line asking the model to be careful about completeness made things
  worse, not better, and was reverted.** Tried: "Your response must be a single complete
  object with every field the schema marks as required present, spelled exactly as the
  schema names it, with no fields omitted and no extra fields added." Measured against the
  real API before and after: 19/20 successful runs before this line, 0/20 after, every
  single one choosing `NoAction` (the simplest, lowest-field-count option) instead of
  attempting a `ToolPlan` for the quality-hold item it had been completing correctly
  moments before. Emphasizing strict completeness seems to have made the model treat the
  more complex proposal shapes as too risky to attempt rather than more careful about
  attempting them. Removed entirely; the lesson is kept here rather than in the prompt,
  since an instruction can fail silently in exactly the direction that looks safest
  (fewer writes, not wrong ones) and would not have been caught by anything in the test
  suite, only by measuring real-API behavior before and after the change.

### What's still a known, accepted residual

After the `summary_for_user` default and the sharper retry message (and reverting the
system-prompt line above), Scenario B completed cleanly on 55 of 56 runs in the batches
measured while those fixes were going in, up from roughly 1 in 3 before any of this
phase's fixes. The one failure in that batch was not captured in enough detail to
attribute to a specific cause (a logging gap in the throwaway check script, not the
harness itself). Once the fixes were final, two further batches against the real API (12
runs, then 25 more, 37 consecutive runs total) came back 100% clean, with full output
capture on any failure this time so it would not go unexplained again; none occurred. The
retry-then-fail design in section 10 still exists as the backstop for whatever this
doesn't cover, some real model, on some future occasion, dropping a field this build still
treats as strictly required (`kind`, `steps`, `reasoning`, `workflow`, `params`), and that
is treated as accepted residual model unreliability, not a gap to keep chasing: `CLAUDE.md`
names exactly this policy, one retry, then fail and report.

### Phase 11: a correctness gap in Scenario B, found by driving the shortage fixture hard

Prompted by wanting Scenario B at "works 100%" confidence, not just "the mechanics are
right." A thorough real-API sweep (both variants, 10+ runs each) surfaced two distinct
free-form reasoning failures that no `FakeLLMClient` test could have caught, since fakes
only return what they're scripted to return.

**First: a partial reallocation with no shortage flag.** On the shortage fixture (90
units free, 100 needed), a real model would sometimes propose `reallocate_lot` moving
only the 90 available, with no `flag_shortage` alongside it -- 6 of 10 runs in one batch.
Nothing in `reallocate_lot`'s precheck validated that the moved quantity matched what was
removed; the order would end up silently under-allocated, 90/100, with nothing tracking
the gap. Fixed two ways: `reallocate_lot`'s precheck now rejects `sum(add) != sum(remove)`
outright (a reallocation either fully covers what it takes off the source lot(s) or it
shouldn't run at all), and `QualityProvider` now computes a `coverage_check` fact
(`required_qty`, `total_free_qty_available`, `shortfall`) instead of leaving the model to
add up several lots' free quantities itself -- the same pattern section 8's own
`required_qty` already uses for the stockout detector. Re-verified: 8 then 12 further
real-API runs, zero silent under-coverage.

**Second, found while investigating the first: a model that reasons correctly and still
acts wrong.** Inspecting a run where the model chose only `notify_user` on the shortage
fixture, its own `reasoning` field correctly computed the 10-unit shortfall using the new
`coverage_check` fact -- the arithmetic was never the problem. It then proposed a
notification describing the shortfall instead of calling `flag_shortage`, the tool whose
entire purpose is routing an unresolved shortfall to someone who can act on it. Across a
12-run batch, 6 of 10 shortage runs where no lot combination covered the need chose
`notify_user` alone, correctly-reasoned-shortfall and all, with no further resolving step.
`flag_shortage`'s description was the likely cause: `"Flag a part shortage to
purchasing."`, with nothing distinguishing it from `notify_user`'s equally thin
`"Send an internal notification to a user."`. Rewrote both (`execution/catalog.py`) to
state the real, generic difference -- one tracked and owned, one not -- without naming any
scenario. Measured: 4/10 correct fix actions became 10/12.

Considered, and explicitly rejected after checking: touching the dedupe mechanism so an
unresolved item could re-surface automatically. `attention_items.dedupe_key` is a bare
`UNIQUE` constraint with no regard for whether the original item was ever actually
resolved -- confirmed directly (a deliberately inadequate `notify_user`-only plan,
approved, "completes," and a second `tick()` on the still-unresolved condition logs
`detection.duplicate_ignored` forever after). Real, but a different kind of gap than the
first two: the dedupe design is intentional (it exists so a problem still being worked on
isn't re-alerted every tick), and building re-entry machinery for "a free-form resolution
that didn't actually resolve anything" is new scope, not a bug fix, so it was left alone.
The two tool-catalog fixes above target the actual cause instead: stop the inadequate
resolution from completing in the first place.

**Still 2/12 choosing `notify_user` alone after the description fix.** Rather than accept
that residual, `Tool` gained a `resolves: bool` field (`False` for `notify_user` and
`send_correction`, the two tools that only ever inform, never themselves address an open
problem), and `gate()` gained one rule: a free-form plan whose every step has
`resolves=False` is `Blocked`, with a new `retryable: bool` field on `Blocked` set `True`
for this one reason. `handle_attention_item` (`app.py`) now loops at most twice -- the
original proposal, and, only on a retryable rejection, one re-plan with the rejection
reason appended as an extra message (`propose()` gained an optional `retry_note`
parameter for this, a different failure class from its existing invalid-output retry).
Every other rejection reason (missing scope, value over everyone's limit, unknown tool)
still reports and stops exactly as before -- re-planning only fires for the one rejection
a fresh proposal could plausibly fix. This is the "optional, max 1 retry, logged"
re-planning-after-gate-rejection the locked decisions table always allowed; the original
build had taken the other allowed branch (report and stop), and this phase is the first
place a real, measured failure mode justified building the other one.

The first retry wording tried offered the model "or propose NoAction if nothing should be
done" as an out. Measured against the real API: this made things worse, not better --
having already judged the item needed action by proposing a plan for it, the model would
sometimes use the offered exit to give up (`NoAction`) rather than find the right tool,
more often than the original notify-only problem occurred (5 of 12 runs ended with no fix
at all, most of them *after* a correctly-triggered retry). Same shape as the Phase 10
system-prompt regression: a plausible-sounding addition, measured, and reverted. Removing
the `NoAction` suggestion and instead stating plainly that the model had already judged
action was needed, so the retry must include a real resolving step, took a 12-run batch
from 5 unresolved to zero. `test_a_retryable_gate_rejection_is_reproposed_once_and_can_succeed`
and `test_a_retryable_gate_rejection_fails_after_exactly_one_retry_if_still_inadequate`
(`test_app.py`) cover the mechanism with `FakeLLMClient`; the numbers above are from the
real API, which is the only thing that could have caught either finding.

**One more gap, found as a side effect of touching this code:** `handle_attention_item`'s
free-form path never logged `gate.allowed` or `gate.blocked` to the audit log at all --
only the declared-workflow path's execution-time re-check did (`engine.py`). Section 13
names both as required event types, and no existing test caught it because the gate
function itself is correctly tested in isolation (`test_gate.py`) and nothing end-to-end
asserted the *caller* audited its result. Fixed in the same change, since the retry
feature needed this logging anyway to be auditable; `test_gate_allowed_is_audited_for_a_
straightforward_free_form_plan` closes the gap.

All five target packages remain at 100% line coverage; 264 tests pass (5 more than before
this phase).

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
   cosmetic. **Refined in the post-build review (Tier 1, F3):** this is "args are frozen,"
   not "nothing may be computed." The frozen plan carries every value a human actually
   approved (supplier, qty, price, `needed_by`); it does not carry `promised_date`,
   because nobody approves a specific promised date, they approve a need-by deadline and
   a supplier's lead time. `create_po` now computes `promised_date` itself, at execution,
   from execution-time "today" plus the chosen supplier's lead time, and its own run()
   refuses the write if that lands after `needed_by`. This matters because approval can be
   delayed (escalation): a plan approved on 9/4 whose lead time is measured from 9/2 (when
   steps 1-4 happened to run) would promise a date that was never actually achievable.
   Downstream steps that need the real date (the notification body, `schedule_check`'s
   `run_at`) carry a placeholder token in the frozen plan instead of a value nobody could
   have approved ahead of time, substituted via a new `overrides` parameter on
   `run_approved_action()` once `create_po`'s own result surfaces it into state. Still zero
   LLM calls after approval, still nothing computed that a human didn't actually approve;
   only a system fact a real supplier system would only return at order-placement time
   moved to where it is actually knowable.
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
16. **A new workflow instance status, `halted_invalid_params`** (phase 9, F1), beyond
    section 12's listed `running, awaiting_approval, completed, halted_no_supplier,
    failed, compensated`. Raised by `reroute_po`'s new step-1 validation when
    `original_po_id` or `prod_order_id` doesn't check out against live ERP data (wrong
    part, not open, doesn't consume the part, or a quantity beyond what the original PO
    has left to give). Kept distinct from `halted_no_supplier`, which is reserved for "no
    candidate survived the supplier/lead-time checks," a different root cause than "the
    params describing the problem don't match the ERP at all."
17. **Re-planning after a gate rejection is implemented for exactly one rejection reason,
    not generally** (section 3 lists it as optional: "max 1 retry, logged"). Originally
    the build took the "skipped" branch the same locked decision allows (report and
    stop) for every rejection. Phase 11 implemented the other branch, narrowly: `Blocked`
    gained a `retryable` field, `True` only when the gate's new all-non-resolving-tools
    rule fires (see the deviation below); every other rejection (missing scope, value over
    everyone's limit, unknown tool, invalid args) still reports and stops exactly as
    before, since re-planning can't fix a fact about the world, only a reasoning mistake
    about which tool to use. `handle_attention_item` loops at most twice, logs `gate.
    blocked` either way, and only re-calls `propose()` with a corrective message on a
    retryable rejection.
18. **No tool gets a bespoke audit event name; every write funnels through the generic
    `action.executed`** (phase 2, noticed as a gap against section 13's literal event list
    in this review). Section 13 names `schedule.created` alongside `schedule.fired` as
    required event types; `schedule_check` only ever produces `action.executed` (detail:
    `tool=schedule_check`, the created `task_id` and `run_at`), the same as every other
    write tool, never a distinct `schedule.created`. Deliberate, not missed: `create_po`
    has no `po.created` either, and introducing one bespoke event name would mean
    introducing a second one for every other tool to stay consistent. `schedule.fired` is
    the one genuinely different case: the scheduler noticing a task is due and dispatching
    it is not a tool write going through `executor.execute()` at all, so it has no
    `action.executed` counterpart to begin with and needs its own name.
19. **`WorkflowDefinition` gained `applies_to_detectors`** (phase 10) — not in section 7's
    contract. A real model proposed `reroute_po` for a quality-hold item despite its
    description explicitly excluding that case; prompt wording reduced this but did not
    eliminate it. The planner now filters both the prompt's `available_workflows` and the
    schema-level `Literal` of nameable workflow values by this field, so an inapplicable
    workflow is never shown and can never be named, a structural fix rather than a wording
    one. The filtering code itself names no specific detector or workflow (invariant 10).
20. **`schedule_check` and `cancel_task` became workflow-only** (phase 10), restricted to
    `reroute_po` the same way the four PO tools already are. `schedule_check`'s
    `created_by_run` must be the run's own id, which is never part of any context a
    free-form plan is shown; a real model proposed it anyway with that field missing. An
    available-by-default oversight from phase 2's original tool catalog, not something
    Scenario B's own design ever needed.
21. **Every `Proposal` variant's `model_config.title` is set to its own `kind` value**
    (phase 10), rather than left as the Python class name. Non-strict structured output
    (required elsewhere by `ToolCall.args` / `WorkflowRequest.params`'s open dicts) does
    not enforce the `const` on `kind`; a real model substituted a variant's schema title
    for the literal it should have copied (`{"kind": "NoAction", ...}` instead of
    `{"kind": "none", ...}`). Matching every title to its own kind value removes the
    mismatch regardless of which variant the model confuses.
22. **`summary_for_user` defaults rather than being strictly required** (phase 10) on
    `ToolPlan` and `WorkflowRequest`, via a `model_validator(mode="before")`: to `summary`
    if the model wrote that instead, else to `reasoning`. Superseded an earlier
    `validation_alias` attempt once it was clear the field is never read anywhere once
    written (it lands in the audit log's record of the proposal and nowhere else). The
    schema shown to the model is unchanged, still the real name, still required; this only
    changes what happens when a model doesn't comply. Every field something downstream
    actually consumes stays strictly required with no such fallback.
23. **The planner's retry message names specific missing or wrong fields**, built from the
    original `ValidationError` (reachable as `LLMOutputInvalid.__cause__`), instead of
    relaying `str(exc)` (phase 10). The raw, stack-trace-shaped error was observed not
    working: a retry given exactly that message dropped the same field again. Falls back
    to the plain message when the cause isn't a structured validation error.
24. **A system-prompt line emphasizing complete, nothing-omitted output was tried, measured,
    and reverted** (phase 10). It dropped Scenario B's real-API success rate from 19/20 to
    0/20, every run choosing `NoAction` instead of attempting the more complex `ToolPlan`
    it had just been completing correctly. Not deviation in the sense of "we did this
    differently"; the deviation is leaving a documented negative result in place rather
    than quietly discarding it, since this is exactly the kind of change that fails in the
    direction that looks safest (fewer writes) and nothing in the test suite would ever
    catch it, only measuring real-API behavior before and after did.
25. **`reallocate_lot`'s precheck gained a quantity-conservation check** (phase 11):
    `sum(add) != sum(remove)` is refused outright. Not in section 12's tool table. A real
    model, on the shortage fixture, repeatedly proposed moving less than the held
    allocation's full amount with no `flag_shortage` alongside it, leaving the order
    silently under-covered; nothing previously validated the two sums against each other.
26. **`QualityProvider` gained a `coverage_check` fact** (`required_qty`,
    `total_free_qty_available`, `shortfall`) (phase 11), not in section 9's contract.
    Mirrors `required_qty` already being handed to the stockout detector rather than left
    for the model to derive; a real model's own reasoning showed it could compute the
    shortfall correctly once the data was given directly, but not reliably when it had to
    sum several released lots' free quantities itself.
27. **`notify_user` and `flag_shortage`'s descriptions were rewritten** (phase 11) to state
    the generic distinction between "informs, creates no tracked follow-up" and "creates
    an owned item someone must act on." Both were one terse sentence before. A real model,
    with correct reasoning about a shortfall, chose `notify_user` to describe the problem
    in prose rather than `flag_shortage` to actually route it; measured across a 12-run
    real-API batch, the rewrite took runs with an actual fix action from 4/10 to 10/12.
28. **`Tool` gained a `resolves: bool` field** (`False` only for `notify_user` and
    `send_correction`) and `gate()` gained a rule blocking any free-form plan whose every
    step has `resolves=False` (phase 11), neither in section 7 or 11's contract. The
    description rewrite above (27) measurably helped but left a residual (2/12 runs still
    proposing `notify_user` alone); this closes it structurally rather than continuing to
    tune wording. This is also the rule that makes deviation 17's `retryable` flag ever
    fire.
29. **The retry message for this one retryable rejection does not offer `NoAction` as an
    alternative**, despite the first version tried doing exactly that (phase 11). Measured
    against the real API: offering it made the failure *more* common, not less -- the
    model would use the offered exit to give up rather than find the right tool, since it
    had already judged (by proposing a plan at all) that the item needed action. Same
    shape as deviation 24: a plausible-sounding addition, measured, found to backfire,
    reverted. The retry message instead states plainly that action was already judged
    necessary and must include a real resolving step.
30. **`handle_attention_item`'s free-form path now logs `gate.allowed` / `gate.blocked`**
    (phase 11); it never had, only the declared-workflow path's execution-time re-check
    did. Section 13 names both as required event types. Found as a side effect of adding
    the retry mechanism above, which needed this logging to be auditable; no existing test
    had caught the gap, since `gate()` itself is correctly unit-tested in isolation and
    nothing end-to-end had asserted the caller recorded its result.
