# Harmony AI Take-Home: Agent Harness Study Guide

Everything covered while working through the "Build an extendable agent harness for enterprise work" assignment: what they want, how the system fits together, the domain concepts, a section-by-section walkthrough of the PDF, design decisions, and the stack.

---

## 1. The assignment in one paragraph

Build a small but real "agent harness" for a fake manufacturer. Each employee gets a personal AI agent that notices problems before being asked, gathers context from the ERP, email, and calendar (only what that user can see), recommends a fix, gets human approval, executes it safely in company systems, follows up later, and leaves an audit trail that explains everything. You must make two scenarios work end to end (A: a late supplier threatening a production order; B: a quality hold on a lot of parts), using the same harness for both.

**Time box:** 3 calendar days. Required parts are about 10 to 14 hours; with optional parts, 18 to 24. Do the required parts well, then choose extras. **Telling them what you cut and why is part of the grade.**

**One-sentence summary of what's being built:** Scenario A and Scenario B running through one harness. A uses a declared workflow after the planner decides; B is free-form, where the planner chooses the tools and their order, so the plan can differ between runs. The harness is the real product; the scenarios are the demos.

---

## 2. Core concepts

### What a harness is

A harness is all the code around the LLM that turns it from "a thing that generates text" into "a thing that can safely do work." On its own a model can only take text in and put text out. It can't check a database, remember yesterday, wake up on Tuesday, or be trusted to respect permissions just because the prompt says so. The harness supplies all of that:

- **Feeds it inputs:** decides when to call the model (a detector firing) and what to show it (context providers).
- **Constrains its outputs:** structured responses, validation, rejecting anything outside allowed options.
- **Turns decisions into actions:** tools, the executor, the workflow engine.
- **Enforces rules the model can't be trusted with:** permissions, thresholds, approval routing.
- **Handles time and memory:** scheduling, persisted state, resuming after crashes.
- **Records everything:** the audit log.

Analogy: the LLM is the horse (the power, good at fuzzy work), the harness connects that power to the cart and steers it. Claude Code, Cursor, and ChatGPT with tools are all harnesses around a model.

"Extendable" means someone can plug in a new detector, provider, tool, or workflow without rewriting the core. Scenario B is the test of that.

### What the LLM does vs. what the harness does

The model does more than read. It **interprets and proposes**:

- reads unstructured input (Supplier Y's email) and connects it to structured data (PO-77812)
- judges the situation ("reroute is the right call, not waiting")
- picks among allowed options (Supplier Z from a pre-filtered list)
- drafts text (the notification)

It **never touches a system directly**. The rule of thumb:

> **The model proposes. The harness disposes.**

The model's output is a structured suggestion. The harness then:

1. **Validates** it (is the chosen supplier in the allowed list? are the args the right types?)
2. **Gates** it (does the user have the write scopes? is the PO over their limit?), in code, before anyone is asked to approve
3. **Asks for approval**, routing to the backup if needed
4. **Executes** through tools in a fixed order, logging each step

Safety is **layered**: model choices are pre-restricted, outputs are validated, policy is enforced in code, and then a human signs off. If one layer fails, the others still hold. This is the answer to "why would anyone trust an AI to create purchase orders?"

### Where the LLM sits on the spectrum

Deterministic detection, then LLM interpretation and choice, then deterministic validation, gating, and execution. The model sits in the middle, boxed in on both sides.

The fixed system does as much as it can first. The detector knows from plain arithmetic that a part runs out in five days. The LLM earns its place on what rules can't handle, like reading "revised ship date is Monday, which puts it on your dock Tuesday."

---

## 3. Architecture

### The loop

1. **Trigger:** clock tick or ERP change event
2. **Detector:** rules find "attention items" (deterministic)
3. **Context providers:** ERP, mail, calendar, each scoped to the user (deterministic)
4. **Planner:** LLM produces a recommendation and proposed plan
5. **Gate and approval:** scopes, thresholds, human sign-off (deterministic)
6. **Executor:** declared workflow or free-form tool calls (deterministic)
7. **Scheduler:** deferred checks that survive restart, which re-enter the loop at the top

An **append-only audit log** records every stage: what was seen, what was concluded, why, whether it was allowed, who approved, what changed where, and what got scheduled.

Only one box (the planner) is the LLM. Everything that touches safety, money, or ordering is ordinary testable code.

### Three layers

**Layer 1: Pluggable (grows with each scenario)**

| Piece | Contract | Scenario A | Scenario B adds |
|---|---|---|---|
| Detectors | look at the world, return attention items with a dedupe key | `StockoutDetector` | `QualityHoldDetector` |
| Providers | given user + item, return a slice of one system, filtered by read scopes | ERP, mail, calendar | lot data (extend ERP or new `QualityProvider`) |
| Tools | one action in one system: input schema, required write scopes, idempotency key, compensation | create PO, reduce/cancel PO, notify user, schedule check | reallocate lot, flag shortage |
| Workflows | named, versioned, ordered list of steps with bounded LLM slots | `reroute_po` | none |

**Layer 2: Harness core (written once, never knows which scenario it's running)**

- **Planner:** builds the prompt from the item, context, and the tool/workflow catalogs; calls the LLM; returns either "run workflow X with these params" or "run these tool calls."
- **Gate:** checks every proposed write against scopes and policy; decides who must approve.
- **Approvals:** holds pending requests; each tick applies the escalation rule (unanswered at end of day and approver out tomorrow means route to backup).
- **Workflow engine:** runs a definition step by step, saves after each step, resumes on restart, compensates in reverse on failure.
- **Tool runner:** runs free-form tool calls in order with the same idempotency and audit behavior.
- **Scheduler:** stores deferred tasks and fires them when the clock reaches their date.
- **Audit log:** everyone writes, nobody edits.
- **Clock:** the only source of "today."

**Layer 3: One SQLite file**

- **Fake company tables:** ERP, mail, calendar, users. Only providers and tools touch them.
- **Harness state tables:** approvals, attention items, workflow instances, scheduled tasks, executed actions, memory, audit. This is what makes resume, dedupe, idempotency, and audit work.

**Key idea:** the top layer grows, the middle layer doesn't. If adding Scenario B required no core edits, you get to say so confidently in the README.

### One tick, end to end

1. Clock advances. Scheduler fires anything due. Approvals escalate anything stale.
2. Every registered detector runs. New attention items are saved; duplicates bounce off the dedupe key.
3. For each new item, the core identifies the owning user, asks every provider for context as that user, and calls the planner.
4. The planner returns a workflow request or a tool plan. The gate checks it and creates an approval request.
5. When approval arrives (via CLI), the workflow engine or tool runner executes, writing to the fake company tables and the audit log.
6. Follow-ups go into the scheduler for a later tick.

### A defensible folder layout

The PDF says: "We'd rather see a decomposition you can defend than a module per bullet."

```
harness/
  detection/     detectors + registry
  context/       providers + scoping
  planning/      prompt building, LLM client, output validation
  policy/        gate + approvals + escalation rule
  execution/     tool catalog, tool runner, workflow engine, idempotency, compensation
  scheduling/    clock + scheduled tasks
  audit/         append-only log
  memory/        run state + persistent facts
  world/         fake company: schema, seed data
```

Reasons for the merges:

- **Gate and approvals together** in `policy/`: both answer "is this allowed, and by whom?" Escalation is just policy that runs on a tick.
- **Workflow engine and tool runner together** in `execution/`: they share idempotency, compensation, and audit behavior. A workflow is just an ordered, persisted list of tool calls.
- **Clock with scheduling:** the scheduler is the main thing that cares about time.
- **LLM client behind an interface** in `planning/`: tests can use a fake model, and providers can be swapped.
- **`world/` separate:** it's the stand-in for real systems. In production it disappears and providers point at real APIs.

---

## 4. Data and storage

### No microservices

The PDF says: "Static files, SQLite, an in-process fake API: your choice." It also says keep the model as small as possible. One process and one SQLite file is exactly what they expect. ERP, mail, and calendar are just tables.

The separation they care about lives in your **code**, not your deployment:

```python
class MailProvider(Protocol):
    def fetch(self, user: User, item: AttentionItem) -> list[Email]: ...

class FakeMailProvider:
    def __init__(self, db): self.db = db
    def fetch(self, user, item):
        if "mail:read" not in user.scopes:
            return []
        # only queries mail tables, only this user's mailbox
        ...
```

Rules that make the boundary real:

- **Only the mail provider touches mail tables**, only ERP providers and tools touch ERP tables. The planner and gate never run SQL against a system directly. Prefix tables (`erp_parts`, `mail_messages`, `cal_events`) to make ownership obvious.
- **Nothing joins across systems in SQL.** Linking Supplier Y's email to PO-77812 happens in the harness, the same as it would with real systems that can't see each other.
- **Writes go through tools**, which declare their required scopes.

Payoff for the design doc: "swap `FakeMailProvider` for a `GraphMailProvider` that calls Microsoft Graph with the user's delegated token; nothing above it changes."

### Why SQLite

Not required, but several requirements quietly need real persistence:

- "A killed process can resume where it left off" means workflow state must be written to disk after each step.
- "Scheduling deferred work that survives a restart" means the Tuesday check can't live in memory.
- Append-only audit and trigger dedupe want durable, queryable records.

SQLite is built into Python, needs no server, and keeps "one documented command" achievable. Postgres would add setup friction for no benefit at this scale; mention it in the scaling section instead.

### Setup

```
harness/
  db/
    schema.sql      # CREATE TABLE statements
    seed.py         # loads the fake company
    connection.py   # opens the db
  harness.db        # created by seed.py (gitignored)
```

```python
import sqlite3

def connect(path="harness.db"):
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row      # rows behave like dicts
    conn.execute("PRAGMA foreign_keys = ON")
    return conn
```

### Schema: fake company

```sql
-- ERP
CREATE TABLE erp_parts (part_id TEXT PRIMARY KEY, description TEXT,
  on_hand INT, daily_usage INT, safety_stock INT, unit_cost REAL, lot_tracked INT);
CREATE TABLE erp_suppliers (supplier_id TEXT PRIMARY KEY, name TEXT,
  approved INT, lead_time_days INT, approved_parts TEXT, pricing TEXT);  -- JSON text
CREATE TABLE erp_purchase_orders (po_id TEXT PRIMARY KEY, part_id TEXT,
  supplier_id TEXT, qty INT, unit_price REAL, promised_date TEXT, status TEXT);
CREATE TABLE erp_production_orders (prod_order_id TEXT PRIMARY KEY,
  scheduled_start TEXT, status TEXT, supervisor_id TEXT, components TEXT);

-- Scenario B
CREATE TABLE erp_lots (lot_id TEXT PRIMARY KEY, part_id TEXT, qty INT,
  status TEXT, received_date TEXT, hold_reason TEXT);
CREATE TABLE erp_lot_allocations (lot_id TEXT, prod_order_id TEXT, qty INT,
  PRIMARY KEY (lot_id, prod_order_id));

-- Mail and calendar
CREATE TABLE mail_messages (message_id TEXT PRIMARY KEY, sender TEXT,
  recipients TEXT, sent_at TEXT, subject TEXT, body TEXT);
CREATE TABLE cal_events (event_id TEXT PRIMARY KEY, owner TEXT,
  start TEXT, end TEXT, title TEXT, out_of_office INT);

-- Identity
CREATE TABLE users (user_id TEXT PRIMARY KEY, name TEXT, role TEXT,
  backup_approver_id TEXT, scopes TEXT, approval_limits TEXT);
```

### Schema: harness state

```sql
CREATE TABLE clock (id INT PRIMARY KEY CHECK (id = 1), today TEXT);

CREATE TABLE attention_items (item_id TEXT PRIMARY KEY,
  dedupe_key TEXT UNIQUE,          -- same condition twice is rejected
  detector TEXT, user_id TEXT, payload TEXT, created_at TEXT);

CREATE TABLE approvals (approval_id TEXT PRIMARY KEY, item_id TEXT,
  approver_id TEXT, status TEXT, requested_at TEXT, decided_at TEXT);

CREATE TABLE workflow_instances (instance_id TEXT PRIMARY KEY,
  definition TEXT, version INT, current_step INT, state TEXT, status TEXT);

CREATE TABLE scheduled_tasks (task_id TEXT PRIMARY KEY, run_at TEXT,
  kind TEXT, payload TEXT, status TEXT);

CREATE TABLE executed_actions (idempotency_key TEXT PRIMARY KEY,
  tool TEXT, result TEXT);         -- running a step twice finds the first result

CREATE TABLE memory_facts (fact_id TEXT PRIMARY KEY, subject TEXT,
  fact TEXT, source_ids TEXT, created_at TEXT, expires_at TEXT);

CREATE TABLE audit_log (seq INTEGER PRIMARY KEY AUTOINCREMENT,
  ts TEXT, run_id TEXT, actor TEXT, event TEXT, detail TEXT);
```

Notes:

- **Lists and nested objects** (approved_parts, pricing, components, scopes) are stored as JSON text. Keeps the table count small.
- **Constraints solve requirements.** `dedupe_key UNIQUE` is trigger dedupe. `idempotency_key PRIMARY KEY` is idempotency. The `clock` table is how you advance time.
- **Append-only audit:** code only INSERTs. Optionally add a trigger that raises on UPDATE or DELETE.

---

## 5. The clock

The clock is fake on purpose. It only moves when told to, because the demo can't wait six real days for Tuesday.

**Rule: nothing in the harness ever calls `datetime.now()`.** Every component asks the clock.

```python
class Clock:
    def __init__(self, db): self.db = db

    def today(self) -> date:
        row = self.db.execute("SELECT today FROM clock WHERE id = 1").fetchone()
        return date.fromisoformat(row["today"])

    def advance(self, days: int = 1):
        new = self.today() + timedelta(days=days)
        self.db.execute("UPDATE clock SET today = ? WHERE id = 1", (new.isoformat(),))
        self.db.commit()

def tick(harness):
    harness.clock.advance(1)
    harness.scheduler.run_due()     # fires tasks whose run_at <= today
    harness.approvals.escalate()    # end-of-day rule
    harness.detectors.run_all()     # look for new attention items
```

Demo story:

1. Clock is 9/2. Detectors run, alert fires, approval requested.
2. Nobody answers. Tick. End-of-day check sees Dana is out tomorrow and routes to the backup.
3. Backup approves. Workflow runs. Arrival check written to `scheduled_tasks`.
4. Tick until the check date. Scheduler fires it and re-enters the loop if the shipment didn't arrive.

Injecting the clock also makes tests reproducible: a test can set any date, run one step, and assert the result with no real waiting.

---

## 6. Domain concepts

### Purchase order vs. production order

- **Purchase order (PO):** the company buys parts from a supplier. Parts come **in**. Supply.
- **Production order:** the factory builds a product. Parts are consumed. Demand. It builds a finished product (order 4812 makes 10 conveyor drive units) and lists every component it needs (120 of P-4471, 30 of P-2210).

Scenario A is a timing mismatch between supply (PO-77812 arriving 9/8) and demand (order 4812 needing it 9/7).

In this domain "PO" always means purchase order. Don't mix them up in code or docs.

### The part record

```json
{ "part_id": "P-4471", "description": "Stepper motor, NEMA 23, 2.8A",
  "on_hand": 150, "daily_usage": 30, "safety_stock": 20, "unit_cost": 42.00, "lot_tracked": false }
```

| Field | Meaning |
|---|---|
| part_id | Internal identifier everything else references |
| description | Human-readable name. NEMA 23 is a standard mounting size; 2.8A is rated current. Matters for LLM output and notifications. |
| on_hand | Units in the warehouse now |
| daily_usage | Average units consumed per day; used to project the future |
| safety_stock | Buffer the company tries never to go below |
| unit_cost | Normal price per unit. Suppliers have their own pricing (Z charges 46.50). |
| lot_tracked | false means all units are interchangeable; one on_hand number is enough |

**The math:**

- Days until empty: 150 / 30 = **5 days** (stock hits zero around 9/7)
- Days until below safety stock: (150 - 20) / 30 = about 4.3 days
- Order 4812 starts 9/7 and needs 120.
- PO-77812 (400 units) was promised 9/4, but Supplier Y's email moves it to 9/8. One day too late.
- Reroute: Supplier Z, approved for this part, 2-day lead time. 400 at 46.50 = **$18,600**, under Dana's **$25,000** limit, so she can approve it herself. Tweak seed data to push over $25k and you can demo escalation to her manager.

**Modeling question:** does daily_usage already include order 4812's 120 units? If you count both, you double-count and alert too early. A defensible choice: daily_usage is background consumption, production orders are discrete demand subtracted on their start date. State the choice in MODEL.md.

### Lots

A lot is a batch of units that came in together (one delivery, one supplier, one date, one production run). If a part is lot-tracked, the ERP records which lot every unit belongs to.

- **Not lot-tracked:** one row per part. `P-4471 | on_hand 150`.
- **Lot-tracked:** one row per batch. The part's total on hand is the sum across its lots.

Manufacturers lot-track parts where quality or traceability matters (safety-critical, regulated, prone to batch defects). If a problem is found, only the bad batch is isolated, and defects can be traced back.

Lots are defined by **where units came from**, not which order they're for. Allocation is a separate layer on top.

| lot_id | part_id | qty | status | allocated_to |
|---|---|---|---|---|
| L-2093 | P-1180 | 100 | hold | 4820 |
| L-2101 | P-1180 | 60 | released | (none) |
| L-2115 | P-1180 | 80 | released | 4831 |

### Status and allocation

**Status: can this batch be used at all?** (quality verdict)

- **released:** passed inspection, usable
- **hold:** problem found or suspected; can't be consumed until investigated (L-2093: 3.4 Ra vs 3.2 spec)
- optional extras: quarantine (not yet inspected), rejected (confirmed bad). Only hold and released are needed.

**Allocated to: has a production order already claimed it?** A reservation, like holding a library book.

They're independent, which creates the Scenario B problem:

| status | allocated | meaning |
|---|---|---|
| released | no | free and usable, ideal replacement |
| released | yes | usable but spoken for |
| hold | yes | **the problem:** an order is counting on units it can't use |
| hold | no | bad batch, nobody affected yet |

### The allocation gap in the sample schema

`allocated_to: ["4820"]` holds **production order** ids and **no quantities**. That breaks as soon as a lot is split across orders or an order draws from two lots. Fix it with an allocations table:

```sql
CREATE TABLE erp_lot_allocations (
  lot_id TEXT, prod_order_id TEXT, qty INT,
  PRIMARY KEY (lot_id, prod_order_id)
);
```

- Free quantity in a lot = lot qty minus sum of its allocations.
- Reallocation = delete one row, insert another. Clean tool with an obvious compensation.
- Good MODEL.md line: "Replaced `allocated_to` with an allocations table because the sample array can't represent quantities, which the reallocation decision depends on."

---

## 7. Section-by-section walkthrough of the PDF

Note: the PDF's numbering skips section 7 (goes 1 to 6, then 8, 9). Something was probably removed. Not something you need to address.

### Section 1: The problem

The vision paragraph is the grading rubric in disguise:

| Phrase in the vision | Where it shows up |
|---|---|
| understands role and work | users table with roles and scopes |
| reasons across ERP, email, calendar | separate providers per system, planner |
| notices before being asked | detectors on a schedule |
| recommends or executes actions | planner proposes, tools execute |
| within that person's permissions | scoped providers, gate checks write scopes |
| human approval where it matters | thresholds, backup routing |
| full explanation of why | audit log with reasoning |

- **"every employee"** is why the doc asks about scaling, and why Scenario B uses a different user. Don't hardwire around Dana.
- **"personal"** means the agent acts as a specific person, never as an all-powerful system account. Root of the identity question.

### Section 2: Scenario A

**Timeline (today = Wed 9/2):**

| Date | What's true |
|---|---|
| 9/2 | 150 motors on hand, 30/day. Agent notices. |
| 9/3 to 9/4 | Dana out of office (supplier site visit) |
| 9/4 | When PO-77812 was supposed to arrive |
| 9/7 | Stock hits zero. Order 4812 starts, needs 120. |
| 9/8 (Tue) | When Supplier Y's email says it will actually arrive |

**The alert** has a structure your planner output should support: what's at risk, why, what I propose, may I.

**On approval:** create replacement PO with Z; cancel or reduce the original; notify production; schedule an arrival check.

- **Cancel vs reduce** is a real decision. Cancel loses Y's 400 units entirely; reduce keeps later supply. Pick one and have the planner state why.

**Backup routing:** if an approval is unanswered at end of day and the approver's calendar shows them out the next day, route to their backup (u-102). Realistic demo: request to Dana on 9/2, no answer, end of day, Dana OOO 9/3, routed to backup, backup approves.

**The seven requirements:**

| # | Requirement | Plain words |
|---|---|---|
| 1 | Detect without being prompted | A detector finds it on a tick |
| 2 | Context via distinct providers, scoped | Three providers, only Dana's data |
| 3 | Reason to a recommendation and plan | The LLM step |
| 4 | Gate before any write | Code checks scopes, limit, routing. Reads OK without approval; writes never. |
| 5 | Execute idempotently with logged rationale | No double POs; every step logged with why |
| 6 | Follow up, re-enter if missing | Persisted task; new attention item if not arrived |
| 7 | Explain from the audit log alone | Log must carry reasons, not just events |

Requirement 7 example of good detail: "created PO-80021 because Supplier Y slipped to 9/8 per email M-001, approved by u-102 as backup since u-101 was OOO per E-002."

#### The Tuesday check problem

Taken literally, it doesn't make sense. Z's order goes out around 9/2 or 9/3, arrives around 9/4 or 9/5 (2-day lead time). Order 4812 starts 9/7. Checking on Tuesday 9/8 discovers a failed delivery **after** the order already missed its start. A useful check fires at Z's expected arrival, while there's still time to react.

Possible reasons they wrote Tuesday: reused the memorable date from the story; deliberately off to see who notices; or they meant confirming Y's reduced shipment (stretch, since the text says "the new shipment").

**What to do:** satisfy the letter and fix the logic.

- Compute the check date from the new PO: expected arrival plus a small buffer, before the production start.
- Still show a follow-up firing when the clock reaches Tuesday (second confirmation, or seed data where Z promises Tuesday; simplest is to make the check date a parameter driven by the PO).
- README note: "The spec schedules the check for Tuesday, but that's after production order 4812 starts, so a missed delivery would be discovered too late to act. The harness schedules the check at the new PO's promised date, before the production start, and the demo also advances to Tuesday as requested."

Noticing a requirement that doesn't serve the customer's goal, handling it without breaking the requirement, and explaining it is the FDE job description.

### Section 3: What you model

Headline: **keep it as small as the scenarios allow.** A handful of entities and four tools beats a fake ERP with forty tables.

**1. Systems and data, with noise:**

- **Irrelevant emails** (newsletter, unrelated supplier thread, internal note)
- **Unrelated POs** for other parts
- **A supplier that looks attractive but shouldn't be used:** cheaper or faster than Z but not approved for this part, or approved but too slow for 9/7. Most important trap, because it's how you demonstrate the gate and workflow constraints working.

Test for seed data: if the agent got the right answer, would it have been easy to get the wrong one? If not, add a trap.

**2. Permission model:**

- Users with roles: Dana (Purchasing Manager), her backup, her manager, a production supervisor, a quality manager for B
- Per-system scopes split into read and write (`erp:po:read` vs `erp:po:create`, `mail:read` vs `mail:send`)
- Approval threshold ($25,000 for Dana)
- Backup approver
- Enforcement in code: providers don't return unreadable data; tools don't run without write scope
- Make users differ. The quality manager lacks PO-creation scope, which is why B ends in "flag a shortage to purchasing."

**3. Tool catalog:**

```python
Tool(
    name="create_po",
    input_schema=CreatePOArgs,  # Pydantic model
    required_scopes=["erp:po:create"],
    idempotency_key=lambda args, ctx: f"{ctx.run_id}:create_po:{args.part_id}",
    compensate="cancel_po",
    allowed_in=["workflow:reroute_po"],
    run=create_po_impl,
)
```

"Whatever your harness needs to run it safely" invites idempotency keys, compensation, a read/write flag, and workflow-only restrictions. Scenario A needs about four or five tools.

**4. Clock:** covered above.

**Also:** use a real LLM API in the main demo (unit tests can stub it). Write MODEL.md:

- *Kept:* parts, suppliers, POs, production orders, mail, calendar, users, mostly as given
- *Changed:* allocations table; daily usage as background demand
- *Left out:* inventory locations, multi-currency, partial receipts, BOM hierarchies, because neither scenario needs them

### Section 4: Part 1, the harness

The key sentence: "How you factor them is your call. We'd rather see a decomposition you can defend than a module per bullet."

| Responsibility | Must prove |
|---|---|
| Detecting on schedule or event | Fires unprompted; no duplicate alerts |
| Gathering context per system, scoped | Separate per system; no data leaks |
| Planning with reasoning | Structured output carrying its reasoning |
| Gating **in code** | A test shows a forbidden action blocked without calling the LLM |
| Executing with idempotency and compensation | No double writes; failures undoable |
| Memory: run vs persistent | See below |
| Scheduling that survives restart | Kill, restart, task still fires |
| Append-only audit | Nothing edited; log alone tells the story |

- **"Enforced in code":** rules only in the prompt fail the requirement. The LLM can be told rules so it proposes sensibly, but code stops it.
- **"Separately replaceable":** swap the LLM provider, storage, or a detector without touching the others.

**Memory:**

- **Run memory:** what one pass knows: item, context, plan, approval status, step results. Saved for resume, then done.
- **Persistent memory:** carries across runs and affects future decisions. "Supplier Y slipped PO-77812 by 4 days." "Dana rejected reroutes to Supplier Q twice."
- Build: a small `memory_facts` table written after runs, surfaced as context next time. Thinking goes in the design doc.
- Rule: **memory informs the planner, it never overrides current data.** If memory says Z is reliable but the ERP shows Z unapproved today, the ERP wins.

### Section 5: Part 2, deterministic workflows

Purchasing's rule: the reroute steps are fixed, in order, every time.

1. Confirm alternate supplier is approved for the part
2. Confirm their lead time meets the production date
3. Create the new PO
4. Cancel or reduce the old one
5. Notify production
6. Schedule the arrival check

**Who controls what:**

| | Planner (LLM) | Workflow definition (code) |
|---|---|---|
| Should we reroute? | decides | |
| Part, old PO, qty, needed-by date | supplies | |
| Which steps, what order | | fixed |
| Skip or add steps | no | no |
| Small judgment calls inside a step | only in designated slots | defines slot, validates answer |

**Definition:**

```python
REROUTE_PO = WorkflowDefinition(
    name="reroute_po",
    version=1,
    params={"part_id": str, "original_po_id": str, "qty": int, "needed_by": date},
    steps=[
        Step("confirm_supplier_approved", check_approved_suppliers),
        Step("confirm_lead_time",         check_lead_time),
        Step("create_po",                 create_po,       compensate=cancel_po),
        Step("cancel_or_reduce_original", reduce_po,       compensate=restore_po),
        Step("notify_production",         notify,          compensate=send_correction),
        Step("schedule_arrival_check",    schedule_check,  compensate=cancel_task),
    ],
)
```

Planner output is just `{"workflow": "reroute_po", "params": {...}}`, validated against declared types.

**Bounded LLM steps.** Pattern: **code narrows the options, the model chooses, code verifies the choice.**

- **Supplier choice:** code filters to approved-for-part and lead-time-OK suppliers; model returns `{"supplier_id", "justification"}`; code checks the answer is in the list. If the model names the trap supplier, it's rejected.
- **Notification:** model writes wording; code controls recipients (supervisor from ERP) and facts (PO number, dates).

**Idempotency:** `create_po` uses a key like `reroute:{instance_id}:create_po`, checks `executed_actions` first, and returns the existing PO if present. Protects resumes where the process died after creating the PO but before marking the step done.

**Compensation:** on failure, walk backward through completed steps and undo them. Some things can't truly be undone (sent email), so compensation is a correction. Say so in the README.

**Persistence and resume:** after every step, save `current_step` and state. On startup, resume instances with status "running."

**Approval placement:** run read-only check steps 1 and 2, pause as `awaiting_approval`, then do the writes. Status flow: running, awaiting_approval, running, completed. The manager approves a plan already validated.

**Versioning:** store the version on each instance. In-flight instances finish on their original version; new ones use the new version; keep old definitions until their last instance completes. Optionally an explicit, audited migration for urgent fixes.

**Closing the back door:** mark PO tools as workflow-only (`allowed_in=["workflow:reroute_po"]`) so the free-form runner refuses them. Then "the reroute always runs as a declared workflow" is guaranteed by code.

**When a check step fails:** if no supplier qualifies, the workflow stops cleanly before any writes and reports back ("no approved supplier can deliver by 9/7"). Good demo failure: remove Z's approval in seed data.

**Testing resume:**

```python
engine.run(instance, crash_after="create_po")   # dies after step 3
engine = WorkflowEngine(db)                      # fresh engine, like a restart
engine.resume_all()                              # picks up at step 4
assert count_pos_for("P-4471", supplier="S-Z") == 1   # no duplicate PO
```

Proves both resumption and idempotency in one test.

### Section 6: Part 3, Scenario B

**Story:** lot L-2093 of P-1180 (100 units) placed on quality hold 9/2 by u-202 (surface finish 3.4 Ra vs 3.2 spec). Production order 4820 is allocated to it and starts in three days. The quality manager's agent either finds a good lot and proposes reallocation plus notifying the production supervisor, or finds none and flags a shortage to purchasing. Seed data or test variants for **both** outcomes.

**What you must add:**

| Required | For you |
|---|---|
| Quality/lot data | lots + allocations tables |
| New detector | `QualityHoldDetector` |
| New or extended provider | lot and allocation data, scoped by `erp:lot:read` |
| At least one new tool | `reallocate_lot`, `flag_shortage` (reuse `notify_user`) |
| Different user, different scopes | quality manager: can read lots and reallocate, cannot create POs |

**Scenario B is not declared.** The PDF says: "Scenario B has no workflow and runs through the free-form path, so both paths stay exercised." Purchasing gave a fixed procedure; quality didn't. It looks predictable enough to declare, which is a good design doc observation.

**Tools check themselves.** In free-form, don't trust the model to have picked a valid lot. `reallocate_lot` verifies: same part, status released, enough unallocated quantity. If not, it refuses. This is the free-form equivalent of workflow check steps.

**`flag_shortage` is a handoff between agents.** It creates an attention item owned by the Purchasing Manager. Next tick, Dana's agent picks it up with its own permissions. One agent can't act beyond its scopes, but can hand the problem to one that can. Strong demonstration of "every employee has an agent."

**Approval still applies.** The quality manager approves the whole plan, since the model proposed the steps.

**"If B required editing the planner, gate, or audit, tell us why."** Aim for "it didn't":

- **Planner** builds prompts from item, context, and catalogs; nothing mentions POs.
- **Gate** checks declared scopes generically; policy rules (PO threshold) are registered and apply only to tools they name.
- **Audit** uses a generic event type plus JSON detail.

If something did need a core edit, say so with the reason (e.g., "the gate assumed every write had a dollar value; I made the threshold rule apply only to tools that declare one").

### Section 8: Design doc (2 to 3 pages)

| Section | Status |
|---|---|
| Identity and authorization | required |
| Long-term memory | required |
| Scaling to thousands | required |
| Workflow-engine question + versioning (from Part 2) | required |
| Connecting real systems | optional |
| Observability and evaluation | optional |

**Identity and authorization**

- **SSO:** the company's identity provider (Okta, Microsoft Entra ID) authenticates users; the harness gets a token. No password handling.
- **Token exchange / on-behalf-of:** to call the ERP as Dana, trade her token for a short-lived token for that system and those scopes (OAuth 2.0 Token Exchange, Microsoft's on-behalf-of flow).
- **No standing credentials:** avoid one god-mode service account. Every call carries an expiring token limited to that user's rights.
- **Background detection wrinkle:** detection uses a narrow read-only service identity; **writes only use a token minted at approval time**, when the human is present. The approval click authorizes the write.
- **Defense in depth:** your gate checks scopes and the real system also checks the token.
- **Policy reads vs user context:** escalation reads the approver's calendar as a policy check, not as context on Dana's behalf. Keep the distinction explicit.

**Long-term memory**

- Promote only stable, useful, structured facts tied to sources.
- Promote on outcome, not prediction (save "Y was late" when the late receipt is confirmed, not when the email arrives).
- Each fact has provenance, timestamp, expiry; conflicting facts get invalidated.
- Memory is a hint; always re-read the source of truth before acting.
- Memory is permission-scoped too.

**Scaling (break points, roughly in order)**

1. Detectors scanning everything per user. Fix: run once per company on change events; route items by role.
2. SQLite and one process. Fix: Postgres plus a queue and workers.
3. In-process scheduler and workflow engine. Fix: durable workflow system like Temporal.
4. LLM cost and rate limits. Fix: call only for real items, small prompts, caching, cheaper models for bounded steps.
5. Real system API rate limits. Fix: caching, webhooks instead of polling.
6. Human attention: thousands of agents means alert fatigue. Prioritization and batching matter as much as infrastructure.

**Workflow-engine question:** see section 8 below.

**Connecting real systems (optional):** interfaces stay; implementations change. `ErpProvider` calls SAP or NetSuite; mail and calendar call Microsoft Graph; a document store gets a search-based provider. What changes: pagination, rate limits, slightly stale data, real idempotency (pass your key as the PO's external reference), and irreversible actions where compensation means a reversing action.

**Observability and evaluation (optional):**

- Trace every run: detection, context, prompt, output, gate decision, approval, execution.
- Measure: approve/reject/edit rates, false-alarm rate, time to resolution, whether the outcome happened (did the order start on time?).
- Catch regressions: saved scenarios with known right answers, replayed on every prompt or model change; block if quality drops. Scenarios A and B are the first eval cases.

**Page budget:** about half a page each for the three required sections, a third of a page for the Part 2 questions, short paragraphs for optional ones. Bullets are fine.

### Section 9: Deliverables

**1. Repo with one documented command** that runs Scenario A through approval and execution, the clock to Tuesday with the follow-up firing, Scenario B, then failure cases.

- **Approval in a one-command demo:** print the prompt, then approve through the same CLI path a human uses (`approve <id> --as u-102`). Add `--interactive` so the reviewer can approve themselves.
- **Reset and reseed** at the start so runs are repeatable.
- **API key:** document the env var. Optional: save model responses from the recorded run and replay them if no key is set.
- **Failure cases:** unapproved supplier rejected; over-limit PO routed to manager; tool blocked for missing scope; no supplier qualifies so workflow stops before writes; process killed and resumed without duplicates; duplicate detection ignored.

**2. MODEL.md:** kept, changed, left out, and why.

**3. README:** how to run; how to add a tool, provider, detector, workflow (short recipes; if it takes many steps across many files, the harness isn't as extendable as you think); what you cut and why (a real section, not a footnote). Tuesday-check note fits here.

**4. Design doc.**

**5. Tests (minimum):**

- Gate: missing scope blocks; over-threshold routes to manager; backup routing when approver out and request unanswered
- Trigger dedupe: detector twice, one item
- Workflow resumption: crash, restart, finish, no duplicate PO
- Use a **fake LLM** so tests are fast, free, deterministic. The gate test passing without a model proves "enforced in code."

**6. Recorded Scenario A run** showing approval prompt, execution, and audit trail (e.g. `runs/scenario_a.txt`). Standout version: an `explain` command that prints the audit log as a story:

```
[9/2 08:00] stockout detector: P-4471 runs out 9/7; order 4812 starts 9/7
[9/2 08:00] context for u-101: PO-77812, M-001 (slip to 9/8), E-002 (OOO 9/3-9/4)
[9/2 08:01] planner: reroute_po to S-Z, 400 units. Reason: ...
[9/2 08:01] gate: allowed ($18,600 < $25,000), approver u-101
[9/2 17:00] escalation: unanswered, u-101 OOO 9/3, routed to backup u-102
[9/3 09:12] approved by u-102
[9/3 09:12] step 3 create_po: PO-80021 created ...
```

**No UI.** Time on a web interface is time not spent on what's graded.

---

## 8. Declared vs. free-form

### The two execution paths in your build

The system is one agent loop with two ways to execute:

- **Declared path (Scenario A):** planner picks `reroute_po` and fills params; six steps run in fixed order.
- **Free-form path (Scenario B):** no workflow exists; planner proposes tool calls; each goes through the gate.

The PDF requires both "so both paths stay exercised."

### The rule

> Declared workflows are mandatory wherever they exist. Free-form covers situations no workflow addresses. A workflow failing ends in a report to a human, never in improvisation.

- The planner always runs first and decides what kind of response is needed.
- If a declared workflow covers it, it **must** be used (enforced by workflow-only tool flags).
- Free-form is not a "backup" for broken workflows. If the reroute stops because no supplier qualifies, do **not** fall back to free-form and create a PO another way. That would defeat purchasing's rule. Stop and tell Dana.
- Scenario B runs free-form simply because nobody wrote a lot-reallocation workflow.

### How free-form works

The planner gets the tool catalog (names, descriptions, schemas), the item, and context, and returns an ordered plan:

```json
{
  "plan": [
    {"tool": "reallocate_lot", "args": {"prod_order_id": "4820", "from_lot": "L-2093", "to_lot": "L-2101", "qty": 100}},
    {"tool": "notify_user", "args": {"user_id": "u-301", "message": "..."}}
  ],
  "reasoning": "..."
}
```

Harness: validate (tools exist, args match, no workflow-only tools); gate each call; human approves the whole plan; tool runner executes in order, stopping and compensating in reverse on failure.

**Plan up front vs step by step:**

- **Plan up front:** model proposes the whole sequence, human approves, then it runs.
- **Step by step** (Claude Code style): model calls a tool, sees the result, decides the next.

Use **plan up front** for writes, since approval must come before any write; with step by step the human would approve a plan that doesn't exist yet. Step by step is fine for reads, but providers already gather context first, so skip it. Explain in one README sentence.

### Declared vs free-form, compared

"Random" isn't the right word; neither path is random. The difference is what the model gets to choose.

Scenario A model output:

```json
{
  "workflow": "reroute_po",
  "params": { "part_id": "P-4471", "original_po_id": "PO-77812",
              "qty": 400, "needed_by": "2026-09-07" },
  "reasoning": "Supplier Y slipped to 9/8, order 4812 starts 9/7..."
}
```

| | Scenario A (declared) | Scenario B (free-form) |
|---|---|---|
| Which steps | fixed | model picks from catalog |
| How many | always 6 | 2 here, could be 1 or 4 |
| Order | fixed | model decides |
| Could it forget a step? | no | yes (reallocate but never notify) |
| Could it add a step? | no | yes (also email the supplier) |

Across runs, B might notify before reallocating, split across two lots, or reallocate and also flag a shortage "just in case."

**What still constrains free-form:** only catalog tools; args match schemas; gate checks every call (quality manager has lot scopes but not `erp:po:create`, so a proposed PO is blocked); human approves the whole plan; optional post-checks in code ("if a reallocation happened, a supervisor notification must exist").

**Promotion idea:** if B always ends up "reallocate, then notify," that pattern is stable enough to declare. Once declared, nobody has to trust the model to remember the notify step.

### Approval in both designs

Human approval exists in both because the business requires it.

- **Design 1:** approval is a checkpoint between planner and executor.
- **Design 2:** approval is a box in the flowchart ("wait for approval, escalate to backup if unanswered and OOO"), with approved and rejected branches drawn. Arguably cleaner because it's visible in the definition.

Declared workflows have humans approving **parameters**; free-form has humans approving **whole plans**. The less the path was declared ahead of time, the more the human has to review.

### The design question: who's the boss?

> If you were designing a deterministic workflow engine from the start, where reasoning is just a small node inside the graph, would you still build it the way you did here?

**Design 1: the LLM is the boss (what you build).** The agent loop decides what to do; workflows are specialized tools it can pick up. Analogy: a general contractor who must call a licensed electrician for electrical work. The contractor decides when; the electrician follows code exactly.

**Design 2: the workflow is the boss (only written about).** A big flowchart of company processes with every path drawn in advance. The LLM is one kind of box, called for judgment. Analogy: an assembly line with an inspector at one station; the inspector has judgment but doesn't decide what the line does next.

Scenario A under Design 2:

```
stockout detected
  box: gather ERP, mail, calendar
  box (LLM): "read the email, is the shipment late? yes/no + new date"
  box: compare new date to production start
  box (LLM): "reroute, wait, or partial? pick one"
     if reroute: confirm supplier, check lead time, create PO, ...
     if wait:    notify production of risk
     if partial: ...
```

**Tradeoff:**

- Design 1 is more flexible: handles undrawn situations (B has no workflow). Cost: the model still decides at the top level whether to use a workflow.
- Design 2 is more predictable: every path exists in advance, reviewable, testable, auditable. Cost: undrawn cases need a fallback, and someone maintains all the flowcharts.

**Having an opinion means picking a side.** Example answer:

> For a manufacturer, I'd lean toward the workflow being the boss. Most purchasing and quality processes are known, repeated, and audited, and the customer told us explicitly they don't want improvisation. I'd make the free-form planner one more node: a fallback that runs when no declared workflow matches, whose output is a proposal a human reviews. Over time, situations that keep hitting the fallback become candidates for new declared workflows, so the system gets more deterministic as it learns.

Key line: **the free-form path is where workflows get discovered; the workflow engine is where they get trusted.** Arguing for Design 1 early on (when processes aren't known yet) is also defensible. What matters is choosing and justifying.

Supporting line: "Scenario B runs free-form per the spec, but its behavior is predictable enough that after observing it a few times, I'd promote it to a declared workflow."

---

## 9. Detectors in detail

```python
@dataclass
class AttentionItem:
    detector: str          # which detector raised it
    dedupe_key: str        # identity of the condition
    owner_role: str        # who should handle it
    summary: str           # one line for humans and the audit log
    facts: dict            # numbers that triggered it

class Detector(Protocol):
    name: str
    def detect(self, db, clock) -> list[AttentionItem]: ...
```

**Stockout detector (A):**

```python
class StockoutDetector:
    name = "stockout"
    HORIZON_DAYS = 7

    def detect(self, db, clock):
        today = clock.today()
        items = []
        for part in db.parts():
            if part.daily_usage == 0:
                continue
            runout = today + timedelta(days=part.on_hand / part.daily_usage)
            if runout > today + timedelta(days=self.HORIZON_DAYS):
                continue
            for order in db.production_orders_consuming(part.part_id):
                if order.status == "planned" and order.scheduled_start >= runout:
                    items.append(AttentionItem(
                        detector=self.name,
                        dedupe_key=f"stockout:{part.part_id}:{order.prod_order_id}",
                        owner_role="Purchasing Manager",
                        summary=f"{part.part_id} runs out {runout}, order "
                                f"{order.prod_order_id} starts {order.scheduled_start}",
                        facts={"part_id": part.part_id, "runout": runout,
                               "prod_order_id": order.prod_order_id,
                               "inbound_pos": db.open_pos_for(part.part_id)},
                    ))
        return items
```

**Key insight:** the ERP still says PO-77812 is promised for 9/4, which would be in time. The slip exists **only in the email**. The detector can only flag the risk ("this order rides on an inbound PO with thin margin"). The LLM confirms the problem by reading the email. Code detects risk from structured data; the LLM confirms the problem from unstructured text. Worth a README sentence.

**Dedupe:** detectors run every tick, so the same condition reappears. `dedupe_key TEXT UNIQUE` rejects the duplicate. What goes in the key is a judgment call: `stockout:P-4471:4812` means one alert per part per order ever; including the inbound PO id allows a fresh alert if the situation materially changes. State your choice.

**Quality hold detector (B):** same contract, different rule: allocations on held lots where the order starts within 3 days. Owner role: Quality Manager.

**Registry:** `DETECTORS = [StockoutDetector(), QualityHoldDetector()]`

**Schedule vs event:** a detector is just a function, so call all detectors on each tick and also after any tool writes to the ERP. The tick version satisfies the requirement; mention ERP webhooks in the real-systems section.

---

## 10. Providers in detail

```python
@dataclass
class ContextSlice:
    source: str            # "erp", "mail", "calendar"
    records: list[dict]    # what goes to the planner
    record_ids: list[str]  # what goes to the audit log

class Provider(Protocol):
    source: str
    def fetch(self, user: User, item: AttentionItem) -> ContextSlice: ...
```

Two decisions per provider:

- **What's allowed (scoping):** check scopes first; filter rows (Dana's mail provider only reads messages where Dana is a recipient). The LLM never sees forbidden data.
- **What's relevant (filtering):**
  - ERP: the part, its open POs, the production order, suppliers approved for that part
  - Mail: messages from contact emails of suppliers on those POs, or with the PO id in the subject (noise dropped here)
  - Calendar: Dana's events for the next few days

Relevance filtering is simple and deterministic; it just shrinks the haystack.

**`record_ids`** let the audit log record exactly what the agent saw (M-001, PO-77812, E-002) without dumping full bodies.

**Scenario B:** extend ERP with lots, or add a `QualityProvider` mapped to its own scope (`erp:lot:read`). Separate is slightly cleaner.

---

## 11. Build vs. write

| Topic | Build | Write about |
|---|---|---|
| Detectors, providers, planner, gate, tools, audit | yes | |
| Workflow engine (order, idempotency, compensation, resume) | yes | |
| Scheduler + clock | yes | |
| Scenarios A and B end to end | yes | |
| Permissions | simple: users, scopes, thresholds in DB | SSO, token exchange, no standing credentials |
| Memory | simple: run state + facts table | promotion, accuracy, staleness |
| Workflow versioning | store version per instance | in-flight handling across version changes |
| Scaling | no | yes |
| Workflow-engine-first design | no | yes |
| Real ERP / Microsoft Graph | no | optional |
| Tracing and evaluation | no (audit log suffices) | optional |

Pattern: build the toy version that proves the idea, write about what production needs. Actually implementing SSO, Postgres, or Temporal would blow the time box and read as poor judgment.

---

## 12. Concise build checklist

**Data (one SQLite file)**
- ERP: parts, suppliers, POs, production orders, lots, lot allocations
- Mail, calendar, users with scopes and approval limits
- Seed data for A and B, with noise and a trap supplier
- Harness tables: clock, attention items, approvals, workflow instances, scheduled tasks, executed actions, memory facts, audit log

**Harness**
- Detectors: stockout (A), quality hold (B), with dedupe keys
- Providers: ERP, mail, calendar (plus lots), scoped to the user
- Planner: real LLM call returning a workflow request or a tool plan
- Gate: scopes, PO threshold, approval routing, backup escalation
- Tools: create PO, reduce/cancel PO, notify user, schedule check, reallocate lot, flag shortage
- Workflow engine: `reroute_po` in fixed order, saves after each step, resumes, compensates
- Tool runner: executes free-form plans in order
- Scheduler: fires tasks when the clock reaches them
- Audit log: append-only, plus `explain`
- Clock: advanceable, only source of "today"

**Demo command:** Scenario A, Tuesday follow-up, Scenario B, failure cases

**Tests:** gate, dedupe, workflow resume

**Docs:** README, MODEL.md, design doc, recorded Scenario A run

---

## 13. Stack

| Need | Pick | Why |
|---|---|---|
| Language | Python 3.11+ | Familiar, good LLM SDKs |
| Database | `sqlite3` (built in) | No server, one file |
| Schemas and validation | Pydantic | Tool schemas, LLM output validation, JSON schema generation |
| LLM | Anthropic or OpenAI SDK with tool use / structured output | Forces JSON matching your schema |
| CLI | Typer (or `argparse`) | `demo`, `approve`, `tick`, `explain` |
| Tests | pytest | Standard |
| Nice output | Rich (optional) | Readable recorded run and approval prompt |
| Env setup | `uv`, or `venv` + `requirements.txt` | Keeps "one command" simple |

**Avoid agent frameworks** (LangChain, LangGraph, CrewAI) and workflow engines (Temporal). The assignment grades you on building the harness; a framework hides exactly those decisions. Mention Temporal in the design doc as the at-scale choice. Plain SQL over an ORM keeps the schema visible. Aim for four or five dependencies.

### Pydantic

Describes data shapes as classes and **checks real data matches at runtime** (plain type hints don't).

```python
class CreatePOArgs(BaseModel):
    part_id: str
    supplier_id: str
    qty: int
    unit_price: float

CreatePOArgs(part_id="P-4471", supplier_id="S-Z", qty="lots", unit_price=46.50)
# ValidationError: qty, input should be a valid integer
```

Three jobs here:

1. **Validate LLM output:**
   ```python
   class WorkflowRequest(BaseModel):
       workflow: Literal["reroute_po"]
       params: RerouteParams
       reasoning: str

   plan = WorkflowRequest.model_validate_json(llm_output)  # raises if malformed
   ```
   `Literal` means the model can only name workflows that exist.
2. **Tell the LLM the shape:** `CreatePOArgs.model_json_schema()` becomes the tool definition. One class is both the instructions and the check.
3. **Define tool inputs** so the gate and runner validate args before running.

### CLI (Typer)

CLI = command line interface. No UI is needed; "a CLI or HTTP endpoint for the approval step is fine."

```bash
python -m harness demo                      # run the whole story
python -m harness tick                      # advance the clock one day
python -m harness approve A-001 --as u-102  # approve a pending request
python -m harness explain                   # print the audit log as a story
```

```python
import typer
app = typer.Typer()

@app.command()
def approve(approval_id: str, as_user: str = typer.Option(..., "--as")):
    harness.approvals.approve(approval_id, approver=as_user)

@app.command()
def tick(days: int = 1):
    harness.tick(days)

if __name__ == "__main__":
    app()
```

Typer builds commands (and `--help`) from function arguments and type hints; `argparse` is built in with more boilerplate.

- **demo:** the one documented command; resets DB and runs everything
- **approve / reject:** the human approval step
- **tick:** advances the clock, firing detectors, escalations, scheduled checks
- **explain:** prints the audit log as a story

### Rich

Makes terminal output readable (colors, boxes, tables). Doesn't change behavior.

```python
from rich.console import Console
from rich.panel import Panel

console = Console()
console.print(Panel(
    "Part P-4471 will likely cause production order 4812 to miss its start.\n"
    "Supplier Y says the shipment slips to Tuesday 9/8.\n\n"
    "Proposed: reroute 400 units to Supplier Z ($18,600) and notify production.\n\n"
    "approve A-001  |  reject A-001",
    title="Approval needed: u-101 (Dana Whitfield)",
    border_style="yellow",
))
```

Cheap polish on a deliverable reviewers actually read.

### Environment setup

- **`venv` + `requirements.txt`:**
  ```bash
  python -m venv .venv
  source .venv/bin/activate
  pip install -r requirements.txt
  python -m harness demo
  ```
  Wrap in a `Makefile` so it's `make demo`.
- **`uv`:** dependencies in `pyproject.toml`; `uv run python -m harness demo` creates the environment and installs on first run. Truly one command.

Test from a fresh clone before submitting.

---

## 14. Lines worth putting in your docs

**README**
- Tuesday check: scheduled at the new PO's promised date, before production start; demo still advances to Tuesday.
- Detector flags risk from structured data; the LLM confirms the problem from the email.
- Plan-up-front for free-form because approval must precede writes.
- Compensation for irreversible actions (sent notifications) is a follow-up correction.
- Adding Scenario B: new detector, provider extension, two tools, a user, two tables; no core edits (or honest explanation if there were).
- Recipes for adding a tool, provider, detector, workflow.
- What you cut and why.

**MODEL.md**
- Replaced `allocated_to` with an allocations table (quantities needed).
- daily_usage treated as background demand; production orders as discrete demand.
- Trap supplier and noise records, and what each tests.
- Left out: inventory locations, multi-currency, partial receipts, BOM hierarchies.

**Design doc**
- Writes use tokens minted at approval time; detection uses narrow read-only identity.
- Memory informs, never overrides; promote on outcome.
- Alert fatigue as a scaling concern.
- Free-form is where workflows get discovered; the workflow engine is where they get trusted.
- Scenario B is predictable enough to promote to a declared workflow.
- In-flight instances finish on their original version.

---

## 15. Decisions you still need to make

- Cancel vs reduce the original PO (and how the planner justifies it)
- Arrival check date logic vs the literal Tuesday requirement
- Whether daily_usage includes scheduled production demand
- What goes in each dedupe key
- Extend the ERP provider vs a separate quality provider
- Exact trap supplier (unapproved, or approved but too slow)
- Seed data variants so both B outcomes and all failure cases are reachable
- Which side you take on the workflow-engine design question
- `uv` vs `venv` + Makefile
- Which LLM API to use
