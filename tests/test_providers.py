"""Section 15.8: providers and scoping. QualityProvider arrives with
Scenario B in phase 6.
"""

from __future__ import annotations

import json

from harness.context.registry import gather_context
from harness.detection.base import AttentionItem
from harness.detection.registry import run_detectors
from harness.world.users import get_user


def _scenario_a_item(conn):
    row = conn.execute("SELECT * FROM attention_items").fetchone()
    return AttentionItem(
        detector=row["detector"], dedupe_key=row["dedupe_key"], owner_id=row["owner_id"],
        summary=row["summary"], facts=json.loads(row["facts"]),
    )


def test_mail_includes_relevant_and_excludes_noise_and_other_mailboxes(make_harness):
    h = make_harness("scenario_a")
    run_detectors(h.conn, h.clock)
    item = _scenario_a_item(h.conn)
    dana = get_user(h.conn, "u-101")

    context = gather_context(h.conn, h.clock, dana, item)
    message_ids = {r["message_id"] for r in context["mail"].records}

    assert message_ids == {"M-001"}


def test_mail_empty_for_user_without_mail_read_scope(make_harness):
    h = make_harness("scenario_a")
    run_detectors(h.conn, h.clock)
    item = _scenario_a_item(h.conn)
    lena = get_user(h.conn, "u-301")  # has mail:read actually; use a user truly lacking it
    assert "mail:read" in lena.scopes  # sanity: confirm fixture assumption

    # Construct a scopeless variant in-memory to exercise the guard directly.
    from dataclasses import replace

    scopeless = replace(lena, scopes=frozenset())
    context = gather_context(h.conn, h.clock, scopeless, item)
    assert context["mail"].records == []
    assert context["mail"].record_ids == []


def test_calendar_only_includes_the_users_own_events_in_window(make_harness):
    h = make_harness("scenario_a")
    run_detectors(h.conn, h.clock)
    item = _scenario_a_item(h.conn)
    dana = get_user(h.conn, "u-101")

    context = gather_context(h.conn, h.clock, dana, item)
    event_ids = {r["event_id"] for r in context["calendar"].records}

    assert event_ids == {"E-001", "E-002"}  # Dana's own events; not E-003 (Lena's)


def test_erp_includes_trap_suppliers_and_excludes_unrelated_pos(make_harness):
    h = make_harness("scenario_a")
    run_detectors(h.conn, h.clock)
    item = _scenario_a_item(h.conn)
    dana = get_user(h.conn, "u-101")

    context = gather_context(h.conn, h.clock, dana, item)
    record_ids = set(context["erp"].record_ids)

    assert {"S-Y", "S-Z", "S-Q", "S-W"}.issubset(record_ids)
    assert "S-N" not in record_ids  # not approved and no pricing for P-4471
    assert "PO-77900" not in record_ids
    assert "PO-77901" not in record_ids
    assert "PO-77812" in record_ids


def test_record_ids_match_returned_records_and_are_audited(make_harness):
    h = make_harness("scenario_a")
    run_detectors(h.conn, h.clock)
    item = _scenario_a_item(h.conn)
    dana = get_user(h.conn, "u-101")

    context = gather_context(h.conn, h.clock, dana, item, run_id="run-1")

    for slice_ in context.values():
        assert slice_.record_ids == [
            r.get("message_id") or r.get("event_id") or r.get("po_id") or r.get("supplier_id")
            or r.get("part_id") or r.get("prod_order_id")
            for r in slice_.records
        ]

    detail = json.loads(h.conn.execute(
        "SELECT detail FROM audit_log WHERE event = 'context.gathered' AND run_id = 'run-1'"
    ).fetchone()[0])
    assert detail["record_ids"]["mail"] == ["M-001"]
    assert set(detail["record_ids"]["erp"]) == set(context["erp"].record_ids)


def test_backup_approvers_calendar_is_not_exposed_to_the_planner(make_harness):
    """The backup approver's calendar is read by policy.approvals during
    escalation, directly against cal_events, never through
    CalendarProvider, and never shown to the model: Dana's gathered
    context must not include Priya's (u-102, her backup) events even
    though Priya's OOO status is exactly what escalation depends on.
    """

    h = make_harness("scenario_a")
    h.conn.execute(
        "INSERT INTO cal_events (event_id, owner, start, end, title, out_of_office) "
        "VALUES ('E-900', 'u-102', '2026-09-03T00:00:00', '2026-09-03T23:59:59', "
        "'Priya conference', 0)"
    )
    h.conn.commit()
    run_detectors(h.conn, h.clock)
    item = _scenario_a_item(h.conn)
    dana = get_user(h.conn, "u-101")

    context = gather_context(h.conn, h.clock, dana, item)
    event_ids = {r["event_id"] for r in context["calendar"].records}

    assert event_ids == {"E-001", "E-002"}
    assert "E-900" not in event_ids
