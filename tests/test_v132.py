"""V13.2 movement predictor: frozen model, feature definitions, peer groups, news/board rules, end-to-end day with a fake Kite, page."""
import datetime as dt
import json
import math
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from app import v132_movers as v
from app.v130_gbm import TextGBM


def test_model_matches_lightgbm_reference_and_features():
    ref = np.load(Path(__file__).with_name("v132_model_reference.npz"))
    m = TextGBM(v.ASSETS / "mover_model.txt")
    assert m.feature_names == v.meta()["features"]
    assert np.allclose(m.predict(ref["X"]), ref["y"], atol=1e-9)
    assert len(v.meta()["conf_seed"]) >= 120


def _daily(n=300, seed=0, start=100.0):
    rng = np.random.default_rng(seed)
    c = start * np.exp(np.cumsum(rng.normal(0, 0.015, n)))
    o = c * np.exp(rng.normal(0, 0.005, n))
    h = np.maximum(o, c) * 1.01; l = np.minimum(o, c) * 0.99
    idx = pd.bdate_range(end="2026-10-06", periods=n).date
    return pd.DataFrame({"open": o, "high": h, "low": l, "close": c, "volume": rng.integers(1000, 5000, n).astype(float)}, index=idx)


def test_stock_pre_matches_research_definitions():
    d = _daily(); p = v.stock_pre(d)
    o, h, l, c, vol = (d[k].to_numpy() for k in ("open", "high", "low", "close", "volume"))
    exc = np.maximum(np.log(h / o), np.log(o / l)); rng = np.log(h / l)
    assert p["pc"] == pytest.approx(c[-1])
    assert p["exc1"] == pytest.approx(exc[-1]) and p["exc20"] == pytest.approx(exc[-20:].mean()) and p["exc60"] == pytest.approx(exc[-60:].mean())
    assert p["rng20"] == pytest.approx(rng[-20:].mean()) and p["squeeze"] == pytest.approx(rng[-1] / rng[-20:].mean())
    assert p["r5"] == pytest.approx(math.log(c[-1] / c[-6])) and p["r20"] == pytest.approx(math.log(c[-1] / c[-21]))
    assert p["vol1"] == pytest.approx(math.log(vol[-1] / np.median(vol[-20:])))
    assert p["hi52"] == pytest.approx(math.log(c[-1] / c[-250:].max())) and p["lo20"] == pytest.approx(math.log(c[-1] / l[-20:].min()))
    assert v.stock_pre(d.iloc[:10]) is None


def test_peer_groups_find_blocks():
    rng = np.random.default_rng(1); n = 250
    f = rng.normal(0, 0.01, (n, 8))
    cols = {f"S{g}_{i}": f[:, g] + rng.normal(0, 0.003, n) for g in range(8) for i in range(5)}
    m = v.peer_groups(pd.DataFrame(cols))
    for g in range(8):
        assert len({m[f"S{g}_{i}"] for i in range(5)}) == 1
    assert len(set(m.values())) == 8


def test_overnight_news_rule():
    today = dt.date(2026, 10, 7)
    rows = [{"symbol": "A", "desc": "Financial Results", "sort_date": "2026-10-06 18:05:00"},   # after close yesterday -> today
            {"symbol": "A", "desc": "Press release", "sort_date": "2026-10-07 08:30:00"},       # before open -> today
            {"symbol": "B", "desc": "Outcome of Board Meeting", "sort_date": "2026-10-06 11:00:00"},  # in session yesterday -> no
            {"symbol": "C", "desc": "x", "sort_date": "2026-10-07 10:00:00"}]                   # in session today -> no
    assert v.overnight_news(rows, today) == {"A": (2, 1)}


def test_board_flags_need_prior_intimation():
    today = dt.date(2026, 10, 7); prev = dt.date(2026, 10, 6)
    rows = [{"bm_symbol": "A", "bm_date": "07-Oct-2026", "bm_purpose": "Financial Results", "bm_timestamp": "30-Sep-2026 17:00:00"},
            {"bm_symbol": "B", "bm_date": "06-Oct-2026", "bm_purpose": "Financial Results", "bm_timestamp": "01-Oct-2026 12:00:00"},
            {"bm_symbol": "C", "bm_date": "07-Oct-2026", "bm_purpose": "Fund raising", "bm_timestamp": "07-Oct-2026 10:00:00"}]   # intimated after open
    f = v.board_flags(rows, today, prev)
    assert f["A"]["bm_today"] == 1 and f["A"]["bm_today_res"] == 1 and f["A"]["bm_yday"] == 0
    assert f["B"]["bm_yday_res"] == 1 and f["B"]["bm_today"] == 0
    assert "C" not in f


def test_confidence_threshold():
    assert v.conf_threshold([0.1] * 50) is None
    assert v.conf_threshold(list(np.linspace(0, 1, 251))) == pytest.approx(np.median(np.linspace(0, 1, 251)[-250:]))


class FakeKite:
    def __init__(self, day, n=60, stamp=None):
        self.day = day; self.n = n; self.stamp = stamp or day
        self.syms = [f"S{i:02d}" for i in range(n)]
        self.close_hl = {}
    def instruments(self, ex):
        if ex == "NFO":
            return [{"name": s, "instrument_type": "FUT", "segment": "NFO-FUT"} for s in self.syms] + [{"name": "NIFTY", "instrument_type": "FUT", "segment": "NFO-FUT"}]
        return [{"tradingsymbol": s, "instrument_token": 1000 + i, "segment": "NSE"} for i, s in enumerate(self.syms)]
    def historical_data(self, tok, a, b, interval):
        assert interval == "day"
        i = tok - 1000; d = _daily(n=len(pd.bdate_range(a.date(), b.date())), seed=i, start=100 + i)
        d.index = pd.bdate_range(a.date(), b.date()).date
        return [{"date": dt.datetime.combine(x, dt.time(0)), **r} for x, r in zip(d.index, d.to_dict("records"))]
    def quote(self, keys):
        out = {}
        for k in keys:
            s = k.split(":")[1]; i = int(s[1:]); base = 100 + i
            o = base * (1 + 0.004 * ((i % 7) - 3))
            hl = self.close_hl.get(s, (o * 1.005, o * 0.995))
            out[k] = {"last_price": o, "timestamp": dt.datetime.combine(self.stamp, dt.time(9, 16)), "ohlc": {"open": o, "high": hl[0], "low": hl[1], "close": base}}
        return out


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(v, "ROOT", tmp_path)
    monkeypatch.setattr(v.time, "sleep", lambda s: None)
    monkeypatch.setattr(v, "_board_meetings", lambda today: [{"bm_symbol": "S03", "bm_date": "06-Oct-2026", "bm_purpose": "Financial Results", "bm_timestamp": "01-Oct-2026 12:00:00"}])
    monkeypatch.setattr(v, "_announcements", lambda a, b: [{"symbol": "S03", "desc": "Financial Results", "sort_date": "2026-10-06 17:00:00"}])
    sent = []
    monkeypatch.setattr(v, "_telegram", lambda text: sent.append(text) or True)
    return sent


def test_end_to_end_day(store):
    day = dt.date(2026, 10, 7); k = FakeKite(day)
    assert v.run_prep(k, day) == 60
    assert v._read_json(f"peers_{day.year}.json", None) is not None
    assert v.run_pick(k, day) == 3
    rec = v._read_jsonl("picks.jsonl")[0]
    assert rec["scored"] == 60 and len(rec["picks"]) == 3 and rec["news_ok"] and rec["board_ok"]
    p = rec["picks"][0]
    assert p["need_pct"] >= 2.0 and p["up_level"] > p["open"] > p["down_level"]
    assert store and "Spotting/paper only" in store[0]
    assert v.run_pick(k, day) == 1                                   # idempotent: no second Telegram
    assert len(store) == 1
    k.close_hl = {p["symbol"]: (p["open"] * 1.05, p["open"] * 0.99)}
    assert v.run_outcomes(k, day) == 3
    rec = v._read_jsonl("picks.jsonl")[0]
    assert rec["picks"][0]["hit"] is True and rec["picks"][0]["dir"] == "UP"
    assert all(q["hit"] is False for q in rec["picks"][1:])
    sc = v.status()["score"]
    assert sc["top1"] == {"n": 1, "hit": 100} and sc["top3"]["n"] == 3


def test_stale_quotes_refuse_to_pick(store):
    day = dt.date(2026, 10, 7)
    v.run_prep(FakeKite(day), day)
    with pytest.raises(RuntimeError):
        v.run_pick(FakeKite(day, stamp=dt.date(2026, 10, 6)), day)
    assert v._read_jsonl("picks.jsonl") == [] and store == []


def test_prep_stops_when_kite_login_expired(store):
    class Expired(FakeKite):
        def historical_data(self, *a):
            raise Exception("Incorrect `api_key` or `access_token`.")
    with pytest.raises(RuntimeError, match="Kite login needed"):
        v.run_prep(Expired(dt.date(2026, 10, 7)), dt.date(2026, 10, 7))


def test_admin_page_renders_movers_card():
    from jinja2 import Environment, FileSystemLoader
    from app import v130_rules as rules
    env = Environment(loader=FileSystemLoader(str(Path(__file__).resolve().parents[1] / "app" / "templates")))
    status = {"scorecard": {k: {"n": 0, "win": None, "avg_pct": None} for k in ("FUTURES", "CASH", "ALL")}, "open": [], "scheduler": {"steps": {}},
              "storage": "x", "settings": rules.resolved({})}
    pick = {"symbol": "ABC", "open": 101.5, "prob_pct": 71.2, "gap_pct": 3.1, "normal_move_pct": 2.2, "need_pct": 2.86, "up_level": 104.44, "down_level": 98.64,
            "results_yday": True, "results_today": False, "overnight_filings": 2, "status": "CLOSED", "hit": True, "max_move_pct": 4.1, "dir": "DOWN"}
    movers = {"today": {"date": "2026-10-07", "scored": 205, "market_gap_pct": 0.2, "confident": True, "top_prob_pct": 71.2, "threshold_pct": 55.0,
                        "news_ok": True, "board_ok": True, "picks": [pick]},
              "score": {"top1": {"n": 1, "hit": 100}, "top1_confident": {"n": 1, "hit": 100}, "top3": {"n": 1, "hit": 100}},
              "research": v.meta()["research"], "steps": {"pick": {"ok": True, "at": "09:16"}}, "recent": []}
    html = env.get_template("v130_admin.html").render(status=status, plan=None, intra=None, movers=movers)
    assert "09:16 movement predictor" in html and "HIGH CONFIDENCE" in html and "ABC" in html and "HIT" in html and "results yesterday" in html


def test_v132_route_is_owner_only(monkeypatch):
    import base64
    from app import security
    from app.web import app
    monkeypatch.setenv("DBI_OWNER_PASSWORD", "t")
    monkeypatch.setenv("DBI_MEMBER_USERNAME", "m"); monkeypatch.setenv("DBI_MEMBER_PASSWORD", "mp")
    security.reset_security_state_for_tests()
    c = app.test_client()
    assert c.get("/api/v132/status", headers={"Authorization": "Basic " + base64.b64encode(b"m:mp").decode()}).status_code == 403
    assert c.get("/api/v132/status", headers={"Authorization": "Basic " + base64.b64encode(b"admin:t").decode()}).status_code == 200
