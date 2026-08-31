import datetime as dt
import json
from pathlib import Path

from app import config, oi_view, scanner

ROOT = Path(__file__).resolve().parents[1]


def _row(symbol, *, oi_structure=None, price=0.0, sector=None, sector_direction=None,
         rs=None, vs_vwap=None, close=100.0, **extra):
    row = {
        "symbol": symbol,
        "oi": 100,
        "oi_structure": oi_structure,
        "price_chg_today_pct": price,
        "sector": sector,
        "sector_direction": sector_direction,
        "rs_pct": rs,
        "vs_vwap": vs_vwap,
        "close": close,
    }
    row.update(extra)
    return row


def test_fno_universe_drops_derivative_names_without_nse_cash_symbol(monkeypatch):
    class Kite:
        def instruments(self, exchange):
            if exchange == "NFO":
                return [
                    {"instrument_type": "FUT", "name": "RELIANCE"},
                    {"instrument_type": "FUT", "name": "NIFTYFPI"},
                ]
            if exchange == "NSE":
                return [
                    {"segment": "NSE", "tradingsymbol": "RELIANCE", "instrument_token": 1},
                ]
            raise AssertionError(exchange)

    monkeypatch.setattr(scanner, "_fno_cache", {"date": None, "symbols": []})
    monkeypatch.setattr(scanner, "_instrument_cache", {})
    assert scanner.get_fno_stock_list(Kite()) == ["RELIANCE"]


def test_settings_v927_migrates_persisted_chase_limit_back_to_125(tmp_path, monkeypatch):
    settings_file = tmp_path / "scanner_settings.json"
    settings_file.write_text(json.dumps({
        "_schema_version": 3,
        "MAX_ENTRY_EXTENSION_ATR": 2.0,
        "WATCHLIST": ["RELIANCE", "NIFTYFPI"],
    }))
    monkeypatch.setattr(config, "SETTINGS_FILE", str(settings_file))

    s = config.Settings()

    assert config.SETTINGS_SCHEMA_VERSION >= 4
    assert s.MAX_ENTRY_EXTENSION_ATR == 1.25
    assert s.WATCHLIST == ["RELIANCE"]
    saved = json.loads(settings_file.read_text())
    assert saved["MAX_ENTRY_EXTENSION_ATR"] == 1.25
    assert saved["_schema_version"] == config.SETTINGS_SCHEMA_VERSION


def test_settings_rejects_chase_limit_above_125(tmp_path, monkeypatch):
    settings_file = tmp_path / "scanner_settings.json"
    monkeypatch.setattr(config, "SETTINGS_FILE", str(settings_file))
    s = config.Settings()

    errors = s.update(MAX_ENTRY_EXTENSION_ATR=2.0)

    assert errors
    assert s.MAX_ENTRY_EXTENSION_ATR == 1.25


def test_market_regime_combines_index_price_breadth_oi_sector_rs_and_vwap():
    rows = [
        _row("A", oi_structure="Short Buildup", price=-1.0, sector="AUTO", sector_direction="Bearish", rs=-1.0, vs_vwap="Below"),
        _row("B", oi_structure="Short Buildup", price=-0.8, sector="BANK", sector_direction="Bearish", rs=-0.7, vs_vwap="Below"),
        _row("C", oi_structure="Long Unwinding", price=-0.5, sector="IT", sector_direction="Bearish", rs=-0.4, vs_vwap="Below"),
        _row("D", oi_structure="Long Buildup", price=0.2, sector="PHARMA", sector_direction="Bullish", rs=0.1, vs_vwap="Above"),
    ]
    market = oi_view.live_market_state(
        rows,
        index_direction="Bearish",
        index_chg_pct=-0.85,
        market_breadth={"bullish_pct": 25.0, "bearish_pct": 75.0},
    )

    assert market["bias"] == "Bearish"
    assert market["bias_strength_pct"] >= 40
    assert market["regime_score"] < 0
    assert set(market["regime_factors"]) == {"index", "price_breadth", "oi_breadth", "sector_breadth", "relative_strength", "vwap"}
    assert market["regime_factors"]["index"]["score"] < 0
    assert market["regime_factors"]["oi_breadth"]["score"] < 0
    assert market["regime_factors"]["sector_breadth"]["score"] < 0


def test_opportunity_radar_uses_multifactor_regime_as_bonus_not_veto():
    rows = [
        _row("BEAR", oi_structure="Short Buildup", price=-0.8, sector="AUTO", sector_direction="Bearish", rs=-1.0,
             vs_vwap="Below", oi_day_chg_pct=7.0, oi_chg_30m_pct=2.0, vol_multiple=1.8,
             v8_relative=90, v8_participation=80, v8_structure=70),
        _row("BULL", oi_structure="Long Buildup", price=0.8, sector="AUTO", sector_direction="Bearish", rs=1.0,
             vs_vwap="Above", oi_day_chg_pct=7.0, oi_chg_30m_pct=2.0, vol_multiple=1.8,
             v8_relative=90, v8_participation=80, v8_structure=70),
    ]
    radar = oi_view.live_opportunity_radar(
        rows,
        index_direction="Bearish",
        index_chg_pct=-1.0,
        market_breadth={"bullish_pct": 20.0, "bearish_pct": 80.0},
    )
    assert radar["market_bias"] == "Bearish"
    assert radar["market_bias_strength_pct"] > 0
    assert radar["bearish"][0]["score"] > radar["bullish"][0]["score"]
    assert radar["bullish"]  # regime is context, never a veto


def test_dashboard_template_exposes_multifactor_bias_and_forward_validation():
    text = (ROOT / "app/templates/index.html").read_text(encoding="utf-8")
    assert "Regime Bias" in text
    assert 'id="lms-regime-factors"' in text
    assert 'id="opportunity-forward"' in text
    assert "renderOpportunityForward" in text
