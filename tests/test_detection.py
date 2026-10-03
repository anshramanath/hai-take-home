"""Section 15.7: detectors and dedupe. QualityHoldDetector arrives with
Scenario B in phase 6; this file covers StockoutDetector only.
"""

from __future__ import annotations

import json

from harness.detection.registry import run_detectors
from harness.execution.args import ReducePoArgs
from harness.execution.catalog import get_tool
from harness.execution.executor import execute
from harness.execution.tools import ToolContext


def _ctx(run_id="run-1", step="step-1"):
    from datetime import date

    return ToolContext(run_id=run_id, step=step, today=date(2026, 9, 2))


def test_stockout_fires_for_4812_via_thin_margin(make_harness):
    h = make_harness("scenario_a")
    created = run_detectors(h.conn, h.clock)

    assert len(created) == 1
    row = h.conn.execute("SELECT * FROM attention_items WHERE item_id = ?", (created[0],)).fetchone()
    assert row["dedupe_key"] == "stockout:P-4471:4812:PO-77812"
    assert row["owner_id"] == "u-101"
    facts = json.loads(row["facts"])
    assert facts["condition"] == "thin_margin"
    assert facts["supplier_id"] == "S-Y"


def test_does_not_fire_for_noise_orders_with_ample_stock(make_harness):
    h = make_harness("scenario_a")
    run_detectors(h.conn, h.clock)
    dedupe_keys = [r[0] for r in h.conn.execute("SELECT dedupe_key FROM attention_items")]
    assert not any("4900" in key for key in dedupe_keys)


def test_short_condition_with_no_inbound_po_fires(make_harness):
    h = make_harness("scenario_a")
    # P-2210 (4812's other component) has no open PO due within the window;
    # drop its stock low enough that background usage alone exhausts it.
    h.conn.execute("UPDATE erp_parts SET on_hand = 0 WHERE part_id = 'P-2210'")
    h.conn.commit()

    created = run_detectors(h.conn, h.clock)

    rows = [h.conn.execute("SELECT * FROM attention_items WHERE item_id = ?", (i,)).fetchone() for i in created]
    short_rows = [r for r in rows if json.loads(r["facts"])["part_id"] == "P-2210"]
    assert len(short_rows) == 1
    facts = json.loads(short_rows[0]["facts"])
    assert facts["condition"] == "short"
    assert facts["inbound_po_id"] == "none"
    assert short_rows[0]["dedupe_key"] == "stockout:P-2210:4812:none"


def test_other_order_demand_on_the_same_day_is_subtracted(make_harness):
    h = make_harness("scenario_a")
    # Give 4900 (currently far outside the horizon) the same start date as
    # 4812 and make it also need P-4471, so its demand competes for the
    # same projected balance on 2026-09-07.
    h.conn.execute(
        "UPDATE erp_production_orders SET scheduled_start = '2026-09-07', "
        "components = '[{\"part_id\": \"P-4471\", \"qty\": 500}]' WHERE prod_order_id = '4900'"
    )
    h.conn.commit()

    created = run_detectors(h.conn, h.clock)

    rows = [h.conn.execute("SELECT * FROM attention_items WHERE item_id = ?", (i,)).fetchone() for i in created]
    facts_4812 = [json.loads(r["facts"]) for r in rows if json.loads(r["facts"])["prod_order_id"] == "4812"]
    assert len(facts_4812) == 1
    # The extra 500-unit competing demand pushes the condition from thin
    # margin to outright short.
    assert facts_4812[0]["condition"] == "short"


def test_no_item_when_balance_is_healthy_and_no_po_is_near_margin(make_harness):
    h = make_harness("scenario_a")
    # An inbound PO landing comfortably early (well outside MARGIN_DAYS)
    # with ample on_hand: neither short nor thin margin.
    h.conn.execute("UPDATE erp_parts SET on_hand = 1000 WHERE part_id = 'P-4471'")
    h.conn.execute("UPDATE erp_purchase_orders SET promised_date = '2026-09-02' WHERE po_id = 'PO-77812'")
    h.conn.commit()

    created = run_detectors(h.conn, h.clock)

    dedupe_keys = [
        h.conn.execute("SELECT dedupe_key FROM attention_items WHERE item_id = ?", (i,)).fetchone()[0]
        for i in created
    ]
    assert not any(key.startswith("stockout:P-4471:") for key in dedupe_keys)


def test_no_item_when_inbound_po_is_in_window_but_outside_the_margin(make_harness):
    h = make_harness("scenario_a")
    # Promised 9/3, four days before the 9/7 start: inside the lookahead
    # window (so it counts toward the balance), but outside MARGIN_DAYS=3,
    # so it is skipped as a thin-margin candidate. The order is already
    # covered without it being fragile.
    h.conn.execute("UPDATE erp_purchase_orders SET promised_date = '2026-09-03' WHERE po_id = 'PO-77812'")
    h.conn.commit()

    created = run_detectors(h.conn, h.clock)

    dedupe_keys = [
        h.conn.execute("SELECT dedupe_key FROM attention_items WHERE item_id = ?", (i,)).fetchone()[0]
        for i in created
    ]
    assert not any(key.startswith("stockout:P-4471:") for key in dedupe_keys)


def test_owner_fallback_by_role_when_no_dependent_po(make_harness):
    h = make_harness("scenario_a")
    h.conn.execute("UPDATE erp_parts SET on_hand = 0 WHERE part_id = 'P-2210'")
    h.conn.commit()

    run_detectors(h.conn, h.clock)

    row = h.conn.execute(
        "SELECT owner_id FROM attention_items WHERE dedupe_key = 'stockout:P-2210:4812:none'"
    ).fetchone()
    # Falls back to the user with role 'Purchasing Manager' (Dana), since
    # there is no dependent inbound PO to attribute ownership to.
    assert row["owner_id"] == "u-101"


def test_running_detectors_twice_yields_one_item_and_logs_duplicate(make_harness):
    h = make_harness("scenario_a")
    run_detectors(h.conn, h.clock)
    run_detectors(h.conn, h.clock)

    assert h.conn.execute("SELECT COUNT(*) FROM attention_items").fetchone()[0] == 1
    events = [r[0] for r in h.conn.execute("SELECT event FROM audit_log ORDER BY seq")]
    assert events.count("detection.raised") == 1
    assert events.count("detection.duplicate_ignored") == 1


def test_a_new_dependent_po_after_a_reroute_gets_a_new_dedupe_key(make_harness):
    """Simulates the post-reroute state directly (rather than running the
    full workflow): the original PO is reduced low enough that it can no
    longer cover 4812 alone, and a new PO from the replacement supplier is
    what the order now also depends on. The new item's dedupe key must
    reference the new PO, not the old one, so it can alert independently.
    """

    h = make_harness("scenario_a")
    run_detectors(h.conn, h.clock)  # the original item, now "known"

    h.conn.execute("UPDATE erp_purchase_orders SET qty = 10, total_value = 420 WHERE po_id = 'PO-77812'")
    h.conn.execute(
        "INSERT INTO erp_purchase_orders (po_id, part_id, supplier_id, qty, unit_price, total_value, "
        "ordered_date, promised_date, status, created_by) VALUES "
        "('PO-NEWZ', 'P-4471', 'S-Z', 150, 46.50, 6975.00, '2026-09-02', '2026-09-04', 'open', 'u-101')"
    )
    h.conn.commit()

    created = run_detectors(h.conn, h.clock)

    new_keys = [
        h.conn.execute("SELECT dedupe_key FROM attention_items WHERE item_id = ?", (i,)).fetchone()[0]
        for i in created
    ]
    assert "stockout:P-4471:4812:PO-NEWZ" in new_keys


# ---------------------------------------------------------------------------
# QualityHoldDetector (Scenario B)


def test_quality_hold_fires_for_l2093_4820_within_three_days(make_harness):
    h = make_harness("scenario_b_covers")
    run_detectors(h.conn, h.clock)

    row = h.conn.execute(
        "SELECT owner_id, facts FROM attention_items WHERE dedupe_key = 'quality_hold:L-2093:4820'"
    ).fetchone()
    assert row is not None
    assert row["owner_id"] == "u-202"  # hold_placed_by
    facts = json.loads(row["facts"])
    assert facts["part_id"] == "P-1180"
    assert facts["qty"] == 100


def test_quality_hold_never_fires_for_the_unrelated_lot_tracked_noise_part(make_harness):
    """T9 (Tier 2): L-3000 (P-5500) is seed noise for a different
    lot-tracked part, present only to prove scoping. It is 'released',
    not 'hold', so it should never raise anything regardless of part.
    """

    h = make_harness("scenario_b_covers")
    run_detectors(h.conn, h.clock)

    assert h.conn.execute(
        "SELECT COUNT(*) FROM attention_items WHERE dedupe_key LIKE 'quality_hold:L-3000:%'"
    ).fetchone()[0] == 0


def test_quality_hold_does_not_fire_for_4831_too_far_out(make_harness):
    h = make_harness("scenario_b_covers")
    run_detectors(h.conn, h.clock)

    assert h.conn.execute(
        "SELECT COUNT(*) FROM attention_items WHERE dedupe_key LIKE 'quality_hold:%:4831'"
    ).fetchone()[0] == 0


def test_quality_hold_does_not_fire_for_released_lots(make_harness):
    h = make_harness("scenario_b_covers")
    run_detectors(h.conn, h.clock)

    # L-2115 (released) is allocated to 4831; neither its released status
    # nor 4831's distance should ever raise an item for it.
    assert h.conn.execute(
        "SELECT COUNT(*) FROM attention_items WHERE dedupe_key LIKE 'quality_hold:L-2115:%'"
    ).fetchone()[0] == 0


def test_quality_hold_owner_falls_back_to_role_when_hold_placed_by_missing(make_harness):
    h = make_harness("scenario_b_covers")
    h.conn.execute("UPDATE erp_lots SET hold_placed_by = NULL WHERE lot_id = 'L-2093'")
    h.conn.commit()

    run_detectors(h.conn, h.clock)

    owner = h.conn.execute(
        "SELECT owner_id FROM attention_items WHERE dedupe_key = 'quality_hold:L-2093:4820'"
    ).fetchone()[0]
    assert owner == "u-202"  # the only seeded Quality Manager


def test_quality_hold_skips_an_allocation_to_a_nonexistent_order(make_harness):
    h = make_harness("scenario_b_covers")
    h.conn.execute(
        "INSERT INTO erp_lot_allocations (lot_id, prod_order_id, qty) VALUES ('L-2093', 'NO-SUCH-ORDER', 1)"
    )
    h.conn.commit()

    # Must not raise despite the dangling allocation.
    run_detectors(h.conn, h.clock)


def test_quality_hold_skips_an_order_that_is_not_planned(make_harness):
    h = make_harness("scenario_b_covers")
    h.conn.execute("UPDATE erp_production_orders SET status = 'completed' WHERE prod_order_id = '4820'")
    h.conn.commit()

    run_detectors(h.conn, h.clock)

    assert h.conn.execute(
        "SELECT COUNT(*) FROM attention_items WHERE dedupe_key = 'quality_hold:L-2093:4820'"
    ).fetchone()[0] == 0


def test_quality_hold_skips_a_held_lot_allocated_outside_the_horizon(make_harness):
    h = make_harness("scenario_b_covers")
    # L-2115 is released in the base fixture; put it on hold and allocate
    # it to 4831 (starts 2026-09-15), well outside the 3-day horizon.
    h.conn.execute("UPDATE erp_lots SET status = 'hold', hold_placed_by = 'u-202' WHERE lot_id = 'L-2115'")
    h.conn.commit()

    run_detectors(h.conn, h.clock)

    assert h.conn.execute(
        "SELECT COUNT(*) FROM attention_items WHERE dedupe_key = 'quality_hold:L-2115:4831'"
    ).fetchone()[0] == 0


def test_quality_hold_dedupes_on_second_run(make_harness):
    h = make_harness("scenario_b_covers")
    run_detectors(h.conn, h.clock)
    run_detectors(h.conn, h.clock)

    assert h.conn.execute(
        "SELECT COUNT(*) FROM attention_items WHERE dedupe_key = 'quality_hold:L-2093:4820'"
    ).fetchone()[0] == 1


def test_detectors_run_after_a_tool_writes_to_an_erp_table(make_harness):
    h = make_harness("scenario_a")
    run_detectors(h.conn, h.clock)  # baseline: the original item is now known

    # reduce_po on an unrelated noise PO (P-8800): touches an ERP table
    # without changing P-4471/4812's stockout condition, so the same item
    # should be re-raised and deduped, proving detection re-ran at all.
    tool = get_tool("reduce_po")
    args = ReducePoArgs(po_id="PO-77901", new_qty=10)
    execute(h.conn, h.clock, tool, args, _ctx(), run_id="run-x", actor="test", requester_id="u-101")

    # Detection ran again as a side effect of the write, without an
    # explicit run_detectors() call, and found the same item already known.
    events = [r[0] for r in h.conn.execute("SELECT event FROM audit_log ORDER BY seq")]
    assert events.count("detection.duplicate_ignored") >= 1
