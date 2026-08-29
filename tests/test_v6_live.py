import pandas as pd
import pytest
import sys
import types

if 'kiteconnect' not in sys.modules:
    mod = types.ModuleType('kiteconnect')
    class KiteConnect:  # pragma: no cover - import shim only
        pass
    mod.KiteConnect = KiteConnect
    sys.modules['kiteconnect'] = mod

from app import background, scanner


def test_fetch_oi_map_carries_near_future_last_price(monkeypatch):
    contracts = {
        "ABC": [
            {"tradingsymbol": "ABCSEP26FUT", "expiry": pd.Timestamp("2026-09-29").date()},
            {"tradingsymbol": "ABCOCT26FUT", "expiry": pd.Timestamp("2026-10-27").date()},
        ]
    }
    monkeypatch.setattr(scanner, "_load_fut_contracts_map", lambda kite: contracts)

    class Kite:
        def quote(self, keys):
            out = {}
            for k in keys:
                out[k] = {
                    "oi": 1000 if "SEP" in k else 800,
                    "oi_day_high": 1100,
                    "oi_day_low": 700,
                    "last_price": 102.5 if "SEP" in k else 103.0,
                    "depth": {"buy": [{"price": 102.4, "quantity": 100}], "sell": [{"price": 102.6, "quantity": 80}]},
                }
            return out

    out = scanner.fetch_oi_map(Kite(), ["ABC"])
    assert out["ABC"]["fut_price_near"] == pytest.approx(102.5)
    assert out["ABC"]["oi_total"] == 1800


def test_cross_sectional_context_ranks_turnover_catalyst_sector_and_regime():
    results = [
        {"symbol": "A", "close": 100, "prev_close": 99, "volume": 10000, "tod_rvol": 2.0,
         "opening_rvol": 1.8, "gap_atr": 0.7, "bar_range_atr": 1.1, "sector": "S1",
         "prior_high_20d": 101, "prior_low_20d": 80, "prior_high_50d": 103, "prior_low_50d": 70,
         "breakout_direction": "Bullish"},
        {"symbol": "B", "close": 50, "prev_close": 50, "volume": 1000, "tod_rvol": 0.8,
         "opening_rvol": 0.7, "gap_atr": 0.05, "bar_range_atr": 0.4, "sector": "S2",
         "prior_high_20d": 60, "prior_low_20d": 40, "prior_high_50d": 65, "prior_low_50d": 35,
         "breakout_direction": "Bullish"},
    ]
    breadth = {"bullish_pct": 70.0, "bearish_pct": 30.0}
    sector_contexts = {
        "S1": {"direction": "Bullish", "chg_pct": 0.8},
        "S2": {"direction": "Bearish", "chg_pct": -0.3},
    }
    background._apply_v6_cross_sectional_context(
        results, index_chg_pct=0.6, breadth=breadth, sector_contexts=sector_contexts
    )
    assert results[0]["turnover_percentile"] > results[1]["turnover_percentile"]
    assert results[0]["catalyst_score"] > results[1]["catalyst_score"]
    assert results[0]["market_regime"] == "Trend Up"
    assert results[0]["sector_rank_percentile"] > results[1]["sector_rank_percentile"]
    assert results[0]["stock_sector_lead_pct"] > 0
    assert results[0]["price_location_score"] >= 80


def test_basis_trend_computes_basis_and_acceleration_without_oi_gate():
    history = {}
    rows = [{"symbol": "ABC", "close": 100.0, "fut_price_near": 100.2}]
    background._apply_v6_basis(rows, history=history, now=pd.Timestamp("2026-08-29 10:00"))
    assert rows[0]["basis_pct"] == pytest.approx(0.2)
    rows[0]["fut_price_near"] = 100.5
    background._apply_v6_basis(rows, history=history, now=pd.Timestamp("2026-08-29 10:30"))
    assert rows[0]["basis_acceleration"] > 0


def test_v6_shortlist_allows_basis_sponsorship_when_oi_does_not_confirm(monkeypatch):
    r = {
        "symbol": "ABC", "close": 100, "prev_close": 99,
        "breakout_direction": "Bullish", "breakout_source": "Recent Range",
        "fresh_breakout": True, "breakout_retained": True, "breakout_retest_confirmed": True,
        "breakout_level": 99.5, "breakout_extension_atr": 0.5,
        "breakout_vwap_agrees": True, "breakout_entry_extended": False,
        "tod_rvol": 1.8, "turnover_percentile": 95, "catalyst_score": 85,
        "sector_rank_percentile": 90, "stock_sector_lead_pct": 0.7,
        "price_location_score": 90, "market_regime": "Trend Up",
        "oi_recent_agrees": False, "basis_pct": 0.25, "basis_acceleration": 0.12,
        "htf_direction": "Bullish", "sector_direction": "Bullish",
        "execution_5m_quality": 80,
    }
    intraday, swing = background._apply_v6_shortlists([r])
    assert r["intraday_eligible"] is True
    assert any(x["symbol"] == "ABC" for x in intraday)
    assert r["movement_stage"] in ("V6 Intraday Entry", "V6 Swing 1-2D")


def test_five_minute_enrichment_is_bounded_to_top_finalists(monkeypatch):
    rows = []
    for i in range(12):
        rows.append({
            "symbol": f"S{i}", "breakout_source": "Recent Range", "breakout_direction": "Bullish",
            "breakout_level": 100.0, "atr": 1.0, "movement_score": 90-i,
            "fresh_breakout": True, "error": None,
        })
    monkeypatch.setattr(scanner, "_load_instrument_map", lambda kite: {f"S{i}": i+1 for i in range(12)})
    calls = []
    idx = pd.date_range("2026-08-29 10:45", periods=8, freq="5min")
    df = pd.DataFrame({
        "open": [100.1]*8, "high": [100.6]*8, "low": [99.95]*8,
        "close": [100.4]*8, "volume": [100,120,200,150,160,170,180,190],
    }, index=idx)
    def fake_fetch(kite, token, timeframe):
        calls.append((token, timeframe))
        return df
    monkeypatch.setattr(scanner, "fetch_candles", fake_fetch)
    background._enrich_v6_execution_5m(object(), rows, max_candidates=5,
                                       signal_time=pd.Timestamp("2026-08-29 10:45"))
    assert len(calls) == 5
    assert all(tf == "5minute" for _tok, tf in calls)
    assert sum(r.get("execution_5m_quality") is not None for r in rows) == 5
