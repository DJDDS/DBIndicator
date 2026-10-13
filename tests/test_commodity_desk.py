import base64

import numpy as np
import pandas as pd
import pytest

import run  # noqa: F401  - register every route (as production does) before any test makes a request
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
    monkeypatch.setattr(cd, "refresh_price", lambda m: None)
    monkeypatch.setattr(web.kite_auth, "get_kite_client", lambda: None)
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
    r = _get_page(client)
    assert r.status_code == 200
    assert b"ADMIN ONLY" in r.data and b"Refresh data" in r.data


def _get_page(client):
    """The page serves a cached background-computed status: first call starts it, wait for it, then read the page."""
    import time
    cd._SC.update(st=None, at=0.0, busy=False)
    client.get("/admin/commodity", headers=OWNER)
    for _ in range(200):
        if cd._SC["st"] is not None or cd._SC["err"]: break
        time.sleep(0.05)
    cd.invalidate_status()
    return client.get("/admin/commodity", headers=OWNER)



def test_page_renders_with_data(client, store):
    _synthetic(store)
    r = _get_page(client)
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


# ------------------------------------------------------------------ MCX layer (fake Kite)
import datetime as _dt


class FakeKite:
    def __init__(self, F=310.0, today=_dt.date(2025, 1, 15)):
        self.F = F; self.today = today
        sh = today - _dt.date(2025, 1, 15)                 # shift every expiry so "today" is always 2025-01-15-like
        e1, e2 = _dt.date(2025, 1, 24) + sh, _dt.date(2025, 2, 24) + sh
        self.rows = [{"tradingsymbol": "NATURALGAS25JANFUT", "instrument_token": 1, "name": "NATURALGAS", "instrument_type": "FUT",
                      "strike": 0, "expiry": _dt.date(2025, 1, 27) + sh, "lot_size": 1250},
                     {"tradingsymbol": "NATURALGAS25FEBFUT", "instrument_token": 2, "name": "NATURALGAS", "instrument_type": "FUT",
                      "strike": 0, "expiry": _dt.date(2025, 2, 25) + sh, "lot_size": 1250}]
        tok = 10
        for e in (e1, e2):
            for k in range(280, 345, 5):
                for t in ("CE", "PE"):
                    tok += 1
                    self.rows.append({"tradingsymbol": f"NATURALGAS{e:%y%b}{k}{t}".upper(), "instrument_token": tok, "name": "NATURALGAS",
                                      "instrument_type": t, "strike": float(k), "expiry": e, "lot_size": 1250})
        self.rows.append({"tradingsymbol": "CRUDEOIL25JANFUT", "instrument_token": 99, "name": "CRUDEOIL", "instrument_type": "FUT",
                          "strike": 0, "expiry": _dt.date(2025, 1, 20), "lot_size": 100})

    def instruments(self, exch):
        assert exch == "MCX"
        return self.rows

    def quote(self, syms):
        from app.commodity_mcx import b76
        out = {}
        for s in syms:
            sym = s.split(":")[1]
            r = next(x for x in self.rows if x["tradingsymbol"] == sym)
            if r["instrument_type"] == "FUT":
                px = self.F
            else:
                T = max((r["expiry"] - self.today).days, 1) / 365
                px = round(b76(self.F, r["strike"], T, 0.6, r["instrument_type"] == "CE"), 2)
            out[s] = {"last_price": px, "oi": 1000, "volume": 10, "ohlc": {"close": self.F * 0.99},
                      "depth": {"buy": [{"price": round(px * 0.99, 2)}], "sell": [{"price": round(px * 1.01, 2)}]}}
        return out

    def historical_data(self, token, a, b, interval):
        return [{"date": _dt.datetime.combine(a, _dt.time()), "close": 300.0 if token < 10 else 9.0}]


def test_mcx_snapshot_picks_contracts_levels_and_option(store):
    from app import commodity_mcx as m
    s = {**cd.DEFAULTS}
    now = _dt.datetime(2025, 1, 15, 22, 45)
    snap = m.snapshot(FakeKite(), s, -1, 2, 0.03, now)
    assert snap["future"]["tradingsymbol"] == "NATURALGAS25JANFUT"           # 12 days to expiry > roll_days
    assert snap["option_expiry"] == "2025-01-24"
    L = snap["levels"]; assert L["entry"] == 310.0 and L["t1"] < 310 < L["stop"]  # PUT: targets below, stop above
    assert all(r["type"] == "PE" for r in snap["chain"]) and len(snap["chain"]) == 5
    atm = next(r for r in snap["chain"] if r["label"] == "ATM"); assert atm["strike"] == 310.0
    itm1 = next(r for r in snap["chain"] if r["label"] == "ITM1"); assert itm1["strike"] == 315.0   # higher strike is ITM for a put
    assert 50 < atm["iv_pct"] < 70 and atm["delta"] < 0
    assert atm["est"]["t1"] > atm["mid"] > atm["est"]["stop"]                 # put gains if the future falls to T1
    assert snap["suggested"]["label"] == "ATM"


def test_mcx_roll_and_expiry_rules():
    from app import commodity_mcx as m
    k = FakeKite()
    rows = [{"tradingsymbol": r["tradingsymbol"], "name": r["name"], "type": r["instrument_type"], "strike": r["strike"],
             "expiry": str(r["expiry"]), "lot_size": r["lot_size"]} for r in k.rows]
    assert m.pick_future(rows, "NATURALGAS", _dt.date(2025, 1, 26), 2)["tradingsymbol"] == "NATURALGAS25FEBFUT"   # 1 day left -> roll
    assert str(m.pick_option_expiry(rows, "NATURALGAS", _dt.date(2025, 1, 22), 4)) == "2025-02-24"               # 2 days left -> next


def test_suggest_steps_to_itm_when_atm_spread_is_wide():
    from app import commodity_mcx as m
    chain = [{"label": "ATM", "tradingsymbol": "A", "mid": 10, "spread_pct": 8.0},
             {"label": "ITM1", "tradingsymbol": "B", "mid": 14, "spread_pct": 2.0}]
    assert m.suggest(chain, 3.0)["label"] == "ITM1"
    assert m.suggest([{"label": "ATM", "tradingsymbol": "A", "mid": 10, "spread_pct": 9}], 3.0)["label"] is None


def test_status_with_kite_records_and_scores_mcx_ledger(store, monkeypatch):
    _synthetic(store)
    monkeypatch.setattr(cd, "refresh_price", lambda m: None)
    ng = pd.read_csv(store / "ng.csv", parse_dates=["date"])
    last = ng.date.max()                                   # treat the last bar as today's in-progress bar
    monkeypatch.setattr(cd, "_settled", lambda f: f.iloc[:-1])
    st = cd.status(kite=FakeKite(today=last.date()), now_ist=_dt.datetime.combine(last.date(), _dt.time(23, 0)))   # Feb: MCX closes 23:55, window 22:55-23:50
    assert "error" not in st["mcx"], st["mcx"]
    assert st["signal"] is not None, "synthetic last bar should carry a signal; pick another seed/day"
    if st["signal"]:
        assert st["signal"]["provisional"] is True and st["mcx"]["levels"]["entry"] == 310.0
        assert cd._mcx_ledger()[-1]["date"] == str(last.date())
        later = _dt.datetime.combine((last + pd.offsets.BDay(4)).date(), _dt.time(10, 0))
        cd.score_mcx_ledger(FakeKite(today=last.date()), later)
        assert "exit" in cd._mcx_ledger()[-1]


def test_page_without_kite_shows_login_hint(client, store):
    _synthetic(store)
    r = _get_page(client)
    assert r.status_code == 200 and b"Kite login required" in r.data


def test_settled_drops_in_progress_bar_including_next_day_evening_session():
    from datetime import datetime, timezone
    idx = pd.to_datetime(["2026-10-08", "2026-10-09", "2026-10-12"])          # Thu, Fri, Mon
    f = pd.DataFrame({"close": [1, 2, 3]}, index=idx)
    utc = lambda s: datetime.fromisoformat(s).replace(tzinfo=timezone.utc)
    # Sunday 19:30 NY (Mon 05:00 IST): Monday's bar has just opened -> not settled
    assert cd._settled(f, utc("2026-10-11T23:30:00")).index[-1] == pd.Timestamp("2026-10-09")
    # Monday 11:00 NY: still trading
    assert cd._settled(f, utc("2026-10-12T15:00:00")).index[-1] == pd.Timestamp("2026-10-09")
    # Monday 15:30 NY: settled
    assert cd._settled(f, utc("2026-10-12T19:30:00")).index[-1] == pd.Timestamp("2026-10-12")
    # winter (EST): 15:10 NY = 20:10 UTC -> settled; 14:50 NY -> not
    g = pd.DataFrame({"close": [1]}, index=pd.to_datetime(["2026-12-14"]))
    assert len(cd._settled(g, utc("2026-12-14T20:10:00"))) == 1
    assert len(cd._settled(g, utc("2026-12-14T19:50:00"))) == 0


def test_truncated_cache_never_becomes_today(store):
    """A half-written ng.csv (last line cut) must not turn an old date with NaN close into 'today'."""
    _synthetic(store)
    lines = (store / "ng.csv").read_text().splitlines()
    cut = lines[:300] + [lines[300].split(",")[0] + ",3.1,3.2"]           # partial row: date,open,close only
    (store / "ng.csv").write_text("\n".join(cut) + "\n")
    df = cd._read("ng.csv")
    assert df.close.notna().all() and df.date.is_monotonic_increasing


def test_atomic_write_leaves_no_partial_file(store):
    cd._atomic_csv("x.csv", pd.DataFrame({"date": ["2026-01-01"], "close": [1.0]}))
    assert (store / "x.csv").exists() and not list(store.glob("x.csv.tmp*"))


def test_mcx_session_follows_us_daylight_time():
    from app.commodity_mcx import mcx_session
    oct_ = mcx_session(_dt.datetime(2026, 10, 12, 23, 40))       # US on daylight time
    assert oct_["close_str"] == "23:30" and oct_["is_open"] is False and oct_["us_dst"] is True
    dec = mcx_session(_dt.datetime(2026, 12, 14, 23, 40))         # US on standard time
    assert dec["close_str"] == "23:55" and dec["is_open"] is True
    assert mcx_session(_dt.datetime(2026, 10, 12, 8, 59))["is_open"] is False
    assert mcx_session(_dt.datetime(2026, 10, 10, 12, 0))["is_open"] is False          # Saturday


def test_lot_units_and_pnl_per_lot(store):
    from app import commodity_mcx as m
    assert m.units_per_lot("NATURALGAS", 1) == 1250 and m.units_per_lot("NATGASMINI", 1) == 250
    snap = m.snapshot(FakeKite(), {**cd.DEFAULTS}, 1, 1, 0.03, _dt.datetime(2025, 1, 15, 22, 45))
    atm = next(r for r in snap["chain"] if r["label"] == "ATM")
    assert atm["premium_per_lot"] == round(atm["mid"] * 1250)
    assert atm["est_pnl_per_lot"]["t2"] > 0 > atm["est_pnl_per_lot"]["stop"]


def test_entry_window_tracks_mcx_close(store, monkeypatch):
    _synthetic(store)
    monkeypatch.setattr(cd, "refresh_price", lambda m: None)
    st = cd.status(kite=None, now_ist=_dt.datetime(2026, 10, 12, 18, 0))
    assert st["session"]["entry_window"] == "22:30-23:25" and st["session"]["close_str"] == "23:30"
    st = cd.status(kite=None, now_ist=_dt.datetime(2026, 12, 14, 18, 0))
    assert st["session"]["entry_window"] == "22:55-23:50"


def test_status_is_cached_and_never_blocks_a_request(monkeypatch):
    import threading, time
    from app import commodity_desk as cd
    gate = threading.Event()
    monkeypatch.setattr(cd, 'status', lambda kite=None, now_ist=None: (gate.wait(5), {'engine': 'x'})[1])
    cd._SC.update(st=None, at=0.0, busy=False)
    t0 = time.time(); a = cd.cached_status(lambda: None); b = cd.cached_status(lambda: None)
    assert time.time() - t0 < 0.5 and a is None and b is None            # slow job runs in the background, one at a time
    gate.set()
    for _ in range(100):
        if cd._SC['st'] is not None: break
        time.sleep(0.05)
    assert cd.cached_status(lambda: None)['engine'] == 'x'
    cd.invalidate_status(); assert cd._SC['at'] == 0.0


def test_open_meteo_gateway_blip_is_retried(monkeypatch):
    from app import commodity_desk as cd
    calls = []

    class R:
        def __init__(self, code, body): self.status_code = code; self._b = body; self.text = 'x'
        def json(self):
            if self._b is None: raise ValueError('html')
            return self._b
    seq = [R(502, None), R(502, None), R(200, [{'ok': 1}])]
    monkeypatch.setattr(cd.requests, 'get', lambda *a, **k: (calls.append(1), seq[len(calls) - 1])[1])
    monkeypatch.setattr(cd.time, 'sleep', lambda s: None)
    assert cd._om_json('u', {}, 5) == [{'ok': 1}] and len(calls) == 3


def test_failed_source_triggers_early_refresh(monkeypatch):
    from app import commodity_desk as cd
    from datetime import datetime, timezone, timedelta
    started = []
    monkeypatch.setattr(cd, 'refresh_async', lambda: started.append(1) or True)
    old = (datetime.now(timezone.utc) - timedelta(minutes=15)).isoformat(timespec='seconds')
    monkeypatch.setattr(cd, '_meta', lambda: {'refreshed_at': old, 'errors': {'hdd': 'HTTP 502'}})
    cd.maybe_refresh_async(); assert started
    started.clear()
    monkeypatch.setattr(cd, '_meta', lambda: {'refreshed_at': old, 'errors': {}})
    cd.maybe_refresh_async(); assert not started
