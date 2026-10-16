"""V13.8 movement-predictor trade plan: two-sided bracket on the high-confidence top pick (paper)."""
import datetime as dt
import math

import pandas as pd
import pytest

from app import v132_movers as m

EXC = 0.02          # normal day move 2% -> triggers at +-1%, target 0.6% beyond the trigger


def _bars(rows):
    return [[k, o, h, l, c] for k, o, h, l, c in rows]


def test_levels_around_0920_price():
    lv = m.plan_levels(100.0, EXC)
    assert lv['buy_above'] == round(100 * math.exp(0.01), 2) and lv['sell_below'] == round(100 * math.exp(-0.01), 2)
    assert lv['long_stop'] == lv['sell_below'] and lv['short_stop'] == lv['buy_above']
    assert lv['long_target'] > lv['buy_above'] and lv['short_target'] < lv['sell_below']


def test_long_trigger_then_target():
    p = m.plan_walk(_bars([(0, 100, 100.2, 99.8, 100), (1, 100, 100.5, 99.9, 100.4), (2, 100.4, 101.2, 100.3, 101.1), (3, 101.1, 102.0, 101.0, 101.9)]), EXC)
    assert p['status'] == 'TARGET' and p['side'] == 'LONG' and p['entry_at'] == '09:30' and p['exit_at'] == '09:35'
    assert p['win'] and p['ret_bps'] == round((p['exit'] / (100 * math.exp(0.01) * 1.0002) - 1) * 1e4 - 10, 1) or abs(p['ret_bps'] - 48) < 3


def test_short_trigger_then_stop_at_other_level():
    p = m.plan_walk(_bars([(0, 100, 100.1, 99.9, 100), (1, 100, 100, 98.9, 99.0), (2, 99.0, 101.5, 98.95, 101.2)]), EXC)
    assert p['status'] == 'STOP' and p['side'] == 'SHORT' and p['exit'] == round(100 * math.exp(0.01), 2) and not p['win'] and p['ret_bps'] < -150


def test_open_trade_shows_live_pnl_and_closes_at_1515_when_final():
    rows = [(0, 100, 100.1, 99.9, 100), (1, 100, 101.05, 100, 101.0)] + [(k, 101.0, 101.2, 100.9, 101.1) for k in range(2, 72)]
    live = m.plan_walk(_bars(rows[:10]), EXC)
    assert live['status'] == 'OPEN' and live['now_bps'] is not None
    done = m.plan_walk(_bars(rows), EXC, final=True)
    assert done['status'] == 'CLOSED' and done['exit_at'] == '15:15'


def test_no_trigger_by_noon_and_whipsaw_candle_skipped():
    quiet = [(k, 100, 100.3, 99.7, 100) for k in range(0, 40)]
    assert m.plan_walk(_bars(quiet), EXC)['status'] == 'NO_TRIGGER'
    assert m.plan_walk(_bars([(0, 100, 100.1, 99.9, 100), (1, 100, 101.5, 98.5, 100)]), EXC)['status'] == 'SKIPPED'
    assert m.plan_walk(_bars([(1, 100, 101, 99, 100)]), EXC)['status'] == 'WAITING'


class _Kite:
    def __init__(self, rows): self.rows = rows
    def historical_data(self, tok, a, b, interval):
        return [{'date': dt.datetime.combine(a.date(), dt.time(9, 15)) + dt.timedelta(minutes=5 * k), 'open': o, 'high': h, 'low': l, 'close': c, 'volume': 1}
                for k, o, h, l, c in self.rows]


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(m, 'ROOT', tmp_path); sent = []
    monkeypatch.setattr(m, '_telegram', lambda t: sent.append(t) or True)
    day = dt.date(2026, 10, 7)
    m._write_json(f'prep_{day}.json', {'pre': {'ABC': {'token': 7}}})
    return day, sent


def _day(conf):
    return {'date': '2026-10-07', 'confident': conf, 'picks': [{'symbol': 'ABC', 'normal_move_pct': 2.0}]}


def test_run_plan_live_cycle_and_telegram(store, monkeypatch):
    day, sent = store
    m._rewrite_jsonl('picks.jsonl', [_day(True)])
    rows = [(0, 100, 100.2, 99.8, 100), (1, 100, 100.5, 99.9, 100.4), (2, 100.4, 101.2, 100.3, 101.1), (3, 101.1, 102.0, 101.0, 101.9)]
    monkeypatch.setattr(m, 'now_ist', lambda: dt.datetime(2026, 10, 7, 9, 26))
    assert m.run_plan(_Kite(rows), day) is False                       # only the first two candles are complete
    p = m._read_jsonl('picks.jsonl')[0]['plan']
    assert p['status'] == 'WAITING' and p['buy_above'] and len(sent) == 1 and 'BUY above' in sent[0]
    monkeypatch.setattr(m, 'now_ist', lambda: dt.datetime(2026, 10, 7, 9, 41))
    assert m.run_plan(_Kite(rows), day) is True
    p = m._read_jsonl('picks.jsonl')[0]['plan']
    assert p['status'] == 'TARGET' and p['win'] and len(sent) == 2 and 'target' in sent[1]
    assert m.run_plan(_Kite(rows), day) is True and len(sent) == 2     # finished: nothing more
    sc = m.scorecard(m._read_jsonl('picks.jsonl'))['plan']
    assert sc['n'] == 1 and sc['win'] == 100 and sc['targets'] == 1 and not sc['drift']


def test_normal_confidence_day_is_watch_only(store):
    day, sent = store
    m._rewrite_jsonl('picks.jsonl', [_day(False)])
    assert m.run_plan(_Kite([]), day) is True
    assert m._read_jsonl('picks.jsonl')[0]['plan']['status'] == 'NO_PLAN' and sent == []


def test_page_shows_plan_and_no_straddle(monkeypatch):
    import base64
    from app import security, v132_movers
    from app.web import app
    monkeypatch.setenv('DBI_OWNER_PASSWORD', 't'); security.reset_security_state_for_tests()
    real = v132_movers.status()
    plan = dict(m.plan_levels(100.0, EXC), symbol='RBLBANK', status='TARGET', side='LONG', entry=101.03, entry_at='09:30', exit=101.64, exit_at='09:35',
                ret_bps=50.3, win=True, normal_move_pct=2.0, updated='2026-10-13T09:41:00')
    today = {'date': '2026-10-13', 'scored': 213, 'market_gap_pct': 0.0, 'confident': True, 'top_prob_pct': 81.6, 'threshold_pct': 67.1, 'picks': [],
             'news_ok': True, 'board_ok': True, 'plan': plan, 'straddle': {'s1': {'X': {'status': 'OPEN'}}}}
    fake = dict(real, today=today, score=dict(real.get('score') or {}, plan={'n': 3, 'win': 67, 'avg_bps': 21.0, 'targets': 2, 'stops': 1, 'no_trigger': 1, 'drift': False}))
    monkeypatch.setattr(v132_movers, 'status', lambda: fake)
    html = app.test_client().get('/admin/v130', headers={'Authorization': 'Basic ' + base64.b64encode(b'admin:t').decode()}).data.decode()
    assert 'Trade plan' in html and 'BUY if it trades above' in html and 'target hit' in html and '+50 bps after costs' in html and '3 trades' in html
    assert 'straddle' not in html.lower()
