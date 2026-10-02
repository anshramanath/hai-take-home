# Harmony Agent Harness: Build Spec

This file is the source of truth for building this repo. It is a take-home assignment for Harmony AI ("Build an extendable agent harness for enterprise work"). The original PDF is in `docs/assignment.pdf` if present. Read this whole file before writing code.

---

## 0. How to work with me

- **Build in phases (section 14). Stop at the end of every phase**, summarize what you built, list any deviation from this spec, show how to run what exists, and wait for me before starting the next phase.
- **Do not deviate from the locked decisions (section 3) or the invariants (section 2) without asking first.** If something in this spec seems wrong, say so and propose a change; don't silently work around it.
- **Write all required docs:** `README.md`, `MODEL.md`, `DESIGN.md`, and the recorded run. Section 17 specifies their content. Docs must describe what the code actually does; if the code and this spec disagree, document the code and flag the discrepancy to me.
- **Every phase ships with its tests.** A phase is not done until its tests (section 15) exist and pass.
- Keep it small. A handful of entities and a few tools that make the story real beats a large fake ERP. Every file should earn its place.
- Prefer plain, readable code over cleverness. I need to explain every boundary in an interview.
- No agent frameworks (LangChain, LangGraph, CrewAI), no workflow engines (Temporal, Airflow), no ORM. Plain Python, plain SQL.
- Never call `datetime.now()`, `date.today()`, or `time.time()` for business logic. Use the injected clock.
- In prose you write (README, MODEL.md, CLI output), do not use em dashes or arrow characters.

---

## 1. What we're building

A platform where every employee at a manufacturer gets a personal AI agent. The agent notices problems before being asked, gathers context from ERP, mail, and calendar (scoped to what that user can see), proposes a fix with reasoning, gets it gated by policy and approved by a human, executes it safely, follows up later, and leaves an audit trail that fully explains what happened.

Two scenarios run through **one harness**:

- **Scenario A (declared workflow):** a critical part will run out, the supplier slipped, a production order is at risk. The planner decides a reroute is needed and supplies parameters; the reroute itself runs as a fixed, versioned workflow.
- **Scenario B (free-form):** a lot is on quality hold and a production order is allocated to it. The planner proposes an ordered list of tool calls; no workflow exists for this.

The harness is the product. The scenarios are demos. Adding Scenario B should require **no edits** to the planner, gate, or audit layers. If any are needed, they must be called out with a reason.

---

## 2. Invariants (never break these)

1. **The LLM never executes anything.** It only produces proposals (data). It has no handle to any tool's `run` function.
2. **Every write passes the gate and an approval** before it runs. Reads (detection, context gathering, arrival checks) do not need approval.
3. **Approval binds to a frozen plan.** The approved plan is stored as canonical JSON with a SHA-256 hash. The executor only ever executes the plan loaded from the approval record, verifies the hash, and makes **zero LLM calls** between approval and execution.
4. **Bounded LLM steps inside a workflow run before approval**, so their outputs are part of what is approved.
5. **Gate and policy are enforced in code**, not in the prompt. The prompt may describe the rules so proposals are sensible, but code is what blocks.
6. **Providers never return data the user cannot read.** Scoping happens in the provider, before anything reaches the LLM.
7. **Tools never run without the required write scopes.**
8. **Declared workflows are mandatory where they exist.** PO tools are workflow-only; the free-form runner refuses them. A workflow that cannot proceed stops and reports to a human; it never falls back to free-form.
9. **The audit log is append-only.** Code only INSERTs. A SQLite trigger raises on UPDATE or DELETE.
10. **The core is scenario-agnostic.** No `if part_id == "P-4471"` or purchasing-specific logic in planner, gate, approvals, executor, scheduler, or audit.
11. **Every component gets "today" from the clock.**
12. **Every write tool is idempotent** (via `executed_actions`) and declares a compensation.

---

## 3. Locked decisions

| Decision | Choice |
|---|---|
| Language | Python 3.11+ |
| Storage | single SQLite file via stdlib `sqlite3` |
| Validation and schemas | Pydantic v2 |
| LLM | OpenAI SDK, structured outputs (JSON schema from Pydantic). Model name via env var `HARNESS_MODEL`, key via `OPENAI_API_KEY` |
| CLI | Typer |
| Output formatting | Rich |
| Tests | pytest, using a fake LLM (no network) |
| Env and run | `uv` with `pyproject.toml`. One command: `uv run python -m harness demo` |
| Original PO on reroute | **Reduce** PO-77812 (not cancel) |
| Arrival check timing | At **Supplier Z's promised arrival date** (before the production start). Not Tuesday. README must explain this deviation (section 13) |
| daily_usage meaning | **Background consumption only.** Production orders are separate, discrete demand subtracted on their start date |
| Trap suppliers | **Two:** one cheaper but unapproved for the part; one approved but too slow for the need date |
| Lot coverage in Scenario B | **Splitting across multiple lots is allowed** |
| Stockout dedupe key | `stockout:{part_id}:{prod_order_id}:{inbound_po_id}` (includes the inbound PO the order depends on, so a new risk after a reroute can alert again) |
| Quality hold dedupe key | `quality_hold:{lot_id}:{prod_order_id}` |
| Free-form style | Plan up front (whole ordered plan proposed, approved, then run). Not step by step |
| Re-planning after gate rejection | Optional, max 1 retry, logged. If skipped, report the rejection to the user |

---

## 4. Repo layout

```
harness/
  __main__.py          # Typer app: demo, tick, approve, reject, explain, status, reset
  app.py               # wires everything: Harness object, registries, tick()
  world/               # the fake company (stand-in for real systems)
    schema.sql
    seed.py            # fixtures: scenario_a, scenario_b_covers, scenario_b_shortage, variants
  detection/           # Detector protocol, StockoutDetector, QualityHoldDetector, registry
  context/             # Provider protocol, ErpProvider, MailProvider, CalendarProvider, QualityProvider
  planning/            # prompt building, LLM client interface, OpenAI client, fake client, output models
  policy/              # gate, policy rules, approvals, escalation
  execution/           # tool catalog, tools, tool runner, workflow engine, workflows/reroute_po.py
  scheduling/          # clock, scheduler
  audit/               # append-only log, explain renderer
  memory/              # run state helpers, memory_facts
tests/
runs/                  # recorded runs (runs/scenario_a.txt)
README.md
MODEL.md
DESIGN.md              # design doc, 2 to 3 pages
pyproject.toml
```

Each folder exposes a small interface. Merges are deliberate: gate plus approvals in `policy/` (both answer "is this allowed, and by whom"); tool runner plus workflow engine in `execution/` (shared idempotency, compensation, audit); clock with scheduler.

---

## 5. Data model

All JSON-ish fields (lists, dicts) are stored as JSON text.

### Fake company (only providers and tools touch these)

```sql
CREATE TABLE erp_parts (part_id TEXT PRIMARY KEY, description TEXT, on_hand INT,
  daily_usage INT, safety_stock INT, unit_cost REAL, lot_tracked INT);
CREATE TABLE erp_suppliers (supplier_id TEXT PRIMARY KEY, name TEXT, contact_email TEXT,
  approved INT, approved_parts TEXT, lead_time_days INT, pricing TEXT);
CREATE TABLE erp_purchase_orders (po_id TEXT PRIMARY KEY, part_id TEXT, supplier_id TEXT,
  qty INT, unit_price REAL, total_value REAL, ordered_date TEXT, promised_date TEXT,
  status TEXT, created_by TEXT);
CREATE TABLE erp_production_orders (prod_order_id TEXT PRIMARY KEY, product TEXT, qty INT,
  scheduled_start TEXT, scheduled_end TEXT, status TEXT, line TEXT, supervisor_id TEXT,
  components TEXT);
CREATE TABLE erp_receipts (receipt_id TEXT PRIMARY KEY, po_id TEXT, qty INT, received_date TEXT);
CREATE TABLE erp_lots (lot_id TEXT PRIMARY KEY, part_id TEXT, qty INT, status TEXT,
  received_date TEXT, hold_reason TEXT, hold_placed_by TEXT, hold_placed_on TEXT);
CREATE TABLE erp_lot_allocations (lot_id TEXT, prod_order_id TEXT, qty INT,
  PRIMARY KEY (lot_id, prod_order_id));
CREATE TABLE mail_messages (message_id TEXT PRIMARY KEY, sender TEXT, recipients TEXT,
  sent_at TEXT, subject TEXT, body TEXT);
CREATE TABLE cal_events (event_id TEXT PRIMARY KEY, owner TEXT, start TEXT, end TEXT,
  title TEXT, out_of_office INT);
CREATE TABLE users (user_id TEXT PRIMARY KEY, name TEXT, email TEXT, role TEXT,
  manager_id TEXT, backup_approver_id TEXT, scopes TEXT, approval_limits TEXT);
CREATE TABLE notifications (notification_id TEXT PRIMARY KEY, to_user TEXT, from_user TEXT,
  sent_at TEXT, subject TEXT, body TEXT);
```

`erp_receipts` exists so the arrival check can ask "did the shipment arrive?" A small demo helper (`receive` CLI command or seed step) records receipts.

`erp_lot_allocations` replaces the sample's `allocated_to` array, which cannot represent quantities.

### Harness state

```sql
CREATE TABLE clock (id INT PRIMARY KEY CHECK (id = 1), today TEXT);
CREATE TABLE attention_items (item_id TEXT PRIMARY KEY, dedupe_key TEXT UNIQUE,
  detector TEXT, owner_id TEXT, summary TEXT, facts TEXT, status TEXT, created_at TEXT);
CREATE TABLE runs (run_id TEXT PRIMARY KEY, item_id TEXT, user_id TEXT, status TEXT,
  state TEXT, created_at TEXT);                       -- run memory
CREATE TABLE approvals (approval_id TEXT PRIMARY KEY, run_id TEXT, approver_id TEXT,
  plan_json TEXT, plan_hash TEXT, status TEXT, requested_at TEXT, decided_at TEXT,
  decided_by TEXT, routed_reason TEXT);
CREATE TABLE workflow_instances (instance_id TEXT PRIMARY KEY, run_id TEXT, definition TEXT,
  version INT, current_step INT, state TEXT, status TEXT);
CREATE TABLE scheduled_tasks (task_id TEXT PRIMARY KEY, run_at TEXT, kind TEXT,
  payload TEXT, status TEXT, created_by_run TEXT);
CREATE TABLE executed_actions (idempotency_key TEXT PRIMARY KEY, tool TEXT, args TEXT,
  result TEXT, executed_at TEXT);
CREATE TABLE memory_facts (fact_id TEXT PRIMARY KEY, subject TEXT, fact TEXT,
  source_ids TEXT, visible_to_scope TEXT, created_at TEXT, expires_at TEXT);
CREATE TABLE audit_log (seq INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, run_id TEXT,
  actor TEXT, event TEXT, detail TEXT);
-- plus triggers that RAISE on UPDATE or DELETE of audit_log
```

`ts` in audit is the simulated clock date plus a monotonic sequence; do not use wall-clock time.

---

## 6. Seed data

Fixtures are named and selectable: `seed(conn, fixture="scenario_a")`. Each demo section and each test reseeds its own fixture. Clock starts at **2026-09-02**.

### Users

| user_id | name | role | backup | manager | limits | key scopes |
|---|---|---|---|---|---|---|
| u-100 | Marcus Hale | Purchasing Director | | | po_create_max 100000 | erp:po:read/create/cancel, erp:production:read, mail:read/send, calendar:read, production:notify |
| u-101 | Dana Whitfield | Purchasing Manager | u-102 | u-100 | po_create_max 25000 | same as u-100 |
| u-102 | Priya Natarajan | Senior Buyer (Dana's backup) | | u-100 | po_create_max 25000 | same as u-101 |
| u-202 | Omar Reyes | Quality Manager | | | | erp:lot:read, erp:lot:allocate, erp:production:read, mail:read, calendar:read, production:notify, purchasing:flag (**no** erp:po:*) |
| u-301 | Lena Ortiz | Production Supervisor, Line 2 | | | | erp:production:read, mail:read, calendar:read |

Names are placeholders; keep them consistent.

### Scenario A (`scenario_a`)

- **Part P-4471** "Stepper motor, NEMA 23, 2.8A": on_hand 150, daily_usage 30, safety_stock 20, unit_cost 42.00, not lot-tracked.
- **Part P-2210** (noise part used by 4812 too): ample stock.
- **Suppliers:**
  - S-Y Kestrel Components: approved for P-4471, lead 7, 42.00
  - S-Z Meridian Drives: approved for P-4471, lead 2, 46.50 (the correct choice)
  - S-Q Bargain Motion (trap 1): **not approved** for P-4471, lead 1, 39.00
  - S-W Westline Supply (trap 2): approved for P-4471, **lead 9** (misses the need date), 43.00
  - one more supplier for P-2210 only (noise)
- **POs:**
  - PO-77812: P-4471, S-Y, qty 400, unit 42.00, ordered 2026-08-26, promised 2026-09-04, open, created_by u-101
  - 2 to 3 unrelated open POs for other parts (noise), one created by someone else
- **Production orders:**
  - 4812: "PRD-CX200 Conveyor Drive Unit", start 2026-09-07, planned, Line 2, supervisor u-301, components P-4471 x120, P-2210 x30
  - 1 to 2 unrelated orders (noise)
- **Mail (Dana's mailbox unless noted):**
  - M-001 from rita.alvarez@kestrelcomponents.example, subject "Re: PO-77812, shipment update", body says revised ship date Monday 9/7, on the dock Tuesday 9/8, sent 2026-09-01
  - M-002 vendor newsletter (noise)
  - M-003 from another supplier about a different part (noise)
  - M-004 internal note unrelated to P-4471 (noise)
  - M-005 addressed to someone other than Dana mentioning PO-77812 (must **not** reach Dana's context; proves mail scoping)
- **Calendar:**
  - E-001 Dana, routine meeting 9/2
  - E-002 Dana, "Out of office, supplier site visit", 2026-09-03 to 2026-09-04, out_of_office true
  - E-003 someone else's event (noise)

### Scenario B fixtures

Clock 2026-09-02. Part P-1180 lot-tracked.

- 4820: needs P-1180 x100, starts 2026-09-05, supervisor u-301
- 4831: needs P-1180 x50, starts 2026-09-15
- L-2093: qty 100, **hold**, "Surface finish 3.4 Ra vs spec 3.2 Ra", placed by u-202 on 9/2, allocation 4820:100
- L-2115: qty 80, released, allocation 4831:50 (30 free)

`scenario_b_covers` (split): add L-2101 qty 70 released, unallocated. 70 + 30 = 100 covers 4820 by splitting.
`scenario_b_shortage`: add L-2101 qty 60 released, unallocated. 60 + 30 = 90, short.

### Failure-case variants

Small fixture overrides, e.g. `scenario_a_no_supplier` (S-Z unapproved), `scenario_a_over_limit` (quantity or price pushing value above 25000), `scenario_a_backup_low_limit` (u-102 limit below the PO value), `scenario_a_no_arrival` (no receipt recorded at check time).

---

## 7. Core contracts

Use Pydantic models or dataclasses; keep them small.

```python
class AttentionItem:   detector, dedupe_key, owner_id, summary, facts: dict
class Detector(Protocol):
    name: str
    def detect(self, ctx) -> list[AttentionItem]: ...      # ctx gives db + clock

class ContextSlice:    source, records: list[dict], record_ids: list[str]
class Provider(Protocol):
    source: str
    def fetch(self, user, item) -> ContextSlice: ...

class Tool:
    name, description
    input_schema: type[BaseModel]
    required_scopes: list[str]
    writes: bool
    allowed_in: list[str] | None     # e.g. ["workflow:reroute_po"]; None means free-form allowed
    value: Callable[[args], float] | None   # dollar value for threshold rules
    precheck: Callable[[db, args], None] | None   # raises PrecheckFailed with a reason
    idempotency_key: Callable[[args, ctx], str]
    compensate: str | None           # name of another tool
    run: Callable[[db, args, ctx], dict]

class WorkflowDefinition: name, version, params_model, steps: list[Step]
class Step: name, kind ("check" | "llm" | "action"), fn, compensate: str | None

# Planner output: exactly one of
class WorkflowRequest: kind="workflow", workflow: Literal[...registered names], params, reasoning, summary_for_user
class ToolPlan:        kind="plan", steps: list[{tool, args}], reasoning, summary_for_user
class NoAction:        kind="none", reasoning     # planner judges nothing should be done
```

The `Literal` of workflow names and the tool list shown to the model are built from the registries at runtime, never hardcoded.

---

## 8. Detectors

**StockoutDetector** (Scenario A). For each planned production order within a 7-day horizon, for each component part, project inventory day by day from today to the order's start:
- subtract `daily_usage` each day (background demand)
- add open inbound POs on their `promised_date`
- subtract other production orders' component demand on their start dates

Raise an item when either:
1. **Short:** projected balance at the start date is below the order's requirement, or
2. **Thin margin:** the order is only covered because of an inbound PO whose promised date is within `MARGIN_DAYS = 3` of the order start.

For 4812 this fires on condition 2 via PO-77812 (promised 9/4, start 9/7). The ERP does not know about the slip; the email does. The detector flags risk; the planner confirms the problem. Owner: the `created_by` of the inbound PO it depends on (fallback: a user with role Purchasing Manager).

**QualityHoldDetector** (Scenario B). For each allocation on a lot with status `hold`, if the production order starts within 3 days, raise an item. Owner: the lot's `hold_placed_by` (fallback: role Quality Manager).

Detectors run on every tick and also after any tool writes to ERP tables. Insert into `attention_items` and treat a UNIQUE violation on `dedupe_key` as "already known" (log it to audit as `detection.duplicate_ignored`).

---

## 9. Providers

Each provider checks the user's read scope first, filters rows to what the user may see, filters for relevance with simple deterministic rules, and returns `record_ids` for audit.

- **ErpProvider:** the part, its open POs, the production order, suppliers with their approval status, lead times, and prices for that part (all of them, including traps, so the planner sees the temptation; the workflow filters).
- **MailProvider:** only messages where the user is a recipient, and only those from the contact emails of suppliers on relevant POs or mentioning a relevant PO id. Noise must be dropped here.
- **CalendarProvider:** the user's events for the next 7 days.
- **QualityProvider** (Scenario B): the held lot, its allocations, the affected order, and every released lot of the same part with **free quantity computed** (qty minus allocations).

Policy reads (e.g. reading an approver's calendar for escalation) are not done through providers and are not shown to the LLM. Keep that distinction explicit.

---

## 10. Planner

- Builds the prompt from: the attention item, all context slices, persistent memory facts relevant to the item (labeled as hints, never overriding live data), the workflow catalog, and the tool catalog **filtered to tools the user has scopes for**, excluding workflow-only tools.
- Calls the LLM through an interface `LLMClient.complete(messages, response_model) -> BaseModel`. Implementations: `OpenAIClient` (structured outputs), `FakeLLMClient` (scripted responses for tests), `ReplayClient` (replays responses recorded from a real run, used by the demo when no API key is set).
- Validates output with Pydantic. Invalid output: one retry with the validation error, then fail the run and report.
- Contains **no** scenario-specific wording.

---

## 11. Gate, policy, approvals

**Gate** (pure functions over the proposal, tool metadata, and users):
1. Every tool exists; args validate against `input_schema`.
2. Free-form plans may not include tools with `allowed_in` set (workflow-only).
3. Requester has every `required_scopes` for every write.
4. Policy rules (registered list). Initial rule: **PO value threshold**: for any action whose tool declares `value`, the approver must have `po_create_max >= value`. Start with the requester; if insufficient, walk up `manager_id` until someone qualifies.
5. Result: `Blocked(reason)` or `Allowed(approver_id, routed_reason)`.

Blocked proposals are never shown as approvable; the agent reports why.

**Approvals:**
- Create the approval with the frozen plan (canonical JSON, sorted keys) and its hash.
- Print the approval prompt: what is at risk, why, what is proposed (concrete values), dollar value, approver. The user-facing message should read like: "Part P-4471 will likely cause production order 4812 to miss its scheduled start. Supplier Y said the shipment is delayed until Tuesday. I can move part of the order to Supplier Z and notify production. Want me to proceed?"
- **Escalation rule** (runs every tick): if an approval is pending at end of day and the approver's calendar shows them out of office the next day, reassign to their `backup_approver_id`. The new approver must also pass the threshold rule; if not, walk up the backup's `manager_id`. Log the reason.
- `approve` and `reject` CLI commands. Only the current approver can act on it.

**Execution-time re-check:** immediately before each write, the executor re-runs the gate scope check for that action and the tool's `precheck`. Approval says a human agreed; the re-check says it is still allowed now.

---

## 12. Execution

### Workflow engine

`reroute_po` version 1, params: `part_id, original_po_id, prod_order_id, qty, needed_by`.

| # | Step | Kind | Runs | Compensation |
|---|---|---|---|---|
| 1 | confirm_supplier_approved | check | before approval | none |
| 2 | confirm_lead_time | check | before approval | none |
| 3 | choose_supplier | llm (bounded) | before approval | none |
| 4 | draft_notification | llm (bounded) | before approval | none |
| 5 | create_po | action | after approval | cancel_po |
| 6 | reduce_original_po | action | after approval | restore_po |
| 7 | notify_production | action | after approval | send_correction |
| 8 | schedule_arrival_check | action | after approval | cancel_task |

Purchasing's required business order (approved supplier, lead time, create, reduce, notify, schedule) is preserved; steps 3 and 4 are bounded LLM slots placed before approval per invariant 4.

- Steps 1 and 2 compute the candidate list in code: suppliers approved for the part **and** whose `today + lead_time_days <= needed_by`. S-Q fails step 1, S-W fails step 2. If the list is empty, the instance stops with status `halted_no_supplier`, no writes, and the user is told why.
- Step 3: the LLM gets only the filtered candidates and returns `{supplier_id, justification}`. Code verifies `supplier_id` is in the candidate list; otherwise reject (one retry, then halt).
- Step 4: the LLM drafts wording only. Recipient (the order's supervisor from ERP) and facts (PO numbers, dates, quantities) are filled by code.
- Quantity choice: the planner proposes `qty` as part of params; a reasonable default is enough to cover order 4812 and background usage until Supplier Y's revised arrival. `reduce_original_po` reduces PO-77812 by the same qty.
- After step 4, the instance status becomes `awaiting_approval` and an approval is created with the full frozen plan.
- After approval: steps 5 to 8 run. State is persisted after every step (`current_step`, `state`). On failure, run compensations for completed action steps in reverse order and set status `compensated`.
- `schedule_arrival_check` schedules a task at **the new PO's promised date** (Z's ETA), kind `arrival_check`, payload with PO id, part, order.
- Statuses: `running`, `awaiting_approval`, `completed`, `halted_no_supplier`, `failed`, `compensated`.
- **Resume:** on startup and on every tick, resume instances with status `running` from `current_step`.
- **Test crash hook:** `run(..., crash_after="create_po")` raises after that step is persisted, simulating a kill.
- **Versioning:** registry keyed by `(name, version)`; instances always resume with their own version.

### Tool runner (free-form)

Runs an approved `ToolPlan` in order with the same idempotency, precheck, re-check, audit, and reverse compensation on failure.

### Tools

| Tool | Scope | writes | allowed_in | value | precheck | compensation |
|---|---|---|---|---|---|---|
| create_po | erp:po:create | yes | workflow:reroute_po | qty * unit_price | supplier approved for part | cancel_po |
| cancel_po | erp:po:cancel | yes | workflow:reroute_po | | PO exists and open | restore_po |
| reduce_po | erp:po:cancel | yes | workflow:reroute_po | | new qty >= 0 and < current | restore_po |
| restore_po | erp:po:cancel | yes | workflow:reroute_po | | | |
| notify_user | production:notify | yes | | | recipient exists | send_correction |
| send_correction | production:notify | yes | | | | |
| schedule_check | (harness-internal, implied by the run) | yes | | | | cancel_task |
| cancel_task | (harness-internal) | yes | | | | |
| reallocate_lot | erp:lot:allocate | yes | | | target lots same part, released, enough free qty in total; source allocation exists | reallocate back |
| flag_shortage | purchasing:flag | yes | | | | withdraw_flag |

`reallocate_lot` args: `prod_order_id, from_lot, allocations: [{lot_id, qty}]` (supports splitting). It deletes the held allocation and inserts the new rows atomically.

`flag_shortage` creates an attention item owned by a Purchasing Manager (dedupe `shortage:{part_id}:{prod_order_id}`). Purchasing's agent then handles it on the next tick. There is no declared workflow for buying lot-tracked stock, and PO tools are workflow-only, so purchasing's agent will **recommend only** (alert Dana with the shortage details). Record this as future work (an `expedite_purchase` workflow).

Idempotency keys are stable across retries and resumes, e.g. `{run_id}:{instance_id}:{step}`.

---

## 13. Scheduler, clock, audit, memory

**Clock:** single row; `today()` and `advance(days)`. `tick()` does, in order: advance clock; run due scheduled tasks; run approval escalation (end-of-day rule applies to the day just ended); resume running workflow instances; run detectors; plan for any new attention items.

**Arrival check task:** looks up receipts for the PO. If received in full, log success and close the run. If not, create a new attention item (the dedupe key naturally differs because the inbound PO is now Z's) so the loop re-enters.

**Audit:** every component writes events with `actor` (detector name, user id, "planner", "gate", "executor", "scheduler") and JSON `detail`. Required event types at least: `detection.raised`, `detection.duplicate_ignored`, `context.gathered` (record_ids per source), `planner.proposed` (full proposal and reasoning), `planner.invalid`, `gate.allowed`, `gate.blocked`, `approval.requested` (plan_hash), `approval.escalated` (reason), `approval.decided`, `workflow.step_started`, `workflow.step_completed`, `workflow.halted`, `action.executed` (idempotency key, result), `action.skipped_idempotent`, `action.compensated`, `schedule.created`, `schedule.fired`, `memory.fact_written`.

**explain command:** reads audit only and prints the story in order, e.g.

```
[2026-09-02 #1] stockout: P-4471 covered for order 4812 only by PO-77812 (promised 9/4, start 9/7)
[2026-09-02 #2] context for u-101: erp [P-4471, PO-77812, 4812, S-Y, S-Z, S-Q, S-W], mail [M-001], calendar [E-001, E-002]
[2026-09-02 #3] planner: reroute_po ... reason: Supplier Y slipped to 9/8 per M-001; 4812 starts 9/7
[2026-09-02 #4] workflow check: S-Q not approved for P-4471; S-W lead time misses 9/7; candidates [S-Z]
...
[2026-09-02 #9] escalation: unanswered at end of day; u-101 out of office 9/3 per E-002; routed to u-102
```

Someone reading only this output must be able to reconstruct what the agent saw, concluded, was allowed to do, who approved what, and what changed in each system.

**Memory:** run memory lives in `runs.state`. Persistent memory: after a run completes or an arrival check confirms an outcome, write a few structured facts with sources (e.g. "Supplier S-Y slipped PO-77812 from 9/4 to 9/8", source M-001). Promote on confirmed outcomes, not predictions. Facts are passed to the planner as hints only.

---

## 14. Build phases (stop after each)

Each phase includes writing and passing its tests from section 15. Run the full suite (`uv run pytest`) at every checkpoint and report the result.

1. **Skeleton:** `pyproject.toml`, package layout, `schema.sql` with append-only trigger, `seed.py` with all fixtures, clock, `reset` and `status` CLI commands. Tests: 15.1. Checkpoint: I can reset, seed, and inspect.
2. **Audit, tools, gate, approvals:** tool catalog and all tools, idempotency, gate with policy rules, approvals with frozen plan hash, escalation rule. Tests: 15.2, 15.3, 15.4, 15.5. Checkpoint.
3. **Workflow engine:** `reroute_po` v1, persistence, resume, compensation, crash hook, version registry. Tests: 15.6. Checkpoint.
4. **Detection, context, planner, Scenario A:** providers for A, StockoutDetector with dedupe, OpenAI client, fake and replay clients, approval prompt, `tick`, `approve`, `reject`. Tests: 15.7, 15.8, 15.9, 15.12 (Scenario A). Checkpoint: Scenario A runs to approval and execution.
5. **Scheduler and follow-up:** arrival check at Z's ETA, receipts, re-entry when missing, memory facts, `explain`. Tests: 15.10, 15.11, 15.13. Checkpoint.
6. **Scenario B:** QualityHoldDetector, QualityProvider, reallocate_lot, flag_shortage, quality manager user. Report exactly which core files changed, if any, and why. Tests: 15.12 (Scenario B), plus B cases in 15.4, 15.7, 15.8. Checkpoint.
7. **Failure cases and test completion:** every remaining item in section 15, the requirement coverage test (15.14), and the coverage report. Checkpoint.
8. **Demo and docs:** `demo` command, recorded run, README, MODEL.md, DESIGN.md (section 17). Tests: 15.15. Checkpoint.

---

## 15. Tests

**Rules:**
- pytest, with `FakeLLMClient` everywhere. **No network.** A test that would need the real API is a bug.
- Every test starts from a named fixture via a `harness` pytest fixture that creates a fresh temp SQLite file, seeds it, and sets the clock. Tests never share state.
- Assert on **both** the system state (ERP rows, approvals, instances, tasks) **and** the audit log events. A behavior that isn't audited is a failing test.
- Prefer one behavior per test with a descriptive name (`test_gate_blocks_free_form_po_creation`).
- Run `pytest --cov=harness` and report coverage. Target **90%+ line coverage** for `policy/`, `execution/`, `detection/`, `scheduling/`, `audit/`; report any uncovered lines in those packages and justify them.

### 15.1 World and clock
- Each fixture seeds without error and has the expected row counts.
- `clock.today()` returns the seeded date; `advance(n)` moves it; persists across a new connection.
- A static check (test that greps the `harness/` source) fails if `datetime.now`, `date.today`, or `time.time` appears outside `scheduling/clock.py`.

### 15.2 Audit
- UPDATE on `audit_log` raises; DELETE raises.
- Events are ordered by `seq` and carry run_id, actor, event, JSON detail.
- `explain` renders only from audit rows (test by running Scenario A, then deleting nothing but closing and reopening the DB, and checking explain output contains the key facts: detection, M-001, E-002, S-Q rejection, S-W rejection, escalation to u-102, approval by u-102, created PO id, reduced PO-77812, notification, scheduled check date).

### 15.3 Tools and idempotency
- Every registered tool's `input_schema` rejects malformed args.
- Each write tool: running twice with the same idempotency key writes once and logs `action.skipped_idempotent`.
- Each tool's `precheck` refuses its invalid case: `create_po` with unapproved supplier; `reduce_po` with qty >= current or < 0; `reallocate_lot` with wrong part, held target lot, insufficient total free qty, or missing source allocation.
- Each compensation actually reverses its action: `cancel_po` after `create_po`; `restore_po` after `reduce_po`; `send_correction` after `notify_user` creates a correction row; `cancel_task` after `schedule_check`; reallocation back restores original allocation rows exactly; `withdraw_flag` closes the shortage item.
- `reallocate_lot` with a split (two target lots) writes both rows atomically; a failure partway leaves no partial allocation.

### 15.4 Gate and policy
- Missing write scope blocks (Dana proposing `reallocate_lot`; Omar proposing anything with `erp:po:create`).
- Workflow-only tool in a free-form plan blocks (`create_po` in a `ToolPlan`).
- Unknown tool blocks; invalid args block.
- Value under the requester's limit: approver is the requester.
- Value over the requester's limit: routes to `manager_id` (u-100) with `routed_reason`.
- Value over everyone's limit in the chain: blocked.
- Tools without `value` (`reallocate_lot`, `notify_user`) are unaffected by the threshold rule.
- Gate tests make **zero** LLM calls (assert the fake client was never invoked).
- Execution-time re-check: revoke the requester's scope after approval, before execution; the executor refuses the write and logs it.

### 15.5 Approvals and escalation
- Approval stores canonical plan JSON and matching hash.
- Tampering with `plan_json` after approval: executor refuses, logs, writes nothing.
- Only the current approver can approve or reject; anyone else is refused.
- Rejecting stops the run; nothing is written; the rejection is audited.
- Escalation: pending at end of day and approver OOO next day (E-002) reassigns to backup u-102 with reason.
- No escalation when the approver is not OOO the next day.
- No escalation when the approval was already decided.
- Backup whose limit is below the plan value: escalation continues to the backup's manager.
- An approval decided by the backup is attributed to the backup in approvals and audit.

### 15.6 Workflow engine
- Happy path runs steps in definition order; audit shows `workflow.step_started/completed` in that order.
- Steps 1 to 4 run before approval; instance is `awaiting_approval`; no ERP writes have happened.
- Candidate filtering: S-Q excluded at step 1, S-W excluded at step 2, both reasons audited; candidates == [S-Z].
- Bounded step: fake LLM returns S-Q, rejected; retry returns S-Z, accepted. Two invalid answers: instance halts, no writes.
- Notification step: fake LLM tries to set a different recipient or wrong PO number; code-controlled fields win.
- No qualifying supplier (`scenario_a_no_supplier`): status `halted_no_supplier`, zero writes, user informed, and **no fallback to free-form** (assert no `ToolPlan` executed).
- Resumption: `crash_after="create_po"`; new engine; `resume_all()`; completes; exactly one PO to S-Z; PO-77812 reduced exactly once.
- Crash after each action step (parametrized over 5 to 8): resume always completes with no duplicate writes.
- Failure in step 7 (force `notify_user` to raise): steps 6 then 5 compensated in reverse; status `compensated`; PO-77812 restored; new PO cancelled.
- Version registry: an instance with version 1 resumes with v1 even when a v2 is registered.
- The model cannot change step order: a planner output with extra or reordered steps is invalid (`WorkflowRequest` has no steps field; assert extra fields rejected).

### 15.7 Detectors and dedupe
- StockoutDetector fires for 4812 via thin-margin on PO-77812 with key `stockout:P-4471:4812:PO-77812`.
- Does not fire for noise orders with ample stock.
- Short condition (no inbound PO) fires.
- Running detectors twice: one item, `detection.duplicate_ignored` logged.
- After the reroute, if Z's PO is at risk, a new item with the new inbound PO key is created.
- Owner resolution: Dana (created_by of PO-77812); fallback by role works.
- QualityHoldDetector fires for L-2093/4820 within 3 days; not for 4831 (too far out); not for released lots.
- Detectors also run after a tool writes to ERP tables.

### 15.8 Providers and scoping
- Mail: Dana's context includes M-001; excludes M-002, M-003, M-004 (relevance) and M-005 (not her mailbox).
- User without `mail:read` gets an empty mail slice.
- Calendar: only the user's own events in the window.
- ERP: includes all suppliers for the part (including traps) and excludes unrelated POs.
- QualityProvider computes free quantity correctly for each fixture.
- `record_ids` match the records returned and are logged in `context.gathered`.
- Policy reads of the approver's calendar during escalation are not exposed to the planner (planner input for Dana does not include u-102's events).

### 15.9 Planner
- Prompt contains only tools the user has scopes for and excludes workflow-only tools (Omar's prompt has no `create_po`).
- Workflow names in the output schema come from the registry.
- Invalid output: one retry with the validation error; second invalid output fails the run with `planner.invalid`.
- `NoAction` output closes the run with no approval.
- Memory facts appear in the prompt labeled as hints; a memory fact contradicting live ERP data does not change gate or workflow results.
- Planner module contains no scenario-specific strings (static test for "P-4471", "PO-77812", "Supplier Z", etc. in `planning/`).

### 15.10 Scheduler and arrival check
- `schedule_arrival_check` creates a task at Z's promised date, not 9/8.
- Task survives restart (close DB, new harness, tick, task fires).
- Received in full: run closes successfully, memory fact written.
- Not received (`scenario_a_no_arrival`): new attention item created and loop re-enters.
- A task fires exactly once even if tick runs repeatedly on the same day.

### 15.11 Memory
- Facts are written only on confirmed outcomes (after arrival check or completion), with source ids.
- Expired facts are not included in the prompt.

### 15.12 End-to-end scenarios (fake LLM)
- **Scenario A:** full story from 9/2 through Tuesday 9/8: detection, escalation to u-102, approval, PO created with S-Z, PO-77812 reduced, supervisor notified, arrival check fired at Z's ETA, audit complete.
- **Scenario B covers (split):** reallocation to L-2101 (70) and L-2115 (30), supervisor notified, L-2093 allocation removed.
- **Scenario B shortage:** shortage flagged, item created for purchasing, Dana's agent recommends only (no PO tools executed), audit shows the handoff.
- **Free-form variability:** fake LLM returns the B plan in a different but valid order (notify first); the harness executes it as approved. A plan missing the notification still executes as approved (documents that free-form does not guarantee completeness).

### 15.13 Explain
- `explain` output for Scenario A contains every fact listed in 15.2, in chronological order, and nothing from other runs.

### 15.14 Requirement coverage
A single test module `tests/test_requirements.py` with one test per assignment requirement, named after it, that asserts the behavior end to end (it may call helpers used elsewhere):
- A1 detect without prompting; A2 distinct scoped providers; A3 recommendation and plan; A4 gate before any write; A5 idempotent logged execution; A6 follow-up re-enters; A7 explain from audit alone
- Part 1: each of the eight responsibilities is separately replaceable (swap a component for a stub and the rest still works: fake LLM, a dummy detector, a dummy provider)
- Part 2: fixed step order; no skip/add/reorder; bounded LLM steps constrained; idempotency and compensation per step; resume after kill; definitions versioned
- Part 3: new detector, provider, tool, and user work without core changes (assert planner, gate, and audit modules have no references to lots or quality)
- Permission model: providers never return unreadable data; tools never run without scope

### 15.15 Demo smoke test
- `demo` runs to completion with `ReplayClient` and no API key, exits 0, and its output contains the section headers and key facts.

---

## 16. Demo command

`uv run python -m harness demo` runs, with clear Rich section headers:

1. **Scenario A:** reseed; tick on 9/2; detection; context; plan; workflow checks (traps rejected); approval prompt to Dana; no answer; end of day escalation to u-102 (Dana OOO 9/3); u-102 approves via the same code path as the `approve` CLI; execution of steps 5 to 8.
2. **Follow-up:** record Z's receipt (or not, per variant); tick day by day **through Tuesday 9/8**; arrival check fires at Z's ETA; show result.
3. **Scenario B:** covers variant (split reallocation), then shortage variant (flag to purchasing, Dana's agent recommends only).
4. **Failure cases:** no qualifying supplier; over-limit routing to manager; missing scope blocked; process crash and resume without duplicates; duplicate detection ignored; frozen plan tamper rejected; arrival not received re-enters.
5. `explain` output for Scenario A.

`--interactive` pauses at approvals so the reviewer can approve or reject themselves.
If `OPENAI_API_KEY` is unset, use `ReplayClient` with recorded responses and print that it is doing so.

---

## 17. Docs and recorded run

All four are deliverables. Write them in phase 8, from the code as it actually exists. Plain, direct prose; bullets are fine; no em dashes or arrow characters. Every claim about behavior should be true of the code (if a test proves it, mention the test name where useful).

### README.md
- **How to run:** `uv run python -m harness demo`, env vars (`OPENAI_API_KEY`, `HARNESS_MODEL`), replay mode when no key, `--interactive`, every CLI command with a one-line example, how to run tests and coverage.
- **Architecture:** a short text diagram of the loop (trigger, detect, context, plan, gate, approve, execute, schedule, audit) and the three layers (pluggable pieces, core, SQLite), plus the folder map and why gate+approvals and runner+engine are merged.
- **Two execution paths:** declared workflow (Scenario A) vs free-form plan (Scenario B), and the rule: declared workflows are mandatory where they exist; a halted workflow reports to a human and never falls back to free-form.
- **Safety model:** model proposes, harness disposes; scoped providers; gate in code; frozen hashed plan; zero LLM calls between approval and execution; execution-time re-check; append-only audit.
- **How to add** a tool, a provider, a detector, a workflow: each a short recipe naming the file to create, the interface to implement, and the one registry line to add.
- **What Scenario B required:** list every file added; list every core file changed (planner, gate, approvals, executor, scheduler, audit) with the reason, or state plainly that none changed.
- **Notes:**
  - Arrival check timing: scheduled at Supplier Z's promised arrival, not Tuesday as the spec says, because Tuesday 9/8 is after order 4812 starts 9/7, so a missed delivery would be discovered too late to act. The demo still advances through Tuesday and the check fires on the way.
  - The detector flags risk from structured data (the ERP still shows PO-77812 on time); the LLM confirms the problem by reading the supplier's email.
  - Free-form plans are proposed up front because approval must precede any write.
  - Compensating a sent notification means sending a correction.
  - After a shortage flag, purchasing's agent recommends only, because no declared workflow exists for buying lot-tracked stock and PO tools are workflow-only.
- **What I cut and why:** e.g. no UI; no real identity provider; SQLite and a single process; simple relevance filters instead of search; memory kept minimal; no in-flight workflow migration; re-planning limited to one retry; no partial receipts. One line of reasoning each.
- **Test map:** the requirement coverage table from 15.14 (requirement, test name).

### MODEL.md
- What was **kept** from the sample schemas (and minor renames).
- What **changed or was added**, with why: `erp_lot_allocations` replacing `allocated_to` (array cannot hold quantities; splitting needs per-lot quantities); `erp_receipts` (arrival check needs a fact to check); `notifications` table; `daily_usage` as background demand with production orders as discrete demand (avoids double counting); dedupe key design (includes inbound PO so a new risk after a reroute can alert); harness state tables and what requirement each serves.
- **Seed design:** each noise record and trap, and the behavior it exists to test (S-Q, S-W, M-002 to M-005, noise POs and orders, scenario B variants, failure variants).
- **Left out** and why: inventory locations, units of measure, multi-currency, partial receipts beyond simple receipts, BOM hierarchies, supplier contracts, lot expiry.

### DESIGN.md (2 to 3 pages)
Write as the author (first person singular). Short paragraphs and bullets. Take clear positions. Sections:

1. **Identity and authorization (required).** SSO through the company's identity provider (Okta or Entra ID); the harness never handles passwords. Per-system calls use token exchange (OAuth 2.0 Token Exchange, or on-behalf-of) to mint short-lived, scope-limited tokens for the acting user; no standing service account with broad write access. Background detection uses a narrow read-only service identity; writes use a token minted at approval time, when the approving human is present, so the approval click is what authorizes the write. Defense in depth: the harness gate checks scopes and the downstream system checks the token. Distinguish user-visible context (scoped to the user) from policy reads (e.g. the approver's calendar for escalation), which run under a separate, audited policy identity and are never shown to the model. Map this onto the current code (scopes in the users table stand in for token claims).
2. **Long-term memory (required).** Run memory vs persistent memory as built. Promote only structured facts with provenance, on confirmed outcomes rather than predictions. Expiry and invalidation when live data conflicts. Memory is a hint to the planner, never a substitute for re-reading the source of truth before acting. Memory is permission-scoped. Name what the current build does and what it would need.
3. **Scaling to thousands of employees (required).** Where it breaks first, in order, and the fix for each: detectors scanning per tick (move to change events from the ERP, run once per company, route items by ownership); SQLite and one process (Postgres, a queue, workers); in-process scheduler and workflow engine (a durable workflow system such as Temporal); LLM cost and rate limits (only call on real attention items, small prompts, caching, smaller models for bounded steps); downstream API limits (caching, webhooks); human attention and alert fatigue (prioritization, batching, digests).
4. **Workflows: versioning and the workflow-first question (required by Part 2).**
   - Versioning: instances record their version and always resume on it (as built). Default policy is pinning: in-flight instances finish on their original version; new instances use the new one; old definitions stay until their last instance completes. Because approval binds to a plan built under a specific version, changing version mid-flight means re-approval. Urgent safety fixes: cancel and re-plan instances not yet approved; explicitly migrate executing instances only if the fix touches their remaining steps, with the migration audited.
   - Workflow-first answer: say "partly." The reroute already runs as a declared workflow, but the decision to enter it, the choice between workflow and free-form, and approval, escalation, and follow-up live in an agent loop around it. Starting workflow-first, the whole lifecycle would be a declared graph started by the detector's event type; each LLM call becomes a typed node with an enumerated output (late or not; reroute, wait, or partial; supplier from a list); approval waits, escalation, and arrival checks become durable timers inside the graph; free-form survives only as a default branch requiring full human review; and the execution history doubles as the audit log. Position: for a manufacturer with known, audited processes, workflow-first is the right production design; agent-first was chosen here because the assignment requires a free-form path and one declared workflow, and a loop is simpler in the time box. Free-form is where workflows get discovered; the workflow engine is where they get trusted. Note that Scenario B is predictable enough to promote to a declared workflow.
5. **Connecting real systems (optional, include briefly).** Provider and tool interfaces stay; implementations change: ERP API (SAP or NetSuite), Microsoft Graph for mail and calendar, a search-backed provider for documents. What changes: pagination, rate limits, staleness, real idempotency (pass the key as the PO's external reference), and irreversible actions where compensation becomes a reversing action.
6. **Observability and evaluation (optional, include briefly).** Trace every run's stages; measure approve, reject, and edit rates, false alarms, time to resolution, and whether the outcome happened; replay saved scenarios (A and B are the first) on every prompt or model change and block regressions.

Keep it to 2 to 3 pages. Cut optional sections shorter before trimming required ones.

### Recorded run
- `runs/scenario_a.txt`: the full console output of Scenario A from the demo with a real API key (approval prompt, escalation, execution, follow-up), followed by `explain` output. Strip ANSI color codes.
- Also save the model responses from that run as the replay fixtures used by `ReplayClient`, so the demo works without a key.
