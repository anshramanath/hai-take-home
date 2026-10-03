from __future__ import annotations

import pytest

from harness.memory.runs import UnknownRun, create_run, get_run, set_run_status, update_run_state


def test_create_and_update_run(make_harness):
    h = make_harness("scenario_a")
    run_id = create_run(h.conn, h.clock, item_id="AI-TEST", user_id="u-101")

    row = get_run(h.conn, run_id)
    assert row["status"] == "running"
    assert row["item_id"] == "AI-TEST"

    update_run_state(h.conn, run_id, {"foo": "bar"})
    set_run_status(h.conn, run_id, "closed")

    row2 = get_run(h.conn, run_id)
    assert row2["status"] == "closed"
    import json
    assert json.loads(row2["state"]) == {"foo": "bar"}


def test_get_run_raises_for_unknown_id(make_harness):
    h = make_harness("scenario_a")
    with pytest.raises(UnknownRun):
        get_run(h.conn, "RUN-NOPE")
