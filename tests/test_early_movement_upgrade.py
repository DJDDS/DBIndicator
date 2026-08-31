import datetime as dt
import sys
import types

import numpy as np
import pandas as pd
import pytest

if 'kiteconnect' not in sys.modules:
    mod = types.ModuleType('kiteconnect')
    class KiteConnect:  # pragma: no cover
        pass
    mod.KiteConnect = KiteConnect
    sys.modules['kiteconnect'] = mod

from app import backtest, background, indicators, scanner
from app.config import settings
import app.config as config


def test_live_engine_defaults_to_fno_15minute_and_small_shortlist():
    assert config.WATCHLIST_TIMEFRAME == '15minute'
    assert settings.RSI_LENGTH == 14
    assert settings.MACD_CUSTOM_FAST == 8
    assert settings.MACD_CUSTOM_SLOW == 17
    assert settings.MACD_CUSTOM_SIGNAL == 9
    assert settings.SHORTLIST_MAX == 5
    assert settings.MAX_ENTRY_EXTENSION_ATR == pytest.approx(1.25)


def test_scan_watchlist_can_use_explicit_fno_universe(monkeypatch):
    monkeypatch.setattr(scanner, '_load_instrument_map', lambda _k: {'AAA': 1, 'BBB': 2})
    monkeypatch.setattr(scanner, 'fetch_oi_map', lambda _k, symbols: {s: {'oi': 10} for s in symbols})
    monkeypatch.setattr(scanner, 'fetch_candles', lambda *_a, **_k: pd.DataFrame({'close': [1]}))
    monkeypatch.setattr(scanner, 'compute_signal', lambda *_a, **_k: {'direction': 'Bullish'})
    rows = scanner.scan_watchlist(object(), symbols=['AAA', 'BBB'])
    assert [r['symbol'] for r in rows] == ['AAA', 'BBB']


class _FakeKite:
    def instruments(self, exchange):
        assert exchange == 'NFO'
        return [
            {'instrument_type': 'FUT', 'name': 'AAA', 'expiry': dt.date(2026, 9, 29), 'tradingsymbol': 'AAASEP', 'instrument_token': 1},
            {'instrument_type': 'FUT', 'name': 'AAA', 'expiry': dt.date(2026, 10, 27), 'tradingsymbol': 'AAAOCT', 'instrument_token': 2},
            {'instrument_type': 'FUT', 'name': 'AAA', 'expiry': dt.date(2026, 11, 24), 'tradingsymbol': 'AAANOV', 'instrument_token': 3},
            {'instrument_type': 'FUT', 'name': 'AAA', 'expiry': dt.date(2026, 12, 29), 'tradingsymbol': 'AAADEC', 'instrument_token': 4},
        ]

    def quote(self, keys):
        vals = {
            'NFO:AAASEP': {'oi': 100, 'oi_day_high': 120, 'oi_day_low': 80},
            'NFO:AAAOCT': {'oi': 60, 'oi_day_high': 70, 'oi_day_low': 50},
            'NFO:AAANOV': {'oi': 40, 'oi_day_high': 50, 'oi_day_low': 30},
        }
        return {k: vals[k] for k in keys if k in vals}


def test_live_oi_map_aggregates_first_three_expiries_but_preserves_near_oi(monkeypatch):
    scanner._fut_map_cache.update({'date': None, 'map': {}, 'tokens': {}})
    if hasattr(scanner, '_fut_contracts_cache'):
        scanner._fut_contracts_cache.update({'date': None, 'map': {}})
    out = scanner.fetch_oi_map(_FakeKite(), ['AAA'])['AAA']
    assert out['oi'] == 100                  # near-month compatibility for historical z-score
    assert out['oi_near'] == 100
    assert out['oi_next'] == 60
    assert out['oi_far'] == 40
    assert out['oi_total'] == 200
    assert [x['tradingsymbol'] for x in out['contracts']] == ['AAASEP', 'AAAOCT', 'AAANOV']


def _intraday_volume_frame(days=8):
    idx = []
    vol = []
    start = pd.Timestamp('2026-08-10')
    for d in range(days):
        day = start + pd.Timedelta(days=d)
        if day.weekday() >= 5:
            continue
        # opening slot naturally 4x midday; latest day has normal open and 2x midday ignition
        idx.extend([day.replace(hour=9, minute=15), day.replace(hour=12, minute=0)])
        vol.extend([4000, 1000])
    df = pd.DataFrame({'volume': vol}, index=pd.DatetimeIndex(idx))
    df.iloc[-1, 0] = 2000
    return df


def test_time_of_day_rvol_adjusts_for_intraday_seasonality():
    df = _intraday_volume_frame()
    rv = indicators.time_of_day_rvol(df, lookback_sessions=4)
    # Latest noon volume is 2x its own noon baseline even though it is below a normal opening bar.
    assert rv.iloc[-1] == pytest.approx(2.0, rel=0.05)
    # Prior opening bar is normal versus prior openings.
    opening = rv[df.index.time == dt.time(9, 15)].dropna()
    assert opening.iloc[-1] == pytest.approx(1.0, rel=0.05)


def test_time_of_day_rvol_scales_forming_bar_by_elapsed_fraction():
    idx = pd.DatetimeIndex([
        '2026-08-26 10:00', '2026-08-27 10:00', '2026-08-28 10:00'
    ])
    df = pd.DataFrame({'volume': [1500, 1500, 500]}, index=idx)
    now = dt.datetime(2026, 8, 28, 10, 5)
    rv = indicators.time_of_day_rvol(df, lookback_sessions=2, now=now, interval_minutes=15)
    # 500 after 5/15 of the bar is exactly on a 1500 full-bar pace.
    assert rv.iloc[-1] == pytest.approx(1.0, rel=0.05)


def test_best_entry_score_requires_independent_fresh_evidence():
    from app.early_movement import score_candidate
    good = {
        'direction': 'Bullish', 'entry_trigger': 'Bullish', 'entry_trigger_bars_ago': 0,
        'trend_state': 'Bullish', 'macd_agrees': True, 'macd_hist_agrees': True,
        'htf_agrees': True, 'vwap_side_agrees': True, 'entry_is_extended': False,
        'oi_agrees': True, 'oi_z': 2.0, 'oi_chg_30m_pct': 1.0, 'oi_chg_60m_pct': 1.8,
        'oi_acceleration': 0.5, 'tod_rvol': 1.6, 'tod_rvol_accel': 1.2,
        'rs_pct': 1.0, 'rs_improving': True, 'sector_agrees': True,
        'vol_contracting_recent': True, 'breakout_state': 'Breakout',
    }
    s = score_candidate(good)
    assert s['eligible'] is True
    assert s['score'] >= 75
    assert not s['blockers']

    late = dict(good, entry_trigger_bars_ago=3, entry_is_extended=True)
    s2 = score_candidate(late)
    assert s2['eligible'] is False
    assert 'stale trigger' in s2['blockers']
    assert 'extended entry' in s2['blockers']


@pytest.mark.parametrize('direction,rs', [('Bullish', -0.5), ('Bearish', 0.5)])
def test_best_entry_rejects_wrong_direction_relative_strength(direction, rs):
    from app.early_movement import score_candidate
    row = {
        'direction': direction, 'entry_trigger': direction, 'entry_trigger_bars_ago': 0,
        'trend_state': direction, 'macd_agrees': True, 'macd_hist_agrees': True,
        'htf_agrees': True, 'vwap_side_agrees': True, 'entry_is_extended': False,
        'oi_agrees': True, 'oi_z': 2.0, 'oi_chg_30m_pct': 0.8, 'oi_chg_60m_pct': 1.2,
        'oi_acceleration': 0.2, 'tod_rvol': 1.4, 'tod_rvol_accel': 1.0,
        'rs_pct': rs, 'rs_improving': False, 'sector_agrees': True,
    }
    out = score_candidate(row)
    assert out['eligible'] is False
    assert 'relative strength against trade' in out['blockers']


def test_best_entry_rejects_missing_or_fading_oi():
    from app.early_movement import score_candidate
    base = {
        'direction': 'Bullish', 'entry_trigger': 'Bullish', 'entry_trigger_bars_ago': 0,
        'trend_state': 'Bullish', 'macd_agrees': True, 'htf_agrees': True,
        'vwap_side_agrees': True, 'entry_is_extended': False,
        'tod_rvol': 1.4, 'tod_rvol_accel': 1.0, 'rs_pct': 0.8, 'rs_improving': True,
        'sector_agrees': True,
    }
    assert score_candidate(dict(base, oi_agrees=None, oi_chg_60m_pct=None, oi_acceleration=None))['eligible'] is False
    assert score_candidate(dict(base, oi_agrees=True, oi_z=2, oi_chg_30m_pct=0.2, oi_chg_60m_pct=-0.2, oi_acceleration=-0.4))['eligible'] is False


def test_live_dashboard_retires_btst_candidates_and_legacy_best_entry_drivers():
    text = open('app/templates/index.html', encoding='utf-8').read()
    assert 'BTST / STBT' not in text
    assert 'Production model status' in text
    assert 'V9.2 Live F&amp;O Monitor' in text
    for legacy in ('V6 Intraday Entry', 'V6 Swing 1-2D', 'Swing remains long-only'):
        assert legacy not in text


def test_live_loop_no_longer_publishes_btst_candidates():
    text = open('app/background.py', encoding='utf-8').read()
    assert 'alerts.publish_btst_candidates(results' not in text
    assert '_apply_btst_candidates(results)' not in text


def test_research_promotion_requires_positive_holdout_expectancy_profit_factor_and_sample():
    from app.backtest import research_promotable
    assert research_promotable({'trade_count': 80, 'avg_return_pct': 0.08, 'profit_factor': 1.2}) is True
    assert research_promotable({'trade_count': 80, 'avg_return_pct': -0.01, 'profit_factor': 1.4}) is False
    assert research_promotable({'trade_count': 80, 'avg_return_pct': 0.08, 'profit_factor': 1.05}) is False
    assert research_promotable({'trade_count': 20, 'avg_return_pct': 0.08, 'profit_factor': 1.4}) is False


def test_oi_radar_is_fno_wide_not_blocked_by_legacy_parameter_tier():
    from app.oi_view import select_oi_screener_rows, oi_history_readiness
    rows = [
        {'symbol': 'EARLY', 'param_tier': 0, 'oi_total': 1500, 'oi': 500, 'oi_z': 0.2,
         'oi_chg_60m_pct': 1.4, 'oi_acceleration': 0.5, 'oi_chg_30m_pct': 0.9},
        {'symbol': 'LATE', 'param_tier': 4, 'oi_total': 1000, 'oi': 400, 'oi_z': 0.1,
         'oi_chg_60m_pct': 0.3, 'oi_acceleration': 0.1, 'oi_chg_30m_pct': 0.2},
        {'symbol': 'NOOI', 'param_tier': 4, 'oi_total': None, 'oi': None},
    ]
    selected = select_oi_screener_rows(rows, unusual_only=False, min_tier=None)
    assert [r['symbol'] for r in selected] == ['EARLY', 'LATE']
    status = oi_history_readiness(rows, min_tier=None)
    assert status['eligible_with_oi'] == 2


def test_dashboard_copy_and_controls_reflect_early_movement_engine_not_legacy_vote_count():
    text = open('app/templates/index.html', encoding='utf-8').read()
    assert 'F&O Early Movement' in text
    assert '15-min execution' in text
    assert 'Watchlist: <strong>daily</strong>' not in text
    assert 'Match required' not in text
    assert 'Matching Now' not in text
    assert 'RSI + MACD + CMF + Rel Volume' not in text


def test_live_loop_skips_legacy_best_entry_filters_and_delivery_refresh():
    text = open('app/background.py', encoding='utf-8').read()
    live = text[text.index('def _run_loop'):]
    for legacy_call in (
        'delivery.refresh_if_stale(',
        '_apply_param_tier(results)',
        '_apply_candle_pattern_filter(results)',
        '_apply_macd_hist_filter(results)',
        '_apply_big_candle_filter(results)',
        '_apply_strong_close_filter(results)',
        '_apply_entry_location_filter(results)',
        '_apply_atr_floor_filter(results)',
        '_apply_delivery_filter(results)',
        '_apply_breadth_filter(results',
        '_apply_weighted_score(results)',
    ):
        assert legacy_call not in live


def test_oi_page_is_positioning_radar_not_legacy_parameter_tier_view():
    text = open('app/templates/oi_screener.html', encoding='utf-8').read()
    assert 'all current NSE stock-F&O contracts with valid live OI' in text
    assert 'All Tiers' not in text
    assert 'data-tier=' not in text
    assert '>Tier<' not in text
    assert 'match 2+' not in text.lower()


def test_best_entry_alert_uses_new_evidence_not_legacy_alignment_count():
    from app.alerts import _format_message
    row = {
        'symbol': 'AAA', 'entry_trigger': 'Bullish', 'direction': 'Bullish', 'close': 100,
        'movement_score': 82.0, 'oi_chg_60m_pct': 1.4, 'oi_acceleration': 0.3,
        'tod_rvol': 1.5, 'rs_pct': 0.8, 'htf_direction': 'Bullish',
        'aligned': 4, 'rsi': 60, 'rsi_state': 'Bullish', 'macd_params': (8,17,9),
        'macd_state': 'Bullish', 'vol_multiple': 2.0,
    }
    msg = _format_message(row, '15minute')
    assert 'Score 82.0' in msg
    assert 'OI60 +1.40%' in msg
    assert 'TOD RVOL 1.5x' in msg
    assert 'Aligned' not in msg
    assert 'CMF' not in msg


def test_settings_only_exposes_and_saves_live_engine_controls():
    tpl = open('app/templates/settings.html', encoding='utf-8').read()
    web = open('app/web.py', encoding='utf-8').read()
    assert 'Indicator internals' not in tpl
    assert 'id="rsi_length"' not in tpl
    assert 'id="rsi_smooth_length"' not in tpl
    for field in ('compression_radar_score', 'tod_rvol_min', 'tod_rvol_strong_no_oi',
                  'max_entry_extension_atr', 'shortlist_max', 'scan_interval_seconds'):
        assert f'name="{field}"' in tpl
    assert '"COMPRESSION_RADAR_SCORE": form.get("compression_radar_score"' in web
    assert '"TOD_RVOL_MIN": form.get("tod_rvol_min"' in web
    assert '"SHORTLIST_MAX": form.get("shortlist_max"' in web
    assert '"SCAN_INTERVAL_SECONDS": form.get("scan_interval_seconds"' in web


def test_alert_message_includes_v82_option_expression_when_available():
    from app.alerts import _format_message
    row = {
        'symbol':'ABC','trade_direction':'Bullish','close':100,'movement_score':92,
        'breakout_source':'Recent Range','option_action':'OPTION BUYER EDGE','option_edge':'HIGH',
        'option_contract':'ABCSEP100CE','option_iv_rv_ratio':0.94,'option_spread_pct':1.2,
        'option_dte':12,
    }
    msg = _format_message(row, '15minute')
    assert 'OPTION BUYER EDGE' in msg
    assert 'ABCSEP100CE' in msg
    assert 'IV/RV 0.94x' in msg
