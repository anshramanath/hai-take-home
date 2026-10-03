"""Section 15.2: audit. The append-only trigger tests live in
test_world.py, since schema.sql (where the trigger lives) was a phase 1
deliverable. The `explain` renderer does not exist until phase 5, so the
"explain reads only from audit" case is deferred there.
"""

from __future__ import annotations

import json

from harness.audit.log import all_events, events_for_run, log


def test_log_inserts_ordered_rows_with_run_actor_event_detail(make_harness):
    h = make_harness("scenario_a")
    log(h.conn, h.clock, run_id="run-1", actor="detector", event="detection.raised", detail={"a": 1})
    log(h.conn, h.clock, run_id="run-1", actor="planner", event="planner.proposed", detail={"b": 2})
    log(h.conn, h.clock, run_id="run-2", actor="detector", event="detection.raised", detail={"c": 3})
    h.conn.commit()

    run1_events = events_for_run(h.conn, "run-1")
    assert [row["event"] for row in run1_events] == ["detection.raised", "planner.proposed"]
    assert [row["seq"] for row in run1_events] == sorted(row["seq"] for row in run1_events)
    assert run1_events[0]["actor"] == "detector"
    assert json.loads(run1_events[0]["detail"]) == {"a": 1}
    assert run1_events[0]["ts"] == "2026-09-02"

    assert len(events_for_run(h.conn, "run-2")) == 1
    assert len(all_events(h.conn)) == 3
