from __future__ import annotations

import pytest

from harness.world.users import UnknownUser, get_user, missing_scopes


def test_get_user_loads_scopes_and_limits(make_harness):
    h = make_harness("scenario_a")
    dana = get_user(h.conn, "u-101")
    assert dana.name == "Dana Whitfield"
    assert dana.manager_id == "u-100"
    assert dana.backup_approver_id == "u-102"
    assert "erp:po:create" in dana.scopes
    assert dana.approval_limits == {"po_create_max": 25000}


def test_get_user_raises_for_unknown_id(make_harness):
    h = make_harness("scenario_a")
    with pytest.raises(UnknownUser):
        get_user(h.conn, "u-999")


def test_missing_scopes_reports_only_what_is_missing(make_harness):
    h = make_harness("scenario_a")
    omar = get_user(h.conn, "u-202")
    assert missing_scopes(omar, ("erp:lot:read", "erp:po:create")) == ["erp:po:create"]
    assert missing_scopes(omar, ("erp:lot:read",)) == []
