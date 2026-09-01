import datetime as dt
from pathlib import Path

import pytest

from app import opportunity_forward, oi_view

ROOT = Path(__file__).resolve().parents[1]


def _radar_row(symbol='ABC', direction='Bullish', score=80, **kw):
    row = {
        'symbol': symbol,
        'direction': direction,
        'score': score,
        'status': 'HIGH ATTENTION',
        'oi_structure': 'Long Buildup' if direction == 'Bullish' else 'Short Buildup',
        'price_chg_pct': 0.6 if direction == 'Bullish' else -0.6,
        'oi_day_chg_pct': 5.0,
        'oi_30m_chg_pct': 0.8,
        'oi_60m_chg_pct': 1.2,
        'tod_rvol': 1.8,
        'relative': 80,
        'participation': 78,
        'technical': 70,
        'htf_direction': direction,
        'vwap_agrees': True,
        'extension_atr': 0.6,
        'chase_guard': 'OK',
        'compression_score': 70,
        'shadow_movement_stage': 'Ignition',
        'oi_z': 1.0,
        'price_move_60m_atr': 0.7,
    }
    row.update(kw)
    return row


def test_v930_btst_and_old_gate_sweep_are_removed_from_backtest_ui():
    text = (ROOT / 'app/templates/backtest.html').read_text(encoding='utf-8')
    assert 'Does the BTST/STBT premise hold?' not in text
    assert 'Run the overnight test' not in text
    assert 'id="on-run"' not in text
    assert 'Legacy gate audit' not in text
    assert 'Run gate sweep' not in text
    assert 'Component Edge Laboratory' in text


def test_v930_overnight_routes_are_removed_from_web_surface():
    text = (ROOT / 'app/web.py').read_text(encoding='utf-8')
    assert '/api/overnight/start' not in text
    assert '/api/overnight/status' not in text


def test_v930_legacy_4h_diagnostic_has_dedicated_fixed_mode():
    html = (ROOT / 'app/templates/backtest.html').read_text(encoding='utf-8').replace(' ', '')
    web = (ROOT / 'app/web.py').read_text(encoding='utf-8')
    assert "mode:'legacy_4h'" in html
    assert "timeframe:'4hour'" in html
    assert '"legacy_4h"' in web
    assert 'if mode == "legacy_4h":' in web


def test_v930_forward_validator_matures_real_second_trading_session_2d():
    start = dt.datetime(2026, 8, 28, 10, 0)  # Friday
    state = opportunity_forward.empty_state()
    radar = {'bullish': [_radar_row()], 'bearish': [], 'market_bias': 'Bullish', 'market_bias_strength_pct': 20}
    state = opportunity_forward.process_scan(state, radar, [{'symbol': 'ABC', 'close': 100.0}], now=start)

    # Monday is the first later trading session => 1D.
    state = opportunity_forward.process_scan(
        state, {'bullish': [], 'bearish': []}, [{'symbol': 'ABC', 'close': 102.0}],
        now=dt.datetime(2026, 8, 31, 10, 5),
    )
    event = state['events'][0]
    assert '1D' in event['outcomes']
    assert '2D' not in event['outcomes']

    # Tuesday is the second later trading session => 2D.
    state = opportunity_forward.process_scan(
        state, {'bullish': [], 'bearish': []}, [{'symbol': 'ABC', 'close': 104.0}],
        now=dt.datetime(2026, 9, 1, 10, 5),
    )
    event = state['events'][0]
    assert event['outcomes']['2D']['directional_return_pct'] == 4.0
    summary = opportunity_forward.summarize(state)
    assert '2D' in summary['horizons']
    assert summary['horizons']['2D']['n'] == 1


def test_v930_dashboard_forward_summary_renders_2d():
    text = (ROOT / 'app/templates/index.html').read_text(encoding='utf-8')
    assert "['30m','1h','2h','4h','1D','2D']" in text


def test_v930_swing_research_console_routes_each_symbol_to_one_horizon_only():
    radar = {
        'market_bias': 'Bullish',
        'market_bias_strength_pct': 25,
        'bullish': [
            _radar_row('IGNITE', shadow_movement_stage='Ignition', oi_z=1.0, price_move_60m_atr=0.8),
            _radar_row('BUILD', shadow_movement_stage='Energy Building', oi_z=2.0, price_move_60m_atr=0.25, compression_score=85),
        ],
        'bearish': [],
    }
    out = oi_view.swing_research_console(radar, limit=5)
    one = out['1D']['bullish']
    two = out['2D']['bullish']
    assert one and one[0]['symbol'] == 'IGNITE'
    assert two and two[0]['symbol'] == 'BUILD'
    symbols_1d = {x['symbol'] for x in one}
    symbols_2d = {x['symbol'] for x in two}
    assert symbols_1d.isdisjoint(symbols_2d)
    assert out['is_trade_signal'] is False
    assert out['label'] == 'RESEARCH / SHADOW'


def test_v930_v8_dashboard_exposes_swing_research_console():
    text = (ROOT / 'app/web.py').read_text(encoding='utf-8')
    html = (ROOT / 'app/templates/index.html').read_text(encoding='utf-8')
    assert 'payload["swing_research"]' in text
    assert 'Swing Research / Shadow' in html
    assert '1D Attention' in html
    assert '2D Attention' in html
    assert 'swing-forward-evidence' in html
    assert 'by_research_horizon' in html


def test_v930_forward_validator_persists_research_horizon_and_summarizes_it():
    start = dt.datetime(2026, 8, 28, 10, 0)
    state = opportunity_forward.empty_state()
    radar = {'bullish': [_radar_row('SW1')], 'bearish': [], 'market_bias': 'Bullish', 'market_bias_strength_pct': 20}
    swing = {
        '1D': {'bullish': [{**_radar_row('SW1'), 'research_horizon': '1D'}], 'bearish': []},
        '2D': {'bullish': [], 'bearish': []},
    }
    state = opportunity_forward.process_scan(
        state, radar, [{'symbol': 'SW1', 'close': 100.0}], now=start, swing_research=swing
    )
    assert state['events'][0]['research_horizon'] == '1D'
    state = opportunity_forward.process_scan(
        state, {'bullish': [], 'bearish': []}, [{'symbol': 'SW1', 'close': 102.0}],
        now=dt.datetime(2026, 8, 31, 10, 5), swing_research={'1D': {'bullish': [], 'bearish': []}, '2D': {'bullish': [], 'bearish': []}}
    )
    summary = opportunity_forward.summarize(state)
    assert summary['by_research_horizon']['1D']['1D']['n'] == 1
    assert summary['by_research_horizon']['1D']['1D']['avg_net_return_pct'] == pytest.approx(1.82)


def test_v930_user_facing_templates_remove_btst_language():
    visible = '\n'.join(
        (ROOT / path).read_text(encoding='utf-8')
        for path in ('app/templates/index.html', 'app/templates/settings.html', 'app/templates/backtest.html')
    )
    assert 'BTST/STBT' not in visible
    assert 'BTST meaning' not in visible
    assert "is_btst_timeframe" not in visible


def test_v930_rendered_dashboard_javascript_is_valid(tmp_path):
    import re
    import shutil
    import subprocess
    from jinja2 import Environment, FileSystemLoader

    node = shutil.which('node')
    if node is None:
        pytest.skip('node is required for rendered dashboard JavaScript syntax check')
    env = Environment(loader=FileSystemLoader(ROOT / 'app/templates'))
    html = env.get_template('index.html').render(
        _=lambda x: x, logged_in=True, login_url=None, results=[], total_scanned=210, valid_scanned=210,
        scan_errors=0, scan_failures=[],
        live_counts={'universe': 210, 'intraday_trade': 0, 'intraday_watch': 0, 'swing_trade': 0, 'swing_watch': 0},
        last_scan='2026-08-31T15:30:00', last_error=None, timeframe_label='15-minute', is_daily_timeframe=False,
        quick_error=None, insights_enabled=False, telegram_enabled=False,
        atr_length=14, max_entry_extension_atr=1.25, min_atr_pct=1.2, min_required=2,
        vol_contraction_lookback=20, risk_budget={'risk_per_trade': 1000}, oipart=0, volpart=0,
    )
    scripts = re.findall(r'<script>(.*?)</script>', html, re.S)
    js_path = tmp_path / 'index-rendered.js'
    js_path.write_text('\n'.join(scripts), encoding='utf-8')
    result = subprocess.run([node, '--check', str(js_path)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
