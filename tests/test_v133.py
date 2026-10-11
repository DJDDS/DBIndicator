"""V13.3 09:25 direction desk: frozen models, research-identical feature engine, selection, order-flow score, NSE parsers,
outcomes, an end-to-end day with a fake Kite, the admin card and route protection."""
import datetime as dt
import json
import math
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from app import v133_direction as v
from app.v130_gbm import TextGBM


@pytest.mark.parametrize("h", v.HORIZONS)
def test_models_match_lightgbm_reference(h):
    m = v.meta()
    assert h in m["horizons"], f"horizon {h} missing from meta"
    tm = TextGBM(v.ASSETS / m["horizons"][h]["model"])
    assert tm.feature_names == m["features"]
    ref = np.load(Path(__file__).with_name(f"v133_{h}_reference.npz"))
    assert np.allclose(tm.predict(ref["X"]), ref["raw"], atol=1e-9)
    assert len(m["horizons"][h]["day_view_seed"]) >= 250
    assert len(m["ensemble"]["day_view_seed"]) >= 250 and m["ensemble"]["research"]["q80_hold"]["win_2024_26"] > 60


def _bars(day: dt.date, o=100.0, seed=0, drift=0.0):
    rng = np.random.default_rng(seed); rows = []; px = o
    for k in range(75):
        t = dt.datetime.combine(day, dt.time(9, 15)) + dt.timedelta(minutes=5 * k)
        op = px; px = px * math.exp(drift + rng.normal(0, 0.002))
        rows.append({"date": t, "open": op, "high": max(op, px) * 1.001, "low": min(op, px) * 0.999, "close": px, "volume": float(rng.integers(100, 1000))})
    return pd.DataFrame(rows)


def test_day_aggregates_and_today_state_match_research_definitions():
    b = _bars(dt.date(2026, 10, 5))
    A = v.day_aggregates(b)
    assert len(A) == 1
    r = A.iloc[0]; C = b.close.to_numpy()
    assert r.o == pytest.approx(b.open.iloc[0]) and r.c == pytest.approx(C[-1]) and r.h == pytest.approx(b.high.max())
    assert r.p925 == pytest.approx(C[1]) and r.r_day == pytest.approx(math.log(C[71] / C[1])) and r.r_30 == pytest.approx(math.log(C[7] / C[1]))
    t = v.today_state(b.iloc[0].to_dict(), b.iloc[1].to_dict())
    assert t["mv925"] == pytest.approx(r.mv925) and t["b1"] == pytest.approx(math.log(C[1] / C[0]))
    tp = ((b.high[:2] + b.low[:2] + b.close[:2]) / 3 * b.volume[:2]).sum() / b.volume[:2].sum()
    assert t["vw925"] == pytest.approx(math.log(C[1] / tp))


def _history(n=280, seed=1):
    days = pd.bdate_range(end="2026-10-05", periods=n).date
    return pd.concat([v.day_aggregates(_bars(d, 100 * (1 + 0.001 * i), seed + i)) for i, d in enumerate(days)], ignore_index=True)


def test_stock_history_windows_end_yesterday():
    A = _history()
    p = v.stock_history(A)
    o, h, l, c = (A[k].to_numpy() for k in ("o", "h", "l", "c"))
    exc = np.maximum(np.log(h / o), np.log(o / l))
    assert p["exc20"] == pytest.approx(exc[-20:].mean()) and p["pc"] == pytest.approx(c[-1])
    assert p["r5"] == pytest.approx(math.log(c[-1] / c[-6])) and p["hi52"] == pytest.approx(math.log(c[-1] / c[-250:].max()))
    assert p["hab_day"] == pytest.approx(A.r_day.to_numpy()[-20:].mean() / p["exc20"])
    assert p["v925_med"] == pytest.approx(np.median(A.v925.to_numpy()[-20:]))


def _universe(n=40, day=dt.date(2026, 10, 6)):
    hist, today, peers = {}, {}, {}
    for i in range(n):
        A = _history(260, seed=10 * i)
        hist[f"S{i:02d}"] = v.stock_history(A)
        b = _bars(day, hist[f"S{i:02d}"]["pc"] * math.exp(0.01 * ((i % 5) - 2)), seed=500 + i, drift=0.0005 * ((i % 3) - 1))
        today[f"S{i:02d}"] = v.today_state(b.iloc[0].to_dict(), b.iloc[1].to_dict())
        peers[f"S{i:02d}"] = i % 4
    return hist, today, peers


def test_features_cover_model_inputs_and_cross_section():
    hist, today, peers = _universe()
    ctx = {"oi1": {}, "oi5": {}, "ban": {"S01": (1, 1, 0)}, "bm": {}, "news": {"S02": (1, 1)}, "ivix": 13.0, "divix": 0.02,
           "fii_idx": -0.3, "fii_stk": 0.1, "fii_didx": -0.01, "fii_dstk": 0.0, "dte": 9.0}
    mk = {"M_r1": -0.01, "M_r5": -0.02, "M_r20": 0.01, "M_intra1": -0.005, "M_intra5": -0.01, "m_ret": -0.004, "m_disp": 0.012}
    X = v.features_925(hist, today, peers, ctx, mk, dt.date(2026, 10, 6))
    assert set(v.meta()["features"]) <= set(X.columns)
    assert len(X) == 40 and X.cs_mv925.between(0, 1).all()
    assert X.M_mv925.nunique() == 1 and X.M_mv925.iloc[0] == pytest.approx(X.mv925.mean())
    assert X.set_index("symbol").at["S01", "inban"] == 1 and X.set_index("symbol").at["S02", "res_overnight"] == 1
    g = X[X.peer == 1]; assert g.peer_gap.iloc[0] == pytest.approx(g.gap.mean())
    for h in v.HORIZONS:
        p = v.predict_up(X, h); assert np.isfinite(p).all() and ((p > 0) & (p < 1)).all()


def test_selection_strong_and_normal_days():
    X = pd.DataFrame({"symbol": [f"S{i}" for i in range(10)]})
    hist = list(0.5 + np.random.default_rng(0).normal(0, 0.02, 300))
    s = {"top_n": 3, "strong_quantile": 0.7, "window": 250}
    strong = v.select(X, np.r_[np.full(9, 0.30), 0.6], hist, s)
    assert strong["strong"] and strong["market_lean"] == "DOWN" and len(strong["list"]) == 3 and set(strong["list"].side) == {"DOWN"}
    normal = v.select(X, np.full(10, 0.5) + np.linspace(-0.01, 0.01, 10), hist, s)
    assert not normal["strong"]
    assert v.day_view_threshold(hist[:30], 0.7, 250) is None


def test_order_flow_score_signs():
    polls = []
    for k in range(6):
        polls.append({"t": f"09:1{k}", "q": {"BUY": [100 + k, 1000 * (k + 1), 900, 100], "SELL": [100 - k, 1000 * (k + 1), 100, 900], "FLAT": [100, 1000 * (k + 1), 500, 500]}})
    of = v.of_scores(polls, {"BUY": 100, "SELL": 100, "FLAT": 100})
    assert of["BUY"]["ofs"] > 0 > of["SELL"]["ofs"] and of["BUY"]["dr"] == 1.0 and of["SELL"]["dr"] == -1.0


def test_nse_parsers():
    raw = ("Participant wise Open Interest (no. of contracts) in Equity Derivatives as on Oct 09, 2026\n"
           "Client Type,Future Index Long,Future Index Short,Future Stock Long,Future Stock Short\n"
           "Client,100,200,300,400\nFII,30000,70000,\"3,000,000\",\"1,000,000\"\nTOTAL,1,1,1,1\n").encode()
    r = v.parse_participant_oi(raw)
    assert r["fii_idx"] == pytest.approx(-0.4) and r["fii_stk"] == pytest.approx(0.5)
    assert v.parse_participant_oi(b"garbage") is None
    prev = dt.date(2026, 10, 9)
    assert v.days_to_expiry([dt.date(2026, 10, 10), dt.date(2026, 10, 27)], prev) == 18.0
    oi1, oi5 = v.oi_changes([{"A": 100.0}] * 5 + [{"A": 120.0}])
    assert oi1["A"] == pytest.approx(math.log(121 / 101)) and oi5["A"] == pytest.approx(math.log(121 / 101))


def test_outcome_uses_exit_candle_and_side():
    b = _bars(dt.date(2026, 10, 6), drift=-0.0004, seed=3)
    C = b.close.to_numpy()
    out = v.outcome_from_bars(b, entry=C[1], p925=C[1], side="DOWN", k_exit=v.HORIZON_BARS["60"])
    assert out["exit"] == pytest.approx(C[13]) and out["ret925_pct"] == pytest.approx(-math.log(C[13] / C[1]) * 100, abs=1e-3)
    assert out["win"] == (C[13] < C[1]) and out["status"] == "CLOSED"


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(v, "ROOT", tmp_path)
    monkeypatch.setattr(v.time, "sleep", lambda s: None)
    return tmp_path


class FakeKite:
    def __init__(self, uni, day):
        self.uni = uni; self.day = day
    def quote(self, keys):
        return {k: {"last_price": 100.0, "volume": 1000, "buy_quantity": 600, "sell_quantity": 400} for k in keys}


def test_end_to_end_list_and_outcomes(store, monkeypatch):
    from app import v132_movers as v2
    day = dt.date(2026, 10, 6)
    hist, today, peers = _universe(40, day)
    for i, s in enumerate(hist): hist[s]["token"] = i + 1
    v._write_json(f"prep_{day}.json", {"date": str(day), "prev_session": "2026-10-05", "hist": hist, "market": {"M_r1": -0.01}, "peers": peers,
                                        "ctx": {"oi1": {}, "oi5": {}, "ban": {}, "bm": {}, "ivix": 13.0, "divix": 0.01, "dte": 9.0}, "missing": {}})
    tok2sym = {h["token"]: s for s, h in hist.items()}
    def fake_5min(kite, token, a, b, tries=3):
        s = tok2sym[token]; i = int(s[1:])
        bars = _bars(day, hist[s]["pc"] * math.exp(0.01 * ((i % 5) - 2)), seed=500 + i, drift=0.0005 * ((i % 3) - 1))
        return bars[(bars.date >= a) & (bars.date < b)] if b.time() <= dt.time(9, 25) else bars
    monkeypatch.setattr(v, "_kite_5min", fake_5min)
    monkeypatch.setattr(v2, "_announcements", lambda a, b: [])
    sent = []; monkeypatch.setattr(v2, "_telegram", lambda t: sent.append(t) or True)
    k = FakeKite(hist, day)
    assert v.run_list(k, day) == 3 * len(v.HORIZONS) + 3
    d = v._read_jsonl("days.jsonl")[-1]
    assert set(d["lists"]) == set(v.LISTS) and all(len(d["lists"][h]["picks"]) == 3 for h in v.HORIZONS) and len(d["lists"]["hc"]["picks"]) == 3 and d["lists"]["hc"]["traded"] is True
    assert d["lists"]["hc"]["book_pct"] == 1.0 and d["lists"]["hc"]["exit_at"] == "15:15"
    assert sent and "09:25 direction lists" in sent[0]
    assert v.run_list(k, day) == 1                       # idempotent per day
    assert v.run_outcomes(k, day) == 3 * len(v.HORIZONS) + 3
    st = v.status()
    assert st["score"]["day"]["traded"]["n"] + st["score"]["day"]["watch"]["n"] == 3
    assert st["score"]["pool"]["days"] == 1 and st["score"]["pool"]["ge1"] is not None
    p0 = d["lists"]["day"]["picks"][0]
    assert "book_ret_pct" in v._read_jsonl("days.jsonl")[-1]["lists"]["day"]["picks"][0] and p0["option"]["status"] == "NO_OPTION"


def test_list_refuses_without_prep(store):
    with pytest.raises(RuntimeError):
        v.run_list(FakeKite({}, dt.date(2026, 10, 6)), dt.date(2026, 10, 6))


def test_settings_are_clamped(store):
    s = v.save_settings({"top_n": 500, "strong_quantile": 3, "trade_normal_days": True, "bogus": 1})
    assert s["top_n"] == 50 and s["strong_quantile"] == 0.95 and v.load_settings()["book_target_pct"] == 0.75 and s["trade_normal_days"] is True and "bogus" not in s


def test_intraday_page_renders_direction_card(store):
    from jinja2 import Environment, FileSystemLoader
    env = Environment(loader=FileSystemLoader(str(Path(__file__).resolve().parents[1] / "app" / "templates")))
    pick = {"symbol": "ABC", "side": "DOWN", "entry": 101.5, "conf": 0.31, "gap_pct": 2.1, "of": {"ofs": -1.2}, "of_agree": True, "status": "CLOSED",
            "exit": 99.0, "win": True, "ret_pct": 2.4, "best_pct": 3.0, "worst_pct": -0.4, "booked": True, "book_ret_pct": 0.69, "book_win": True,
            "option": {"status": "CLOSED", "ts": "ABC26OCT100PE", "entry": 3.2, "spread_pct": 1.1, "lot": 500, "ret_pct": 41.0, "win": True}}
    lists = {h: {"exit_at": v.EXIT_LABEL[h], "view": 0.42, "strength": 0.08, "threshold": 0.05, "strong": True, "lean": "DOWN", "traded": True, "picks": [pick], "book_pct": 1.0} for h in v.LISTS}
    st = v.status(); st["today"] = {"date": "2026-10-06", "at": "2026-10-06 09:26:31", "n_scored": 190, "lists": lists, "missing": {"fii": True, "vix": False}}
    intra = {"score_short": {"n": 12, "win": 50, "avg_bps": -55.0}, "score_short_bear": {}, "score_short_other": {"n": 12, "win": 50, "avg_bps": -55.0}}
    html = env.get_template("intraday_admin.html").render(direction=st, intra=intra)
    for txt in ("DAILY POOL OF 1", "daily pool", "at least 1 of 3", "Intraday Desk", "09:25 direction desk", "▼ DOWN", "STRONG day", "ABC26OCT100PE", "booked", "RIGHT", "missing inputs: fii", "09:45 short detector", "+41.0%"):
        assert txt in html, txt
    assert "Intraday Desk" in env.get_template("intraday_admin.html").render(direction={"error": "x"}, intra=None)


def test_booking_exit_and_option_choice():
    b = _bars(dt.date(2026, 10, 6), drift=0.0006, seed=4); C = b.close.to_numpy()
    out = v.outcome_from_bars(b, entry=C[1], p925=C[1], side="UP", book_pct=0.5)
    up = (b.high[2:72].max() / C[1] - 1) * 100
    assert out["booked"] == (up >= 0.5) and (not out["booked"] or out["book_ret_pct"] == pytest.approx(0.5 - 0.06))
    nfo = [{"segment": "NFO-OPT", "name": "ABC", "instrument_type": t, "expiry": e, "strike": k, "tradingsymbol": f"ABC{e:%d%b}{int(k)}{t}", "instrument_token": i, "lot_size": 500}
           for i, (t, e, k) in enumerate((t, e, k) for t in ("CE", "PE") for e in (dt.date(2026, 10, 7), dt.date(2026, 10, 27)) for k in (95.0, 100.0, 105.0))]
    ch = v.option_chain_map(nfo, {"ABC"}, dt.date(2026, 10, 6))
    assert ch["ABC"]["expiry"] == "2026-10-27" and len(ch["ABC"]["CE"]) == 3      # the 1-day expiry is skipped
    c_up = v.option_candidates(ch["ABC"], "UP", 101.0); c_dn = v.option_candidates(ch["ABC"], "DOWN", 101.0)
    assert [c["strike"] for c in c_up] == [100.0, 105.0] and [c["strike"] for c in c_dn] == [100.0, 95.0] and c_dn[0]["type"] == "PE"
    q = {c_up[0]["ts"]: {"depth": {"buy": [{"price": 4.0}], "sell": [{"price": 4.6}]}},          # ATM spread 14% -> too wide
         c_up[1]["ts"]: {"depth": {"buy": [{"price": 2.0}], "sell": [{"price": 2.04}]}, "oi": 9000}}
    o = v.choose_option(c_up, q, 3.0)
    assert o["label"] == "OTM1" and o["entry"] == 2.04 and o["status"] == "OPEN"
    assert v.choose_option(c_up, {}, 3.0)["status"] == "NO_OPTION"
    ob = pd.DataFrame({"date": [dt.datetime(2026, 10, 6, 15, 10)], "close": [3.0]})
    r = v.option_outcome(ob, {"entry": 2.0, "spread_pct": 2.0}, v.K_EXIT)
    assert r["exit"] == pytest.approx(2.97) and r["win"] is True


def test_v133_routes_are_owner_only(monkeypatch, store):
    import base64
    from app import security
    from app.web import app
    monkeypatch.setenv("DBI_OWNER_PASSWORD", "t")
    monkeypatch.setenv("DBI_MEMBER_USERNAME", "m"); monkeypatch.setenv("DBI_MEMBER_PASSWORD", "mp")
    security.reset_security_state_for_tests()
    c = app.test_client(); own = {"Authorization": "Basic " + base64.b64encode(b"admin:t").decode()}
    assert c.get("/api/v133/status", headers={"Authorization": "Basic " + base64.b64encode(b"m:mp").decode()}).status_code == 403
    assert c.get("/api/v133/status", headers=own).status_code == 200
    assert c.get("/admin/intraday", headers=own).status_code == 200
    assert c.get("/admin/intraday", headers={"Authorization": "Basic " + base64.b64encode(b"m:mp").decode()}).status_code == 403
    r = c.post("/api/v133/settings", json={"top_n": 7}, headers=own)
    assert r.status_code in (200, 400, 403) and (r.status_code != 200 or r.get_json()["top_n"] == 7)


# ------------------------------------------------------------------ order-flow lab
def _q(ltp, vol, bq, sq, oi=None, bo=10, ao=10):
    return {"last_price": ltp, "volume": vol, "buy_quantity": bq, "sell_quantity": sq, "average_price": 100.0, "last_quantity": 5, "oi": oi,
            "ohlc": {"open": 100.0}, "depth": {"buy": [{"price": ltp - 0.05, "quantity": bq // 10, "orders": bo}] * 5,
                                               "sell": [{"price": ltp + 0.05, "quantity": sq // 10, "orders": ao}] * 5}}


def test_lab_snapshot_and_futures_map():
    r = v.snap(_q(101.0, 5000, 900, 100, oi=7000))
    assert len(r) == len(v.SNAP_COLS) and r[0] == 101.0 and r[6] == 7000 and r[9] == 450 and r[11] == 50 and r[13] == 100.0
    nfo = [{"instrument_type": "FUT", "segment": "NFO-FUT", "name": "ABC", "expiry": dt.date(2026, 10, 27), "tradingsymbol": "ABC26OCTFUT"},
           {"instrument_type": "FUT", "segment": "NFO-FUT", "name": "ABC", "expiry": dt.date(2026, 11, 24), "tradingsymbol": "ABC26NOVFUT"},
           {"instrument_type": "FUT", "segment": "NFO-FUT", "name": "OLD", "expiry": dt.date(2026, 9, 29), "tradingsymbol": "OLD26SEPFUT"}]
    assert v.futures_map(nfo, {"ABC", "OLD"}, dt.date(2026, 10, 6)) == {"ABC": "ABC26OCTFUT"}


def _lab_polls(day):
    polls = [{"t": f"{day}T09:1{k}:00", "ph": "pre", "q": {"BUY": v.snap(_q(100, 0, 800, 200)), "SELL": v.snap(_q(100, 0, 200, 800))}} for k in (8, 9)]
    for k in range(10):
        t = f"{day}T09:{15 + k:02d}:00"
        polls.append({"t": t, "ph": "fast",
                      "q": {"BUY": v.snap(_q(100 + 0.1 * k, 1000 * (k + 1), 900, 100, bo=5, ao=20)), "SELL": v.snap(_q(100 - 0.1 * k, 1000 * (k + 1), 100, 900, bo=20, ao=5)),
                            "FLAT": v.snap(_q(100, 1000 * (k + 1), 500, 500))},
                      "f": {"BUY": v.snap(_q(100.5 + 0.1 * k, 500 * (k + 1), 700, 300, oi=1000 + 50 * k)),
                            "SELL": v.snap(_q(100.5 - 0.1 * k, 500 * (k + 1), 300, 700, oi=1000 + 50 * k))}})
    polls.append({"t": f"{day}T09:40:00", "ph": "slow", "q": {"BUY": v.snap(_q(90, 1, 1, 999))}})      # after the cutoff: ignored
    return polls


def test_lab_feature_signs():
    F = v.of_features(_lab_polls("2026-10-06")).set_index("symbol")
    for c in ("ofs", "imb_avg", "imb_last", "d5_avg", "ord5_avg", "big_side", "dr", "pre_imb", "fut_imb", "fut_dr", "buildup", "mv"):
        assert F.loc["BUY", c] > 0 > F.loc["SELL", c], c
    assert set(v.LAB_FEATURES) <= set(F.columns) and np.isnan(F.loc["FLAT", "buildup"])


def test_lab_pipeline_poll_labels_leaderboard_export(store, monkeypatch):
    day = dt.date(2026, 10, 6)
    hist = {s: {"token": i + 1} for i, s in enumerate(["S%02d" % i for i in range(40)])}
    v._write_json(f"prep_{day}.json", {"date": str(day), "prev_session": "2026-10-05", "hist": hist})
    v._write_json(f"futs_{day}.json", {s: s + "FUT" for s in hist})

    class K:
        calls = 0
        def quote(self, keys):
            K.calls += 1; out = {}
            for k in keys:
                i = int(k.split(":")[1][1:3]); up = i % 2 == 0
                out[k] = _q(100.0 + (0.5 if up else -0.5), 1000 * K.calls, 900 if up else 100, 100 if up else 900, oi=1000 + K.calls)
            return out
    kk = K()
    v._of_buf.clear()
    for ph in ("pre", "fast", "fast", "slow"):
        assert v.run_of_poll(kk, day, ph) == 40
    st = v.of_status(str(day))
    assert st["polls"] == 4 and st["stocks"] == 40 and st["futures"] == 40 and st["by_phase"] == {"pre": 1, "fast": 2, "slow": 1}
    polls = v._read_of(day)
    assert len(polls) == 4 and len(polls[1]["f"]) == 40
    for p, t in zip(polls, ("09:10:00", "09:15:00", "09:20:00", "09:45:00")): p["t"] = f"{day}T{t}"
    v._path(f"of_{day}.jsonl").write_text("".join(json.dumps(p) + "\n" for p in polls))

    def fake_5min(kite, token, a, b, tries=3):         # even stocks rise all day, odd stocks fall: order flow is right
        up = (token - 1) % 2 == 0
        return _bars(day, 100.0, seed=token, drift=0.002 if up else -0.002)
    monkeypatch.setattr(v, "_kite_5min", fake_5min)
    assert v.run_labels(kk, day) == 1
    assert v._path(f"bars_{day}.json.gz").exists() and v._path(f"of_{day}.jsonl.gz").exists() and not v._path(f"of_{day}.jsonl").exists()
    assert len(v._read_of(day)) == 4                     # packed file still readable
    lab = v.lab_summary()
    assert lab["sessions"] == 1 and lab["table"]
    imb = next(r for r in lab["table"] if r["feature"] == "imb_avg")["h"]["day"]
    assert imb["hit"] == 100.0 and imb["calls"] == 10
    assert v.run_labels(kk, day) == 1                    # idempotent: labels are not downloaded twice
    import zipfile
    names = zipfile.ZipFile(v.export_lab_zip()).namelist()
    assert f"of_{day}.jsonl.gz" in names and f"bars_{day}.json.gz" in names and "of_lab.json" in names


def test_list_saves_every_stock_scores(store, monkeypatch):
    test_end_to_end_list_and_outcomes(store, monkeypatch)
    sc = v._read_gz("scores_2026-10-06.json.gz", None)
    assert sc and len(sc["data"]) == 40 and all(f"p_{h}" in sc["columns"] for h in v.HORIZONS)
