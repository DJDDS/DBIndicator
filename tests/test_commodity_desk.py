import base64

import numpy as np
import pandas as pd
import pytest

from app import commodity_desk as cd
from app import security, web


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(cd, "ROOT", tmp_path / "commodity_desk")
    return cd.ROOT


@pytest.fixture
def client(monkeypatch, store):
    monkeypatch.setenv("DBI_OWNER_USERNAME", "owner")
    monkeypatch.setenv("DBI_OWNER_PASSWORD", "test-owner-ng-password")
    monkeypatch.setenv("DBI_MEMBER_USERNAME", "member")
    monkeypatch.setenv("DBI_MEMBER_PASSWORD", "test-member-ng-password")
    monkeypatch.setattr(web, "_scanner_started", True)
    monkeypatch.setattr(cd, "maybe_refresh_async", lambda: None)   # never touch the network in tests
    security._AUTH_FAILURES.clear()
    return web.app.test_client()


def _auth(user, pw):
    return {"Authorization": "Basic " + base64.b64encode(f"{user}:{pw}".encode()).decode()}


OWNER = _auth("owner", "test-owner-ng-password")
MEMBER = _auth("member", "test-member-ng-password")


def _synthetic(store):
    """Two winters of deterministic data written in the cache format the desk reads."""
    store.mkdir(parents=True, exist_ok=True)
    d = pd.bdate_range("2023-01-02", "2025-02-28")
    rng = np.random.default_rng(7)
    close = 3 * np.exp(np.cumsum(rng.normal(0, 0.03, len(d))))
    ng = pd.DataFrame({"date": d, "open": close * (1 + rng.normal(0, .005, len(d))), "close": close})
    ng["high"] = ng[["open", "close"]].max(axis=1) * 1.01
    ng["low"] = ng[["open", "close"]].min(axis=1) * 0.99
    ng["volume"] = 1000
    ng.to_csv(store / "ng.csv", index=False)
    weeks = pd.date_range("2022-10-04", "2025-03-01", freq="W-TUE")
    pd.DataFrame({"date": weeks, "oi": 1.5e6 * np.exp(np.cumsum(rng.normal(0, .02, len(weeks))))}).to_csv(store / "cot.csv", index=False)
    days = pd.date_range("2019-01-01", "2025-02-28")
    hdd = np.maximum(0, 30 * np.cos((days.dayofyear - 15) / 365 * 2 * np.pi) + 10 + rng.normal(0, 4, len(days)))
    pd.DataFrame({"date": days, "hdd": hdd}).to_csv(store / "hdd_obs.csv", index=False)
    pd.DataFrame({"date": pd.date_range("2025-03-01", periods=16), "hdd": 20.0}).to_csv(store / "hdd_fcst.csv", index=False)
    pd.DataFrame({"date": d[-5:], "close": 85.0}).to_csv(store / "usdinr.csv", index=False)


# ------------------------------------------------------------------ access control
def test_routes_require_owner(client):
    for path in ("/admin/commodity", "/api/commodity/status", "/api/commodity/ledger/export"):
        assert client.get(path).status_code == 401
        assert client.get(path, headers=MEMBER).status_code == 403
    assert client.post("/api/commodity/refresh", headers=MEMBER).status_code == 403
    assert client.post("/api/commodity/settings", headers=MEMBER, data={"oi_weeks": 3}).status_code == 403


def test_page_renders_without_data(client):
    r = client.get("/admin/commodity", headers=OWNER)
    assert r.status_code == 200
    assert b"ADMIN ONLY" in r.data and b"Refresh data" in r.data


def test_page_renders_with_data(client, store):
    _synthetic(store)
    r = client.get("/admin/commodity", headers=OWNER)
    assert r.status_code == 200
    assert b"Backtest" in r.data and b"Weekly playbook" in r.data
    st = client.get("/api/commodity/status", headers=OWNER).json
    assert st["backtest"]["n"] > 0 and st["inputs"]["date"].startswith("2025-02")


def test_settings_roundtrip_and_clamping(client, store):
    r = client.post("/api/commodity/settings", headers=OWNER, json={"oi_weeks": 99, "wed_hold": 3, "leg_fri": False})
    assert r.status_code == 200
    s = cd.load_settings()
    assert s["oi_weeks"] == 12 and s["wed_hold"] == 3 and s["leg_fri"] is False


# ------------------------------------------------------------------ rules
def _row(date, **kw):
    base = dict(oi_chg=np.nan, r3s=np.nan, cold_z=np.nan, dow=pd.Timestamp(date).dayofweek)
    base.update(kw)
    return pd.Series(base, name=pd.Timestamp(date))


def test_monday_and_friday_open_interest_legs_point_opposite_ways():
    s = dict(cd.DEFAULTS)
    assert cd.leg_signal(_row("2025-01-06", oi_chg=-0.02), s)[0] == 1    # Monday, OI falling -> CALL
    assert cd.leg_signal(_row("2025-01-06", oi_chg=0.02), s)[0] == -1    # Monday, OI rising -> PUT
    assert cd.leg_signal(_row("2025-01-10", oi_chg=-0.02), s)[0] == -1   # Friday, OI falling -> PUT
    assert cd.leg_signal(_row("2025-01-10", oi_chg=0.02), s)[0] == 1     # Friday, OI rising -> CALL


def test_wednesday_put_needs_fall_or_cold_and_only_in_winter():
    s = dict(cd.DEFAULTS)
    assert cd.leg_signal(_row("2025-01-08", r3s=-0.3, cold_z=-1), s) == (-1, "WED into-EIA put", 2)
    assert cd.leg_signal(_row("2025-01-08", r3s=0.4, cold_z=0.5), s)[0] == -1
    assert cd.leg_signal(_row("2025-01-08", r3s=0.4, cold_z=-0.5), s)[0] == 0
    assert cd.leg_signal(_row("2025-06-04", r3s=-0.3), s)[0] == 0          # June: outside Oct-Feb
    assert cd.leg_signal(_row("2025-01-07", oi_chg=-0.02), s)[0] == 0      # Tuesday: no leg


def test_disabled_leg_is_silent():
    s = {**cd.DEFAULTS, "leg_mon": False}
    assert cd.leg_signal(_row("2025-01-06", oi_chg=-0.02), s)[0] == 0


def test_cot_is_only_used_after_release(store):
    """Tuesday positions must not be visible before the following Monday (released Friday after the NG settle)."""
    _synthetic(store)
    ng = pd.read_csv(store / "ng.csv", parse_dates=["date"]); cot = pd.read_csv(store / "cot.csv", parse_dates=["date"])
    hdd = pd.read_csv(store / "hdd_obs.csv", parse_dates=["date"]).set_index("date").hdd
    f = cd.features(ng, cot, hdd, dict(cd.DEFAULTS))
    oi = cot.set_index("date").oi
    tue = pd.Timestamp("2024-01-09")
    expected = np.log(oi).diff(4)[tue]
    assert f.loc["2024-01-12", "oi_chg"] != pytest.approx(expected)       # Friday of release week: still old value
    assert f.loc["2024-01-15", "oi_chg"] == pytest.approx(expected)       # next Monday: new value


def test_outcome_first_touch_counts_same_bar_as_loss():
    idx = pd.bdate_range("2025-01-06", periods=3)
    f = pd.DataFrame({"close": [100.0, 100.0, 100.0], "high": [100.0, 110.0, 100.0], "low": [100.0, 90.0, 100.0],
                      "sig": [0.02, 0.02, 0.02], "roll": [False, False, False]}, index=idx)
    o = cd.outcome(f, 0, 1, 1)
    assert o["first_touch"] == -1


def test_refresh_endpoint_is_non_blocking(client, monkeypatch):
    calls = []
    monkeypatch.setattr(cd, "refresh_async", lambda: calls.append(1) or True)
    r = client.post("/api/commodity/refresh", headers=OWNER, json={})
    assert r.status_code == 200 and r.json["status"] == "STARTED" and calls == [1]
