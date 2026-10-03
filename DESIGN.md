# DESIGN.md

Written in the first person, for the parts of the assignment I didn't build. Sections 1-3
are required; 4 is required by Part 2; 5-6 are optional and kept brief.

## 1. Identity and authorization

A real deployment establishes who the user is through the company's existing identity
provider — Okta, Entra ID, whatever SSO the company already runs — and the harness never
sees a password. What it holds instead is a short-lived session token for the employee,
obtained the normal web-app way (OIDC/SAML login), scoped to that one person.

The harder question is what happens when the agent needs to call ERP, mail, and calendar
*on that person's behalf*. The harness should never hold a standing service account with
broad write access across all three systems — that's a single point of compromise with the
blast radius of every employee it serves. The right shape is token exchange (OAuth 2.0
Token Exchange, or on-behalf-of flows where the identity provider supports them): the
harness presents the user's own session token to the identity provider and gets back a new,
narrowly-scoped, short-lived token for *one specific downstream system*, carrying the same
user's identity. ERP calls use an ERP-scoped token; mail calls use a Graph token scoped to
that mailbox; and so on. The downstream system enforces its own permissions against that
token — the harness's gate is defense in depth, not the only lock on the door.

This maps cleanly onto what's built: `users.scopes` in this harness stands in for the
claims that would actually ride inside such a token (`erp:po:create`, `mail:read`, and so
on). `world/users.py`'s `User` dataclass is what a decoded token would deserialize into.

Two identities matter, and the harness already keeps them structurally apart. The agent's
own *background* work — detection, scanning for risk — runs under a narrow, read-only
service identity with no write capability anywhere, since nothing in that phase should ever
need one. A *write* only ever happens with a token minted at the moment a human approves
it, so the approval click is literally what authorizes the downstream call, not something
the harness decided to do quietly in the background. And `policy/approvals.py`'s
escalation check — reading the approver's own calendar to decide if they're out of office —
runs under a separate, audited *policy* identity, never the context-provider path, and its
result is never shown to the model. That split (user-visible context vs. policy-only reads)
is already real in this codebase, not just a design-doc aspiration: `context/calendar.py`
and the direct `cal_events` query inside `approvals.py` are two different code paths on
purpose.

## 2. Long-term memory

What's built: run memory (`runs.state`) is everything the *current* attempt at resolving
one attention item knows — the gathered context's record ids, the item's own facts, which
approval it's waiting on. It's scratch space, thrown away in spirit once the run reaches a
terminal status, even though the row itself persists for audit. Persistent memory
(`memory_facts`) is a much smaller, much more deliberate set of structured facts, written
at exactly two points: a workflow reaching `completed`, and an arrival check confirming a
receipt. Both are *confirmed outcomes* — something actually happened — not predictions or
in-flight reasoning. Each fact carries `source_ids` back to the record that justified it,
and an optional expiry.

The reason for promoting only on confirmed outcomes: a plan that's merely been proposed, or
even approved but not yet executed, could still be rejected, fail, or get compensated. If I
wrote a fact the moment the planner said "Supplier Y slipped," and that run then got
rejected or crashed uncompensated, the harness would carry forward a belief about the world
that never actually came to pass. Waiting for completion means every fact in the table is
true of something that genuinely happened in a system of record, not something an LLM once
said.

Keeping it accurate over time is mostly about *not* writing much. With two trigger points
and a handful of facts per scenario, there's little to go stale. The real risk is a fact
outliving its relevance — "Supplier Y slipped PO-77812" is useful context for a few weeks,
not forever — which is what `expires_in_days` is for; `facts_for_prompt()` filters anything
past its expiry before it ever reaches a prompt. A larger deployment would need a stronger
notion of staleness than a TTL — ideally, invalidating a fact when the system of record it
was derived from changes again (a new email from the same supplier, a corrected receipt) —
but that requires exactly the kind of event-driven invalidation section 3 below argues for
anyway.

The one invariant that matters most: memory is a *hint*, never a substitute for reading the
live system. `facts_for_prompt()`'s output is labeled `memory_hints` in the prompt payload
and nowhere else — the gate, the workflow engine, and the free-form runner never read
`memory_facts` at all. A planted fact that's flatly wrong (tested directly: "S-Z is not
approved and has a 10-day lead time," which is false) cannot change what `reroute_po`'s own
`confirm_supplier_approved` step concludes, because that step queries `erp_suppliers`
itself, every time, regardless of what the prompt said. Memory can mislead a *recommendation*;
it cannot corrupt a *decision* a human didn't actually make, because the gate and the
workflow's own checks never consult it.

## 3. Scaling to thousands of employees

In the order I'd expect this design to actually break:

**Detectors scanning per tick.** Right now every detector re-scans the entire ERP on every
tick, for every user implicitly covered by whatever it finds. At thousands of employees
and real transaction volume, a full-table scan per tick per detector stops being free. The
fix is event-driven detection: detectors subscribe to ERP change events (a PO's promised
date changes, a lot's status flips to hold) and evaluate only the records that changed,
plus whatever they're joined to, running once company-wide rather than once per tick
regardless of load. Ownership resolution (who the item belongs to) already exists and
doesn't need to change — only *when* a detector runs does.

**SQLite and one process.** The whole harness is one file and one connection right now.
That's the first hard wall: SQLite doesn't give concurrent writers across processes the way
this needs once more than a handful of agents are acting at once. Postgres (or a managed
equivalent) with a real connection pool is the direct replacement; the schema barely
changes since the appendix-derived tables were never SQLite-specific to begin with.
Detection, planning, and execution become worker processes pulling off a queue instead of
one Python process doing everything serially inside `tick()`.

**The in-process scheduler and workflow engine.** `scheduling/tasks.py` and
`execution/engine.py` both assume a single process that's always running `tick()` to notice
due work. At scale, a durable external workflow system (Temporal is the obvious choice,
given the shape of what's already here — a fixed step sequence with compensations) replaces
both: the engine's actual step logic barely changes, since it's already written as discrete
steps with explicit compensations, but durability and retries stop being this codebase's
problem.

**LLM cost and rate limits.** Thousands of employees each generating attention items would
multiply real API calls fast. The mitigations are: only call the model when a detector
actually raised something (already true), keep prompts as small as the current ones (no
redundant history, just the one item's context), cache prompt prefixes where the catalog
content repeats across calls, and consider a smaller/cheaper model for the bounded workflow
steps specifically, since `choose_supplier` and `draft_notification` are much narrower
tasks than the free-form planner's.

**Downstream API limits.** Real ERP/Graph APIs rate-limit per app and sometimes per user.
Caching read-heavy provider calls and moving to webhook-driven context refresh (rather than
re-fetching on every gather) buys headroom here the same way event-driven detection does
above.

**Human attention.** This is the one that doesn't have a code fix. At enough scale, routing
every approval straight to one person's inbox produces exactly the alert fatigue the
assignment's escalation rule is a small defense against. The real answer is prioritization
(dollar value and risk, already half-present in the threshold rule) and digesting —
batching low-stakes approvals into a daily summary rather than a ping per item — which is a
product decision more than an architectural one, but it's the actual bottleneck once the
infrastructure above is solved.

## 4. Workflows: versioning, and the workflow-first question

**Versioning**, as built: an instance records its own `(name, version)` at creation and
always resumes against that exact version, even after a newer one is registered —
`test_part2_definitions_are_versioned_instances_keep_their_own_version` is the proof. My
default policy for handling in-flight instances across a version change would be pinning:
new instances pick up the new definition; instances already running finish on the version
they started on; the old definition stays registered until its last instance completes.
The reason this has to be the default, not just a convenience: approval binds to a frozen
plan built under a specific version's step sequence (invariant 3 — the hash is over the
plan the *old* version produced). Switching an in-flight instance to a new version
mid-stream would mean either re-approving under the new plan or silently running steps
nobody approved, and neither is acceptable. For an urgent fix to a declared workflow
(a wrong step, a compliance issue), I'd handle it in two tiers: cancel and re-plan any
instance that hasn't been approved yet (cheap, nothing executed), and only migrate an
already-approved, already-executing instance if the fix specifically affects steps it
hasn't reached yet — and even then, require a fresh approval for the remaining steps, with
the migration itself audited as its own event, not a silent swap.

**Would I build it the same way starting from scratch, with reasoning as a small node
rather than the thing driving it?** Partly, and the "partly" is the honest answer. What's
built already leans in that direction for the one declared workflow: `reroute_po` really
is in charge of its own step order once entered, the two LLM steps are genuinely bounded
(one picks from a pre-filtered list and must justify it; the other drafts free text with no
field for a code-owned fact), and nothing about step order is negotiable — `WorkflowRequest`
doesn't even have a `steps` field for a model to try to fill in. But the *decision to enter
that workflow at all*, the choice between a declared workflow and a free-form plan, the
approval wait, the escalation, and the follow-up all live in an agent loop wrapped around
the engine, not inside a graph the engine itself owns.

Starting workflow-first, I'd make the whole lifecycle a declared graph, triggered by a
detector's event type rather than by a planner's free-form decision to "enter" it: a
detector firing is the start node, every LLM call becomes a typed node with an enumerated
output ("delayed" or not; reroute, wait, or partial-reroute; one of N pre-approved
suppliers), approval-wait and escalation become durable timers that are themselves part of
the graph rather than something a human-coded `tick()` loop polls for, and the arrival
check is a scheduled node with its own typed outcome feeding back into the same graph
rather than a side door that raises a brand-new attention item. Free-form would still need
to exist, but demoted to an explicit fallback branch — "no declared graph matches" — gated
behind full human review rather than treated as a first-class second path.

My actual position: for a manufacturer with known, audited, repeatable processes,
workflow-first is the right production design, and I'd build toward it given more time.
Agent-first — a loop with one declared workflow and one free-form path bolted onto two
sides of the same gate — was the right choice *for this exercise* specifically, because the
assignment requires exercising both a fixed-order path and a free-form path, and a loop
around a single workflow engine is a smaller, faster thing to build correctly in a three-day
box than a general declared-graph runtime would be. The free-form path is also where new
workflows get *discovered* in practice — Scenario B's reallocation is predictable enough,
in hindsight, to be its own declared workflow (confirm the lot is genuinely unusable,
confirm a covering combination exists, reallocate, notify — the same shape as
`reroute_po`), and promoting it would be the next concrete step past this exercise, not a
hypothetical one.

## 5. Connecting real systems

The provider and tool interfaces (`fetch(ctx, user, item) -> ContextSlice`,
`Tool.run(db, args, ctx) -> dict`) don't need to change shape to point at real systems —
`ErpProvider` becomes a thin client over SAP or NetSuite's API, `MailProvider` and
`CalendarProvider` become Microsoft Graph calls, and a document-store-backed provider for
internal knowledge slots in the same way `QualityProvider` did for Scenario B. What does
change: pagination (a real inbox or ERP result set doesn't fit in one call the way a
seeded fixture does), rate limits and backoff, and staleness — a provider result is a
snapshot of a remote system's state at call time, not guaranteed current by the time a
write happens, which is exactly what the execution-time re-check already defends against
for permissions and would need to extend to for data freshness. Real idempotency keys would
be built the same way they are now (`{run_id}:{instance_id}:{step}`) but passed through as
the downstream system's own idempotency or external-reference field where the API supports
one, so a retried call is provably a no-op on the far end too, not just locally.
Compensation gets harder: `cancel_po` assumes the fake ERP lets a PO be cancelled cleanly;
a real supplier-facing PO might already be in flight, making "compensation" a genuine
reversing transaction (a credit, a return) rather than a clean undo.

## 6. Observability and evaluation

I'd trace every stage of the loop as spans under one run id: detection (what fired, from
what data), context gathering (which providers ran, what record ids came back), the
planner call (prompt size, latency, the raw proposal), the gate decision, the approval's
full lifecycle (requested, escalated, decided, by whom), and each execution step with its
idempotency key and result — which is almost exactly what the audit log already captures
structurally, just not yet exported as spans. "The agent did a good job" would mean:
approve/reject/edit rates on its proposals, false-alarm rate on detections (items raised
that a human dismissed as not real), time from detection to resolution, and — the one that
actually matters most — whether the predicted outcome happened (did the replacement
actually arrive on time; did the reallocation actually prevent a missed production start).
To catch a regression before users do, I'd keep a fixed library of scenarios (this harness's
Scenario A and B are the first two) and replay them against every prompt or model change,
diffing the resulting proposals and gate outcomes against a known-good baseline — the same
fixture-and-fake-LLM pattern this test suite already uses, just run continuously against
candidate prompts instead of only against already-correct code.
