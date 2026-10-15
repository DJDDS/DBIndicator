"""V13.6 NIFTY ACD desk: levels, signal, stop/exit, an end-to-end day with a fake Kite, weekly cap, the admin card and route protection."""
import base64
import datetime as dt
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from app import v136_acd as v

DAY = dt.date(2026, 10, 13)   # a Tuesday


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(v, "ROOT", tmp_path)
    monkeypatch.setattr(v.time, "sleep", lambda s: None)
    monkeypatch.setattr(v, "_participant", lambda d: {"date": str(d), "fii_fut_net": -1000, "fii_fut_long_pct": 40.0, "client_opt_bias": 5, "client_opt_lean": "bullish"})
    sent = []
    monkeypatch.setattr(v, "_alert", lambda t: sent.append(t))
    monkeypatch.setattr(v, "now_ist", v.now_ist)
    return sent


def _daily(n=15, end=DAY):
    days = [end - dt.timedelta(days=k) for k in range(n * 2, 0, -1)]
    days = [d for d in days if d.weekday() < 5][-n:]
    rows = [{"date": pd.Timestamp(d), "open": 25000.0, "high": 25100.0, "low": 24900.0, "close": 25000.0 + 5 * i} for i, d in enumerate(days)]
    return pd.DataFrame(rows)


def _minutes(day, path):
    """path: array of 375 closes; bars have open = previous close and +-2 wicks."""
    t0 = dt.datetime.combine(day, dt.time(9, 15))
    prev = np.r_[path[0], path[:-1]]
    return pd.DataFrame({"date": [t0 + dt.timedelta(minutes=k) for k in range(len(path))], "open": prev,
                         "high": np.maximum(prev, path) + 2, "low": np.minimum(prev, path) - 2, "close": path})


def _down_day():
    """Range 24990-25010 in the first 30 minutes, then a steady slide to ~24700 by 11:00 and drift lower."""
    p = np.full(375, 25000.0)
    p[:30] = 25000 + 8 * np.sin(np.arange(30))
    p[30:105] = np.linspace(25000, 24700, 75)
    p[105:] = np.linspace(24700, 24600, 270)
    return p


def test_levels_match_definition():
    lv = v.compute_levels(_daily().rename(columns={"date": "d"}))
    assert lv["atr10"] == pytest.approx(200.0) and lv["A"] == pytest.approx(30.0)
    last = _daily().iloc[-1]
    piv, mid = (last.high + last.low + last.close) / 3, (last.high + last.low) / 2
    assert lv["pr_top"] == pytest.approx(round(max(piv, mid), 2)) and lv["pr_bot"] == pytest.approx(round(min(piv, mid), 2))
    assert v.compute_levels(_daily(5)) is None


def test_signal_needs_15_closes_beyond_a_and_pivot():
    lv = {"A": 30.0, "pr_top": 25050.0, "pr_bot": 24950.0}
    c = np.full(200, 25000.0); c[60:74] = 24900.0                      # only 14 closes below A-down (24960)
    assert v.scan_signal(c, lv, 25010.0, 24990.0) is None
    c[60:75] = 24900.0
    assert v.scan_signal(c, lv, 25010.0, 24990.0) == {"bar": 74, "side": -1}
    c2 = np.full(200, 25000.0); c2[60:80] = 25045.0                     # above A-up (25040) but below pivot top
    assert v.scan_signal(c2, lv, 25010.0, 24990.0) is None
    c2[60:80] = 25060.0
    assert v.scan_signal(c2, lv, 25010.0, 24990.0)["side"] == 1
    c3 = np.full(375, 25000.0); c3[301:] = 24900.0                      # 15th close would be after 14:29 -> no signal
    assert v.scan_signal(c3, lv, 25010.0, 24990.0) is None


def test_stop_first_and_1520_exit():
    o = np.full(375, 100.0); h = o + 1; l = o - 1
    l[50] = 59.0                                                         # long from bar 40 at 100, stop 40 -> hit at bar 50
    r = v.run_acd(o, h, l, 40, 1, 100.0, 40.0)
    assert r["state"] == "closed" and r["points"] == -40.0 and r["reason"] == "stop"
    o2 = np.linspace(100, 160, 375); h2 = o2 + 1; l2 = o2 - 1
    r2 = v.run_acd(o2, h2, l2, 40, 1, float(o2[40]), 40.0)
    assert r2["reason"] == "15:20 exit" and r2["points"] == pytest.approx(round(o2[365] - o2[40], 1))
    assert v.run_acd(o2[:200], h2[:200], l2[:200], 40, 1, float(o2[40]), 40.0)["state"] == "open"
    assert v.acd_stop(24800, -1, 25100, 24900) == 40.0 and v.acd_stop(25010, 1, 25010, 24990) == pytest.approx(10.0)


class FakeKite:
    def __init__(self, minutes, daily):
        self.minutes, self.daily, self.calls = minutes, daily, 0
    def instruments(self, ex):
        if ex == "NSE":
            return [{"tradingsymbol": "NIFTY 50", "instrument_token": 256265}]
        return [{"name": "NIFTY", "instrument_type": "FUT", "expiry": dt.date(2026, 10, 27), "tradingsymbol": "NIFTY26OCTFUT", "lot_size": 65},
                {"name": "NIFTY", "instrument_type": "CE", "expiry": DAY}]
    def historical_data(self, token, a, b, interval):
        self.calls += 1
        df = self.daily if interval == "day" else self.minutes
        df = df[(df.date >= a) & (df.date <= b)]
        return df.to_dict("records")


def test_end_to_end_short_day(store):
    k = FakeKite(_minutes(DAY, _down_day()), _daily())
    assert v.run_prep(k, DAY) == 1 and v.run_prep(k, DAY) == 1        # idempotent
    day = v._read_json(f"day_{DAY}.json", None)
    assert day["instruments"]["fut_symbol"] == "NIFTY26OCTFUT" and day["instruments"]["expiry_today"] is True
    assert v.run_tick(k, dt.datetime.combine(DAY, dt.time(9, 40))) == "opening range forming"
    st = v.run_tick(k, dt.datetime.combine(DAY, dt.time(10, 30)))
    assert st == "trade" and store and "SHORT" in store[0] and "CORE" in store[0]
    t = v._read_json(f"day_{DAY}.json", None)["trade"]
    assert t["state"] == "open" and t["stop_pts"] == 40.0 and t["live_points"] > 0
    assert v.run_tick(k, dt.datetime.combine(DAY, dt.time(15, 25))) == "final"
    rows = v._read_jsonl("trades.jsonl")
    assert len(rows) == 1 and rows[0]["side"] == "SHORT" and rows[0]["reason"] == "15:20 exit" and rows[0]["points"] > 0
    assert v.run_tick(k, dt.datetime.combine(DAY, dt.time(15, 30))) == "final" and len(v._read_jsonl("trades.jsonl")) == 1
    v.now_ist = lambda: dt.datetime.combine(DAY, dt.time(15, 31))
    s = v.status()
    assert s["score_short"]["n"] == 1 and s["today"]["signal"]["side"] == "SHORT" and "_bar" not in s["today"]["signal"]


def test_weekly_cap_skips_third_signal(store):
    for d in ("2026-10-12", "2026-10-12b"):
        v._append("trades.jsonl", {"date": d, "week": v.iso_week(DAY), "side": "SHORT", "points": 10.0})
    k = FakeKite(_minutes(DAY, _down_day()), _daily())
    v.run_prep(k, DAY)
    v.run_tick(k, dt.datetime.combine(DAY, dt.time(15, 25)))
    day = v._read_json(f"day_{DAY}.json", None)
    assert day["signal"]["skipped_week_cap"] is True and day["trade"] is None and len(v._read_jsonl("trades.jsonl")) == 2


def test_tick_without_prep(store):
    assert v.run_tick(FakeKite(pd.DataFrame(), _daily()), dt.datetime.combine(DAY, dt.time(10, 0))) == "no prep"


def test_participant_parser():
    raw = (b"Participant wise Open Interest as on Oct 13, 2026\n"
           b"Client Type,Future Index Long,Future Index Short,Option Index Call Long,Option Index Put Long,Option Index Call Short,Option Index Put Short\n"
           b"Client,100,50,1000,500,800,900\nFII,30000,120000,1,1,1,1\n")
    p = v.parse_participant(raw)
    assert p["fii_fut_net"] == -90000 and p["fii_fut_long_pct"] == 20.0 and p["client_opt_bias"] == 600 and p["client_opt_lean"] == "bullish"
    assert v.parse_participant(b"garbage") is None


def test_intraday_page_renders_acd_card(store):
    from jinja2 import Environment, FileSystemLoader
    env = Environment(loader=FileSystemLoader(str(Path(__file__).resolve().parents[1] / "app" / "templates")))
    k = FakeKite(_minutes(DAY, _down_day()), _daily())
    v.run_prep(k, DAY); v.run_tick(k, dt.datetime.combine(DAY, dt.time(10, 30)))
    acd = v.status(); acd["today"] = v._read_json(f"day_{DAY}.json", None)
    html = env.get_template("intraday_admin.html").render(direction={"error": "x"}, intra=None, acd=acd)
    for txt in ("NIFTY ACD desk", "▼ SHORT", "CORE", "NIFTY26OCTFUT", "How to take the trade", "A-down", "EXPIRY", "retail index options bullish"):
        assert txt in html, txt
    assert "ACD desk unavailable" in env.get_template("intraday_admin.html").render(direction={"error": "x"}, intra=None, acd={"error": "boom"})


def test_v136_routes_are_owner_only(monkeypatch, store):
    from app import security
    from app.web import app
    monkeypatch.setenv("DBI_OWNER_PASSWORD", "t")
    monkeypatch.setenv("DBI_MEMBER_USERNAME", "m"); monkeypatch.setenv("DBI_MEMBER_PASSWORD", "mp")
    security.reset_security_state_for_tests()
    c = app.test_client(); own = {"Authorization": "Basic " + base64.b64encode(b"admin:t").decode()}
    mem = {"Authorization": "Basic " + base64.b64encode(b"m:mp").decode()}
    assert c.get("/api/v136/status", headers=mem).status_code == 403
    assert c.get("/api/v136/status", headers=own).status_code == 200
    assert c.get("/api/v136/export", headers=mem).status_code == 403
    assert c.get("/admin/intraday", headers=own).status_code == 200
