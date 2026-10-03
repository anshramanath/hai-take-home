# Harmony agent harness

An extendable agent harness for enterprise work: a personal AI agent per employee that
notices problems from company data before being asked, gathers context scoped to what
that employee can see, proposes a fix with reasoning, gets the fix gated by policy and
approved by a human before anything writes, executes it safely and idempotently, follows
up later, and leaves an audit trail that explains the whole story on its own.

## How to run

Requires [`uv`](https://docs.astral.sh/uv/). No other setup.

```
uv sync
uv run python -m harness demo
```

That one command runs Scenario A through approval, escalation, execution, and the
follow-up; then Scenario B's two variants; then seven failure cases; then prints
`explain`'s reconstruction of Scenario A's audit trail. It works with no API key (see
below). The full recorded run is at [`runs/scenario_a.txt`](runs/scenario_a.txt).

### Environment variables

- `OPENAI_API_KEY` — if set, Scenario A's planning and its two bounded workflow steps call
  the real OpenAI API. If unset, `demo` and the `tick`/`approve` CLI commands print a
  notice and replay recorded responses from [`runs/scenario_a_responses.json`](runs/scenario_a_responses.json)
  instead — an actual real-model run, captured once and replayed, not hand-written.
  Scenario B and the failure cases inside `demo` always use a scripted fake client
  regardless of this variable: they exist to demonstrate mechanics (reallocation,
  compensation, tamper-rejection), not live model reasoning, and nothing in the test suite
  is allowed to touch the network either.
- `HARNESS_MODEL` — model name for the real API (default `gpt-4o-mini`).

### `--interactive`

`uv run python -m harness demo --interactive` pauses at Scenario A's final approval
decision and asks you, in the terminal, whether to approve or reject as the current
approver. Everything before that (detection, planning, the escalation itself) is
unattended, since the point of the demo is to show the escalation rule firing, not to
require you to manufacture it.

### Every CLI command

```
uv run python -m harness reset --fixture scenario_a        # wipe and reseed harness.db
uv run python -m harness status                             # row counts + today's date
uv run python -m harness tick                                # advance one simulated day
uv run python -m harness approve AP-XXXX --as u-101          # approve and execute
uv run python -m harness reject AP-XXXX --as u-101           # reject, nothing runs
uv run python -m harness receive PO-XXXX 120                 # record a receipt
uv run python -m harness explain                              # print the audit narrative
uv run python -m harness demo                                 # the full walkthrough
```

All of them accept `--db <path>` (default `harness.db`); `reset --fixture` accepts any of
the seven names in `harness/world/seed.py` (`scenario_a`, four `scenario_a_*` failure
variants, `scenario_b_covers`, `scenario_b_shortage`).

### Tests and coverage

```
uv run pytest
uv run pytest --cov=harness --cov-report=term-missing
```

204 tests, no network calls anywhere in the suite (`FakeLLMClient` and `ReplayClient`
only). `policy/`, `execution/`, `detection/`, `scheduling/`, and `audit/` are all at
**100%** line coverage. The two remaining gaps are documented and intentional:
`world/seed.py` (96%, two unreachable defensive guards on fixture names) and
`planning/llm.py` (`OpenAIClient`'s real-network branches, which the "no network in
tests" rule forbids exercising through pytest — validated by hand against the real API
instead, repeatedly, across development).

## Architecture

One loop, seven replaceable stages:

```
 ┌─────────┐   ┌─────────┐   ┌────────┐   ┌──────┐   ┌─────────┐   ┌───────────┐   ┌───────┐
 │ detect  │──>│ context │──>│  plan  │──>│ gate │──>│ approve │──>│  execute  │──>│ audit │
 └─────────┘   └─────────┘   └────────┘   └──────┘   └─────────┘   └───────────┘   └───────┘
      ▲                                                     │              │
      │                                              (escalation)   (schedule follow-up)
      └─────────────────────── tick() ──────────────────────┴──────────────┘
```

`tick()` (`harness/app.py`) is the heartbeat: it runs due scheduled tasks, escalates
overdue approvals, resumes any workflow a crash left mid-flight, runs every detector, and
plans for whatever's still unplanned — all scoped to "today" — then advances the clock
last. See **Notes** below for why last, not first.

Three layers:

- **Pluggable** (swap freely, each behind its own registry): `detection/` (one class per
  detector), `context/` (one class per provider), `execution/workflows/` (one module per
  declared workflow), `planning/llm.py` (one class per LLM client).
- **Core** (scenario-agnostic, never edited to add a scenario): `planning/planner.py` and
  `prompt.py` (the free-form reasoner), `policy/gate.py` and `approvals.py` (permissions,
  thresholds, escalation, the frozen plan), `execution/executor.py` and `engine.py` and
  `runner.py` (the one place tools actually run, the declared-workflow engine, the
  free-form plan runner), `audit/` (the append-only log and its renderer).
- **SQLite**: one file, one schema (`harness/world/schema.sql`), holding both the fake
  company and the harness's own state (approvals, workflow instances, scheduled tasks,
  memory facts, the audit log).

Folder map:

```
harness/
  __main__.py, app.py, demo.py   CLI, orchestration (tick/approve/reject), the demo script
  world/        schema.sql, seed.py (7 fixtures), users.py, receipts.py
  detection/    Detector protocol + registry; StockoutDetector, QualityHoldDetector
  context/      Provider protocol + registry; Erp/Mail/Calendar/QualityProvider
  planning/     prompt building, output models, LLMClient + Fake/OpenAI/Replay/Recording
  policy/       gate.py (permissions + thresholds), approvals.py (frozen plan, escalation)
  execution/    tool catalog, the one executor, the workflow engine, the free-form runner,
                workflows/reroute_po.py
  scheduling/   clock.py, tasks.py (generic deferred-task runner), arrival_check.py
  memory/       runs.py (run-scoped state), facts.py (persistent, confirmed-outcome facts)
  audit/        log.py (append-only writer), explain.py (renders it as a narrative)
tests/          20 files, 204 tests
runs/           scenario_a.txt (recorded transcript), scenario_a_responses.json (replay fixture)
```

**Why gate and approvals share a package**: both answer "is this allowed, and by whom" —
the gate decides once at proposal time, approvals enforces it afterward (who the current
approver is, whether the plan still matches what was approved). **Why the tool runner and
workflow engine share a package**: they share the same idempotency, scope re-check, and
compensation primitives (`execution/executor.py`); the only real difference between them
is whether step order is fixed by a definition or by whatever the planner proposed.

## Two execution paths

**Declared workflow** (Scenario A, `reroute_po`): the planner only decides *that* this
workflow applies and supplies its parameters. From there, the definition is in charge —
fixed step order, two plain code checks, two LLM steps bounded to a pre-filtered candidate
list and free-text drafting only (never the facts or the recipient), then the four
approved actions. `WorkflowRequest` has no `steps` field at all; the model cannot reorder,
skip, or add to what the definition declares.

**Free-form** (Scenario B): the planner proposes a whole ordered list of tool calls up
front. Nothing enforces step order or completeness beyond what the gate already checked
and what the approval already froze — a plan can omit a step a human might expect and
still run exactly as approved, by design (see `test_free_form_plan_missing_a_step_still_executes_as_approved`).

**The rule connecting them**: declared workflows are mandatory where they exist.
`create_po`/`cancel_po`/`reduce_po`/`restore_po` declare `allowed_in=("workflow:reroute_po",)`,
and the gate refuses them outright in a free-form plan. A workflow that halts (no
qualifying supplier, two invalid bounded-LLM attempts) reports to a human and never falls
back to free-form.

## Safety model

- **The model proposes; the harness disposes.** The LLM never holds a tool's `run`
  function. It produces a `WorkflowRequest`, a `ToolPlan`, or a `NoAction` — data, not
  execution.
- **Providers scope before they filter.** Every provider checks the user's read scope
  first; a user without it gets an empty slice back, never an error, never someone else's
  data.
- **The gate is code, not prompt text.** Unknown tool, invalid args, a workflow-only tool
  used free-form, a missing scope, a dollar value nobody in the approval chain covers —
  all blocked in `policy/gate.py`, independent of anything the model said.
- **The approved plan is frozen and hashed.** Canonical JSON, SHA-256, stored at approval
  time. Both execution paths re-verify the hash before running anything; a tampered
  `plan_json` is refused, nothing writes, and it's audited.
- **Zero LLM calls between approval and execution.** Action steps read their args back
  from the approval's own frozen plan — never recomputed, so a clock advance or a delayed
  approval can't silently change what gets executed.
- **Execution-time re-check.** Immediately before every write, the executor re-reads the
  requester's current scopes and the tool's precheck — a scope revoked after approval
  stops the write even though the approval already happened.
- **The audit log is physically append-only.** A SQLite trigger raises on `UPDATE` or
  `DELETE` against `audit_log`; code only ever `INSERT`s.

## How to add a tool, a provider, a detector, a workflow

**Tool**: add a Pydantic args model to `execution/args.py`, a `run` function (and
`precheck`/`compensation_args` if it writes) to `execution/catalog.py`, and one `Tool(...)`
entry in that file's `_TOOLS` list. It's immediately visible to the free-form planner
(unless you set `allowed_in=(...)` to restrict it to a workflow) and immediately subject
to the same idempotency, scope-check, and audit every other tool gets, for free.

**Provider**: implement `Provider` (`source: str`, `fetch(ctx, user, item) -> ContextSlice`)
in a new `context/<name>.py`, scope-gate at the top, and add one line to `PROVIDERS` in
`context/registry.py`. See `context/quality.py` for the shortest real example.

**Detector**: implement `Detector` (`name: str`, `detect(ctx) -> list[AttentionItem]`) in
a new `detection/<name>.py` and add one line to `DETECTORS` in `detection/registry.py`.
Give every raised item a `dedupe_key` that includes whatever fact would legitimately
change if the same underlying problem reappeared (see `detection/quality_hold.py` for the
smallest real example).

**Workflow**: define `params_model`, a tuple of `Step`s (`kind` one of `"check"` / `"llm"`
/ `"action"`), and `build_plan_steps` in a new `execution/workflows/<name>.py`, following
`reroute_po.py`'s shape; call `register(...)` at import time and make sure something
imports the module (see `execution/workflows/__init__.py`) so registration actually runs —
this bit me twice during development (see **Notes**) before I made it an explicit,
guaranteed side-effect import in both `app.py` and `tests/conftest.py`.

## What Scenario B required

New: `detection/quality_hold.py`, `context/quality.py`, `execution/runner.py` (the
free-form tool runner, deferred from the phase that built Scenario A specifically so both
paths would stay genuinely independent), and the fixtures in `world/seed.py`. The
"different user with different scopes" (Omar Reyes, quality manager) was already seeded
from the start.

**Core files that changed, and why**: `detection/registry.py` and `context/registry.py` —
one line each, adding the new detector/provider to their lists (the kind of change section
3 of the assignment explicitly anticipates). `execution/args.py` / `catalog.py` — a fix to
`flag_shortage` (see Notes). `app.py` — wiring the free-form runner into `approve()`, the
one piece of orchestration deliberately left unfinished until Scenario B needed it.

**Core files that did NOT change**: `planning/planner.py`, `planning/prompt.py`,
`policy/gate.py`, `policy/approvals.py`, `audit/log.py`, `audit/explain.py`. Verified, not
just asserted — `test_part3_planner_gate_and_audit_have_no_references_to_lots_or_quality`
greps the actual files for both words.

## Notes

- **Arrival check timing**: scheduled at Supplier Z's own promised arrival date
  (2026-09-04 in the seeded scenario), not literally "Tuesday" as the original scenario
  text says. Tuesday (9/8) is after production order 4812 is already scheduled to start
  (9/7), so a check that late would discover a missed delivery too late to act on it.
  "Tuesday" in the original text is tied to *that* scenario's own numbers (the supplier's
  email said dock Tuesday); the equivalent point in this harness's numbers is Z's own ETA.
  The demo doesn't pad out extra ticks to reach a date with nothing left to show.
- **`tick()` advances the clock last, not first.** The escalation rule reads as "if
  unanswered at end of the day that's ending, and the approver is out tomorrow" — a check
  meant to run while "today" is still that day. Advancing first would process tomorrow's
  date on the very first tick after seeding and would shift the escalation's "is the
  approver out tomorrow" check by a day from how it's built and tested.
- **The detector flags risk from structured data alone**; the email is what confirms it.
  The ERP still shows PO-77812 as promised on time; only `MailProvider`'s context (M-001)
  tells the planner it actually slipped.
- **Free-form plans are proposed whole, up front**, not step by step, because approval has
  to precede any write — there's no safe moment to ask for a single step's approval and
  then let the model decide the next one.
- **Compensating a sent notification means sending a correction** (`send_correction`): you
  can't un-send an email, only follow up.
- **After a shortage flag, purchasing's agent recommends only.** No declared workflow
  exists for buying lot-tracked stock, and PO tools are workflow-only, so the free-form
  path literally cannot create one — by design, not as a gap. The fix would be a new
  `expedite_purchase` workflow, deliberately left as future work.
- **`flag_shortage` resolves its own `owner_id`** instead of taking it as an argument — a
  correction made once the free-form planner actually had to call it: nothing in a
  planner's context tells it Dana's internal `user_id`, so asking for it as a parameter
  was asking the model to invent an unreachable fact.
- **Two bugs were found only by running against the real API, not by reasoning about the
  code**: a workflow-registration side effect that depended on some other import having
  already triggered it (fixed by making the import explicit in both `app.py` and
  `tests/conftest.py`), and OpenAI's strict structured-output mode rejecting the
  intentionally open `dict` fields in `ToolCall.args` / `WorkflowRequest.params` (fixed by
  building the request with `"strict": false` by hand and trusting Pydantic's own
  validation on the response). Full account of both, and four more real-model findings
  that shaped prompt and schema design, in `BUILD_LOG.md`.

## What I cut, and why

- **No UI.** The CLI's `approve`/`reject`/`explain` and the `--interactive` demo flag are
  the whole interaction surface the assignment asks for ("a CLI or HTTP endpoint ... is
  fine").
- **No real identity provider.** `users.scopes` stands in for what a real SSO/token-exchange
  layer would assert; see `DESIGN.md` for how that maps onto a real deployment.
- **SQLite, one process, one file.** Sufficient for two scenarios and a few hundred rows;
  `DESIGN.md` covers where this breaks first at scale.
- **Simple relevance filters instead of search.** `MailProvider` matches on sender and a
  literal PO-id substring. Correct for a handful of seeded messages; a real mailbox would
  need a search-backed provider behind the same interface.
- **Memory stays minimal.** Two write sites (workflow completion, confirmed arrival), no
  subject-scoped retrieval beyond "everything unexpired" — the demo's data volume doesn't
  need more, and `DESIGN.md` covers what more would look like.
- **No in-flight workflow migration.** Versioning is built and tested (an instance keeps
  resuming on the version it started on even after a new one is registered); migrating an
  *executing* instance to a new version mid-flight is a `DESIGN.md` question, as the
  assignment specifies.
- **Re-planning after an invalid LLM output is capped at one retry**, then the run fails
  and reports why, rather than looping.
- **No partial-receipt accounting beyond a simple sum.** The arrival check sums
  `erp_receipts` for a PO and compares to the ordered quantity; it doesn't model
  over-receipt, multiple partial shipments with different dates, or quality inspection on
  receipt.

## Test map

One test per assignment requirement, in `tests/test_requirements.py`:

| Requirement | Test |
|---|---|
| A1: detect without being prompted | `test_a1_detect_without_being_prompted` |
| A2: gather context via distinct scoped providers | `test_a2_gather_context_from_distinct_scoped_providers` |
| A3: reason to a recommendation and a proposed plan | `test_a3_reason_to_a_recommendation_and_a_proposed_plan` |
| A4: gate before any write | `test_a4_gate_blocks_before_any_write` |
| A5: idempotent, logged execution | `test_a5_execution_is_idempotent_with_each_step_logged` |
| A6: follow-up schedules and re-enters | `test_a6_follow_up_schedules_a_check_and_re_enters_if_missing` |
| A7: explain from audit alone | `test_a7_explain_reconstructs_the_story_from_audit_alone` |
| Part 1: LLM client is swappable | `test_part1_the_llm_client_is_swappable_with_no_other_code_change` |
| Part 1: a dummy detector is pluggable | `test_part1_a_dummy_detector_is_pluggable` |
| Part 1: a dummy provider is pluggable | `test_part1_a_dummy_provider_is_pluggable` |
| Part 2: step order is fixed, not model-chosen | `test_part2_step_order_is_fixed_the_model_cannot_add_or_reorder_steps` |
| Part 2: bounded LLM step is constrained | `test_part2_bounded_llm_step_rejects_a_choice_outside_the_candidates` |
| Part 2: every action step has a compensation | `test_part2_every_action_step_declares_a_compensation` |
| Part 2: resume after kill, no duplicates | `test_part2_resume_after_kill_completes_with_no_duplicate_writes` |
| Part 2: definitions are versioned | `test_part2_definitions_are_versioned_instances_keep_their_own_version` |
| Part 3: no core references to lots/quality | `test_part3_planner_gate_and_audit_have_no_references_to_lots_or_quality` |
| Part 3: different user, different scopes | `test_part3_quality_manager_has_different_scopes_than_purchasing` |
| Part 3: same generic planner and gate | `test_part3_scenario_b_runs_through_the_same_generic_planner_and_gate` |
| Permission model: providers scope reads | `test_permission_model_providers_never_return_unreadable_data` |
| Permission model: tools enforce write scope | `test_permission_model_tools_never_run_without_scope` |

The gate, trigger dedupe, and workflow resumption requirements called out specifically in
the assignment's deliverables are covered in depth in `tests/test_gate.py`,
`tests/test_detection.py` (dedupe), and `tests/test_workflow_engine.py` (resumption), with
the above as the requirement-level summary.

## Other docs

- [`MODEL.md`](MODEL.md) — what was modeled, what changed from the sample schemas, and why.
- [`DESIGN.md`](DESIGN.md) — identity/authorization, long-term memory, scaling, and the
  workflow-first design question, for the parts not built.
- [`BUILD_LOG.md`](BUILD_LOG.md) — a running engineering log of every phase: what was
  built, why, every deviation from the original spec with its reasoning, and the real bugs
  found by testing against the actual API rather than reasoning about the code in advance.
