"""V13.1 intraday paper lanes: frozen short model, features, picks, gap basket, end-to-end with a fake Kite, admin page."""
import datetime as dt
import math
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from app import v131_intraday as v
from app.v130_gbm import TextGBM


def test_short_model_matches_lightgbm_reference():
    ref = np.load(Path(__file__).with_name("v131_model_reference.npz"))
    m = TextGBM(v.ASSETS / "short945_model.txt")
    assert m.feature_names == v.FE
    assert np.allclose(m.predict(ref["X"]), ref["y"], atol=1e-9)


def _daily(n=150, start=100.0, seed=0):
    rng = np.random.default_rng(seed)
    c = start * np.exp(np.cumsum(rng.normal(0, 0.015, n)))
    idx = pd.bdate_range(end="2026-10-02", periods=n).date
    return pd.DataFrame({"close": c, "high": c * 1.01, "low": c * 0.99}, index=idx)


def test_prep_matches_definitions():
    d = _daily()
    fv = pd.Series(np.arange(1, 31, dtype=float) * 1000, index=d.index[-30:])
    p = v.prep_from_history(d, fv)
    c = d.close.to_numpy()
    assert p["pc"] == pytest.approx(c[-1])
    assert p["r1"] == pytest.approx(math.log(c[-1] / c[-2]))
    assert p["r5"] == pytest.approx(math.log(c[-1] / c[-6]))
    assert p["d20h"] == pytest.approx(math.log(c[-1] / c[-20:].max()))
    assert p["atr20"] == pytest.approx(math.log(1.01 / 0.99))
    assert p["vs20"] == pytest.approx(np.median(np.arange(11, 31) * 1000))
    assert v.prep_from_history(d.iloc[:10], fv) is None


def _prep_and_quotes(n=60):
    prep, q = {}, {}
    for i in range(n):
        p = v.prep_from_history(_daily(seed=i), pd.Series([1e5] * 25, index=_daily(seed=i).index[-25:]))
        s = f"S{i:02d}"; prep[s] = p
        o = p["pc"] * (1 + 0.002 * ((i % 7) - 3)); c = o * (1 + 0.003 * ((i % 11) - 5))
        q[s] = dict(o=o, c=c, h=max(o, c) * 1.004, l=min(o, c) * 0.996, v=1.5e5 + i * 1000, vwap=(o + c) / 2)
    return prep, q


def test_snapshot_features_and_pick_shorts_respect_bans():
    prep, q = _prep_and_quotes()
    X = v.snapshot_features(prep, q)
    assert len(X) == 60 and set(v.FE) <= set(X.columns)
    r = X.iloc[0]; p = prep[r.symbol]
    assert r.gap == pytest.approx(math.log(q[r.symbol]["o"] / p["pc"]))
    assert r.nrel == pytest.approx((r.mv - X.mv.mean()) / r.atr20)
    picks = v.pick_shorts(X, set())
    assert len(picks) == 3 and list(picks.score) == sorted(picks.score)
    banned = set(picks.symbol[:1])
    assert not banned & set(v.pick_shorts(X, banned).symbol)
    assert v.pick_shorts(X.head(10), set()).empty            # too few stocks scored: no picks


def test_gap_basket_only_on_market_gap_down_days():
    prep, _ = _prep_and_quotes()
    flat = {s: p["pc"] for s, p in prep.items()}
    mg, picks = v.gap_candidates(prep, flat)
    assert mg == pytest.approx(0, abs=1e-12) and picks.empty
    down = {s: p["pl"] * 0.95 for s, p in prep.items()}        # everything opens 5% below yesterday's low
    mg, picks = v.gap_candidates(prep, down)
    assert mg < -0.005 and 0 < len(picks) <= 10
    for r in picks.itertuples():
        assert r.gapL < -r.sd90 and prep[r.symbol]["pc"] > prep[r.symbol]["ma20"]


class FakeKite:
    def __init__(self, day, n=60):
        self.day = day; self.n = n
        self.syms = [f"S{i:02d}" for i in range(n)]
    def instruments(self, ex):
        if ex == "NFO":
            return [{"name": s, "instrument_type": "FUT", "segment": "NFO-FUT"} for s in self.syms] + [{"name": "NIFTY", "instrument_type": "FUT", "segment": "NFO-FUT"}]
        return [{"tradingsymbol": s, "instrument_token": 1000 + i, "segment": "NSE"} for i, s in enumerate(self.syms)]
    def historical_data(self, tok, a, b, interval):
        i = tok - 1000; base = 100 + i
        if interval == "day":
            days = pd.bdate_range(a.date(), b.date())
            return [{"date": dt.datetime.combine(d.date(), dt.time(0)), "open": base, "high": base * 1.02, "low": base * 0.98, "close": base * (1 + 0.001 * ((k + i) % 5))} for k, d in enumerate(days)]
        out = []
        for d in pd.bdate_range(a.date(), b.date()):
            for m in range(555, 930, 5):
                px = base * (1 - 0.0001 * (m - 555) * (1 if i % 2 else -1))
                out.append({"date": dt.datetime.combine(d.date(), dt.time(m // 60, m % 60)), "open": px, "high": px * 1.001, "low": px * 0.999, "close": px, "volume": 1000})
        return out
    def quote(self, keys):
        out = {}
        for k in keys:
            s = k.split(":")[1]; i = int(s[1:]); base = 100 + i
            o = base * (1 - 0.003 * (i % 4)); c = o * (1 + 0.002 * ((i % 9) - 4))
            out[k] = {"last_price": c, "volume": 9000 + 10 * i, "average_price": (o + c) / 2, "total_buy_quantity": 10, "total_sell_quantity": 12,
                      "timestamp": dt.datetime.combine(self.day, dt.time(9, 45)), "ohlc": {"open": o, "high": max(o, c) * 1.003, "low": min(o, c) * 0.997, "close": base}}
        return out


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(v, "ROOT", tmp_path)
    monkeypatch.setattr(v.time, "sleep", lambda s: None)
    import app.v130_swing as sw
    monkeypatch.setattr(sw.NSE, "ban_day", lambda self, d: {"S05"})
    return tmp_path


def test_end_to_end_paper_day(store):
    day = dt.date(2026, 10, 6); k = FakeKite(day)
    with pytest.raises(RuntimeError):
        v.run_prep(FakeKite(day, n=20), day)                    # too few stocks: refuses to write a thin prep
    assert v.run_prep(k, day) == 60
    prep = v._read_json(f"prep_{day}.json", None)
    assert prep["banned"] == ["S05"] and "NIFTY" not in prep["stocks"]
    assert v.run_preopen(k, day) == 60 and v.run_preopen(k, day) == 1          # idempotent
    v.run_gap(k, day)
    assert v.run_shorts(k, day) == 3 and v.run_shorts(k, day) == 1
    day_rec = v._read_jsonl("shorts.jsonl")[-1]
    assert "S05" not in {p["symbol"] for p in day_rec["picks"]}
    assert v.run_short_outcomes(k, day) == 3
    for p in v._read_jsonl("shorts.jsonl")[-1]["picks"]:
        assert p["status"] == "CLOSED"
        assert p["ret_bps"] == pytest.approx(-math.log(p["exit"] / p["entry"]) * 1e4 - 10, abs=0.1)
    st = v.status()
    assert st["score_short"]["n"] == 3 and st["preopen_days"] == 1


def test_stale_quotes_refuse_to_pick(store):
    day = dt.date(2026, 10, 6); k = FakeKite(day)
    v.run_prep(k, day)
    k2 = FakeKite(dt.date(2026, 10, 5))                           # quotes stamped yesterday (holiday / no login)
    with pytest.raises(RuntimeError):
        v.run_shorts(k2, day)
    assert v._read_jsonl("shorts.jsonl") == []


def test_admin_page_renders_intraday_lanes():
    from jinja2 import Environment, FileSystemLoader
    from app import v130_rules as rules
    env = Environment(loader=FileSystemLoader(str(Path(__file__).resolve().parents[1] / "app" / "templates")))
    status = {"scorecard": {k: {"n": 0, "win": None, "avg_pct": None} for k in ("FUTURES", "CASH", "ALL")}, "open": [], "scheduler": {"steps": {}},
              "storage": "x", "settings": rules.resolved({})}
    intra = {"steps": {"short": {"ok": True, "at": "09:45"}}, "constants": v.constants(), "score_short": {"n": 3, "win": 67, "avg_bps": 21.0, "total_bps": 63},
             "score_gap": {"n": 0, "win": None, "avg_bps": None, "total_bps": None}, "preopen_days": 1,
             "today_short": {"date": "2026-10-06", "scored": 180, "ban_known": True, "market": {"move_since_open_pct": -0.4, "share_up": 31.0, "gap_pct": -0.2},
                             "picks": [{"symbol": "ABC", "entry": 101.5, "move_since_open_pct": -1.2, "vs_market_atr": -0.8, "volume_x_normal": 2.1, "exit": 99.0, "ret_bps": 239.0}]},
             "today_gap": {"date": "2026-10-06", "market_gap_pct": -0.21, "active": False, "picks": []}, "short_recent": [], "gap_recent": []}
    html = env.get_template("v130_admin.html").render(status=status, plan=None, intra=intra)
    assert "09:45 short detector" in html and "ABC" in html and "Gap-down bounce basket" in html and "+239 bps" in html


def test_v131_routes_are_owner_only(monkeypatch):
    import base64
    from app import security
    from app.web import app
    monkeypatch.setenv("DBI_OWNER_PASSWORD", "t")
    monkeypatch.setenv("DBI_MEMBER_USERNAME", "m"); monkeypatch.setenv("DBI_MEMBER_PASSWORD", "mp")
    security.reset_security_state_for_tests()
    c = app.test_client()
    member = {"Authorization": "Basic " + base64.b64encode(b"m:mp").decode()}
    assert c.get("/api/v131/status", headers=member).status_code == 403
    assert c.get("/api/v131/export/shorts", headers=member).status_code == 403
    owner = {"Authorization": "Basic " + base64.b64encode(b"admin:t").decode()}
    assert c.get("/api/v131/status", headers=owner).status_code == 200
    assert c.get("/api/v131/export/nope", headers=owner).status_code == 404
