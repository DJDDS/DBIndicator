"""V13.0 swing desk: model reader, rules, option chooser, exits, admin template."""
import datetime as dt
import math
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from app import v130_core as core
from app import v130_rules as rules
from app.v130_gbm import TextGBM

ASSETS = Path(__file__).resolve().parents[1] / "app" / "v130_assets"


def test_text_model_matches_lightgbm_reference():
    ref = np.load(Path(__file__).with_name("v130_model_reference.npz"))
    m = TextGBM(ASSETS / "which_model.txt")
    assert m.feature_names == core.ST
    assert np.allclose(m.predict(ref["X"]), ref["y"], atol=1e-9)


def _X(rows):
    base = dict(r5=-0.06, sig20=0.03, low52_band=1.5, close=100.0)
    return pd.DataFrame([{**base, **r} for r in rows])


def _mk(**kw):
    return pd.Series({"tdte": 10, "vix": 20.0, "m10": 0.0, "nifty_up200": True, **kw})


def test_gate_open_takes_top_two_and_applies_quiet_filter():
    X = _X([dict(sym="A", ps=9), dict(sym="B", ps=8, r5=0.01), dict(sym="C", ps=7)])
    plan = rules.evening_plan(X, _mk(), {"open": True}, {"mode": "G"})
    assert [p["sym"] for p in plan["picks"]] == ["A"]
    assert plan["rejected"][0]["sym"] == "B" and "quiet" in plan["rejected"][0]["reasons"][0]


def test_closed_gate_only_takes_big_top_pick():
    big = _X([dict(sym="A", ps=9, sig20=0.04)])          # log(20*0.04) = -0.22 >= -0.5677 -> BIG
    small = _X([dict(sym="A", ps=9, sig20=0.01)])        # log(0.2) = -1.6 -> not BIG
    assert rules.evening_plan(big, _mk(), {"open": False}, {"mode": "G"})["picks"][0]["tag"] == "BIG"
    assert rules.evening_plan(small, _mk(), {"open": False}, {"mode": "G"})["picks"] == []
    assert rules.evening_plan(big, _mk(), {"open": False}, {"mode": "A"})["picks"] == []


def test_expiry_week_routes_to_futures_and_targets_are_prices():
    plan = rules.evening_plan(_X([dict(sym="A", ps=9, sig20=0.01)]), _mk(tdte=2), {"open": True}, {"mode": "G"})
    p = plan["picks"][0]
    assert p["tag"] == "EXPIRY" and p["instruments"][0]["type"] == "FUTURES"
    assert p["exits"]["target_price"] == pytest.approx(106.0) and p["exits"]["stop_close_price"] == pytest.approx(88.0)


def test_filters_results_news_ban_low52_and_crash_brake():
    X = _X([dict(sym="A", ps=9), dict(sym="B", ps=8)])
    plan = rules.evening_plan(X, _mk(m10=-0.09), {"open": True}, {"mode": "G"}, ban_next={"A"},
                              results={"B": {"ago_days": 3, "in_hold": False}})
    assert plan["picks"] == [] and plan["crash_brake"]
    X2 = _X([dict(sym="A", ps=9, low52_band=1.02)])
    assert "52-week" in rules.evening_plan(X2, _mk(), {"open": True}, {})["rejected"][0]["reasons"][0]
    news = {"A": {"n3_promoter": 1, "n3_bad_reg": 0, "news_abn": 0}}
    assert "bad news" in rules.evening_plan(_X([dict(sym="A", ps=9)]), _mk(), {"open": True}, {}, news=news)["rejected"][0]["reasons"][0]


def test_news_categories_follow_research():
    assert rules.categorize("Disclosure under SEBI Takeover Regulations", "") == "promoter"
    assert rules.categorize("Updates", "imposed a penalty of Rs 5 lakh") == "bad_reg"
    assert rules.categorize("Loss of Share Certificates", "") == "routine"
    now = dt.datetime(2026, 10, 5, 18, 0)
    f = rules.news_flags([{"ts": now - dt.timedelta(days=1), "desc": "Disclosure under SEBI Takeover Regulations", "text": ""}], now)
    assert f["n3_promoter"] == 1


def test_choose_call_picks_liquid_cheap_strike_with_levels():
    exp = dt.date(2026, 11, 24)
    chain = [dict(tradingsymbol=f"X26NOV{k}CE", strike=k, expiry=exp, bid=b, ask=a, oi=500000, lot_size=500)
             for k, b, a in ((1000, 38.0, 38.6), (1020, 29.0, 29.5), (1040, 21.5, 21.9))]
    out = rules.choose_call(chain, 1005.0, 0.03, 20.0, "BIG", 20000.0, {}, lambda e: 30)
    assert out["status"] == "BUY_CALL"
    assert out["target"] == pytest.approx(out["limit"] * 1.5, abs=0.01) and out["stop_close"] == pytest.approx(out["limit"] * 0.5, abs=0.01)
    assert out["label"] == "OTM1" and out["limit"] == pytest.approx(29.25)
    assert out["max_price"] >= out["limit"]
    wide = [dict(c, bid=c["bid"] * 0.8) for c in chain]
    assert rules.choose_call(wide, 1005.0, 0.03, 20.0, "BIG", 20000.0, {}, lambda e: 30)["status"] == "NO_CALL"
    assert rules.choose_call(chain, 1005.0, 0.03, 20.0, "BIG", 20000.0, {}, lambda e: 5)["status"] == "NO_CALL"


def test_implied_vol_round_trip():
    p = rules.bs_call(1000, 1020, 30 / 252, 0.35)
    assert rules.implied_vol(p, 1000, 1020, 30 / 252) == pytest.approx(0.35, abs=1e-4)


def test_exit_rules_match_research():
    from app.v130_swing import evaluate_exit
    s = rules.resolved({})
    assert evaluate_exit(100, [101, 103], [102, 107], s)[0] == "TARGET"
    assert evaluate_exit(100, [95, 87], [96, 95], s)[0] == "STOP_CLOSE"
    assert evaluate_exit(100, [98, 99, 101], [99, 100, 102], s)[0] == "SEVEN_DAY_HIGH_CLOSE"
    assert evaluate_exit(100, [99 - 0.1 * k for k in range(15)], [99.5] * 15, s)[0] == "TIME"


def test_gate_score_uses_frozen_formulas():
    import json
    ref = json.loads((ASSETS / "gate_ref.json").read_text())
    row = pd.Series({v: 0.0 for v in core.GATE_VARS}) + pd.Series({"vix": 30, "vix_rel": 1.6, "vix_pct": 0.95, "vix_z": 2.0, "vix_peak": 1.0, "mrsi2": 10, "breadth": 0.2,
                                                                    "m5": -0.05, "m20": -0.1, "mdd": -0.15, "nlow7": 0.5, "tdte": 3, "vix5": 0.2})
    out = core.gate_score(row, ref)
    assert out["n_formulas"] == len(ref["formulas"]) and 0 <= out["G"] <= 1


def test_admin_template_renders_three_lanes(tmp_path):
    from jinja2 import Environment, FileSystemLoader
    env = Environment(loader=FileSystemLoader(str(Path(__file__).resolve().parents[1] / "app" / "templates")))
    plan = rules.evening_plan(_X([dict(sym="A", ps=9, sig20=0.04), dict(sym="B", ps=8)]), _mk(), {"open": True, "G": 0.6, "cut": 0.47}, {"mode": "G", "normal_instrument": "CASH"})
    plan.update(date="2026-10-05", entry_day="2026-10-06", market={"vix": 20.0})
    status = {"scorecard": {k: {"n": 0, "win": None, "avg_pct": None} for k in ("FUTURES", "CASH", "ALL")}, "open": [], "scheduler": {"steps": {}},
              "storage": "x", "settings": rules.resolved({})}
    html = env.get_template("v130_admin.html").render(status=status, plan=plan)
    assert "Futures" in html and "Cash" in html and "Options (calls)" in html and "ADMIN ONLY" in html
