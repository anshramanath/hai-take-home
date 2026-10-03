# MODEL.md

What was modeled, what changed from the assignment's sample schemas, and why. Full schema
is in `harness/world/schema.sql`; fixtures are in `harness/world/seed.py`.

## What was kept from the sample schemas

The appendix's four ERP entities (parts, suppliers, purchase orders, production orders),
the mail and calendar shapes, and the users-and-permissions shape are all kept close to
the sample, field for field, with minor renames for consistency (`end` instead of
implicit end times, `out_of_office` as an explicit int flag rather than inferred from the
title). The clock's single `{"today": ...}` row is unchanged.

## What changed or was added, and why

- **`erp_lot_allocations` replaces the sample's `allocated_to` array** on a lot. An array
  of production order ids can't carry a quantity, and Scenario B's "covers" case needs to
  split one held lot's demand across two released lots by specific amounts (70 to one, 30
  to another). A join table with `(lot_id, prod_order_id, qty)` can.
- **`erp_receipts` is new.** The arrival-check follow-up needs a fact to check against —
  "did the shipment actually show up" has to be answered from something other than the
  PO's own promised date, or the check would just be re-reading the same optimistic number
  it's supposed to be verifying.
- **`notifications` is new**, as the landing table for `notify_user` / `send_correction`.
- **`daily_usage` means background consumption only**, not inclusive of known production
  demand. Production orders are separate, discrete subtractions on their own
  `scheduled_start` date. Treating `daily_usage` as "everything the part is used for"
  would double-count the exact demand the detector is trying to project against.
- **The stockout detector's inbound-PO window is `today <= promised_date <=
  scheduled_start`, inclusive of today.** Found by actually running the Tuesday follow-up,
  not designed in advance: a PO promised *today* should still read as on-time to a
  detector that only trusts structured ERP data (that's the whole premise of Scenario A —
  the ERP doesn't know about the slip, the email does); excluding it at exactly that
  boundary produced a spurious second risk the moment the clock reached the replacement
  PO's own promised date. See `harness/detection/stockout.py`'s docstring and
  `BUILD_LOG.md` phase 5 for the full story.
- **Dedupe keys are designed to re-trigger legitimately.** `stockout:{part_id}:
  {prod_order_id}:{inbound_po_id}` includes the specific PO the risk depends on, so once a
  reroute replaces PO-77812 with a new PO from Supplier Z, a fresh risk against *that* PO
  gets a different key and can alert independently — the uniqueness constraint dedupes
  "the same known problem," not "any problem with this part." `quality_hold:{lot_id}:
  {prod_order_id}` and `shortage:{part_id}:{prod_order_id}` follow the same logic at
  their own granularity.
- **Harness state tables** (`attention_items`, `runs`, `approvals`, `workflow_instances`,
  `scheduled_tasks`, `executed_actions`, `memory_facts`, `audit_log`) are new — the
  appendix only modeled the company, not the agent's own bookkeeping. Each exists for a
  specific requirement: `attention_items` for detection (with a `status` lifecycle,
  `open -> planned`, added once more than one source could raise an item in the same
  tick — see `BUILD_LOG.md` phase 5); `runs` for what one attempt at resolving an item
  knows, as distinct from what persists; `approvals` for the frozen, hashed plan;
  `workflow_instances` for the declared-workflow engine's resumable state;
  `scheduled_tasks` for deferred follow-up; `executed_actions` for idempotency;
  `memory_facts` for confirmed-outcome learning; `audit_log` for the append-only trail
  everything else writes to.
- **`flag_shortage` resolves its own `owner_id`** rather than taking it as an argument —
  a correction made once the free-form planner actually had to call it (phase 6): a model
  has no reliable way to know Dana's internal `user_id` from context alone. The tool
  resolves "the Purchasing Manager" itself, the same role-fallback every detector uses.
- **`CreatePoArgs` takes a caller-supplied `po_id`** rather than generating one inside the
  tool. The workflow needs the new PO's identity to be part of the frozen, approved plan
  (the notification text and the arrival-check's payload both reference it), and nothing
  may be computed between approval and execution — including an id.

## Seed design: noise and traps, and what each tests

**Scenario A** (`scenario_a`, clock 2026-09-02):
- **S-Q (Bargain Motion)**: cheaper and faster than the right answer, but not approved for
  P-4471. Tests that the workflow's `confirm_supplier_approved` step rejects it even
  though the free-form-style temptation (lowest price) is strong, and that the planner's
  context includes it (so excluding it is the *workflow's* job, not the provider's).
- **S-W (Westline Supply)**: approved for P-4471, but a 9-day lead time misses the need
  date. Tests `confirm_lead_time` specifically, as a distinct rejection reason from S-Q's.
- **S-N**: approved only for other parts, included in ERP context as ordinary noise.
- **M-002 through M-004**: a newsletter, an unrelated supplier's pricing email, an internal
  note — none mention the relevant PO or come from the relevant supplier, testing
  `MailProvider`'s relevance filter.
- **M-005**: from the relevant supplier, mentioning the relevant PO, but addressed to
  Marcus, not Dana. The one noise message that *would* pass the relevance filter if
  mailbox membership weren't checked first — proves scoping happens before relevance.
- **PO-77900, PO-77901, production order 4900**: unrelated open POs and a production order
  for a different, ample-stock part, testing that the detector and `ErpProvider` don't
  spill irrelevant records into the one item that matters.
- **E-003**: another user's calendar event, testing `CalendarProvider` returns only the
  requesting user's own events.

**Scenario A failure variants** (each a one-field mutation of the base fixture, so they
can never drift from it in some unrelated way): `scenario_a_no_supplier` (S-Z loses
`approved_parts` for P-4471, leaving zero candidates), `scenario_a_over_limit` (4812's
P-4471 requirement raised to 700, pushing the reroute's value past Dana's limit),
`scenario_a_backup_low_limit` (Priya's limit dropped below the reroute's value, forcing
escalation past her to Marcus), `scenario_a_no_arrival` (identical data to `scenario_a` —
the "no arrival" behavior is entirely about a test or demo run not calling the
receipt-recording helper, never a seed-level difference).

**Scenario B** (`scenario_b_covers` / `scenario_b_shortage`, clock 2026-09-02): L-2093 is
on hold, allocated 100 units to order 4820 (starts 2026-09-05, within the 3-day horizon).
L-2115 is released with 30 free units (80 total, 50 already allocated elsewhere, to order
4831 — included so `QualityProvider`'s free-quantity math has something non-trivial to
compute, and so 4831's distant start date, 2026-09-15, tests that `QualityHoldDetector`
doesn't fire for an order outside its own window). L-5500/L-3000 is an unrelated
lot-tracked part and lot, pure noise, proving the provider scopes by part. The two
variants differ in exactly one new lot: `scenario_b_covers` adds a 70-unit released lot
(70 + 30 free = 100, covers 4820 exactly via a split); `scenario_b_shortage` adds a
60-unit one instead (60 + 30 = 90, ten short).

## Left out, and why

Inventory locations, units of measure, multi-currency, partial receipts beyond a simple
quantity-and-date record, BOM hierarchies, supplier contracts, and lot expiry are all
absent. None of them are load-bearing for either scenario's actual decision, and each
would add a table and a join without adding a test the assignment asks for. The guidance
to keep this "a handful of entities," not "a fake ERP with forty tables," was taken
literally: every table in `schema.sql` earns its place by being read or written by a
specific detector, provider, tool, or policy rule that's actually exercised.
