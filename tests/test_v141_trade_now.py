"""V14.1 Trade-now panel + hierarchy volume-baseline keeper."""
import datetime as dt

import pandas as pd

from app import v141_trade_now as tn


def _direction(ec_label='WEAK', trade=False, conf=0.104, grade='B'):
    pick = {'symbol': 'ICICIPRULI', 'side': 'DOWN', 'conf': conf, 'grade': grade, 'pod': True, 'status': 'OPEN', 'entry': 438.8,
            'entry_check': {'label': ec_label, 'score': 0 if ec_label == 'WEAK' else 3, 'of': 4}}
    item = {'symbol': 'ICICIPRULI', 'side': 'DOWN', 'entry': 438.8, 'added': '2026-10-16T09:26:59', 'source': '09:25 pool', 'trade': trade,
            'track': {'now_pct': -0.44, 'stop_px': 445.72, 'n_obs': 10, 'best_pct': 0.41, 'worst_pct': -0.6, 'path': [], 'mkt_pct': 0.41, 'booked': False, 'stopped': False, 'closed': False, 'guarded': False, 'at': '10:10'}}
    return {'today': {'date': '2026-10-16', 'settings': {'grade_a_conf': 0.14, 'ec_min_score': 3, 'hc_book_pct': 1.0},
                      'lists': {'hc': {'picks': [pick]}}},
            'watch': {'today': {'items': [item]}, 'settings': {'book_pct': 1.0, 'exit_at': '15:15'}},
            'v47': {'today': {'picks': [], 'rejected': {'NO_VOLUME_BASELINE': 199, 'NO_INDUSTRY': 8, 'GROUP_TOO_SMALL': 6},
                              'baseline_days_have': 4, 'baseline_days_min': 10}}}


def test_weak_entry_check_explained_and_no_trade():
    out = tn.build(_direction(), {}, {})
    assert out['take'] == [] and out['skip'][0]['symbol'] == 'ICICIPRULI' and 'WEAK 0/4' in out['skip'][0]['why']
    assert 'no A grade today' in out['grade_rule'] and '0.104' in out['grade_rule']
    assert any('4 of 10 days' in n and 'volume history still filling' in n for n in out['notes'])


def test_traded_pool_pick_gets_levels():
    out = tn.build(_direction('GOOD', True, 0.16, 'A'), {}, {})
    r = out['take'][0]
    assert r['action'] == 'SELL' and r['stop'] == 445.72 and r['target'] == round(438.8 * 0.99, 2) and r['grade'] == 'A' and out['live'] == 1


def test_movers_bracket_and_acd_rows():
    mv = {'today': {'confident': True, 'picks': [{'symbol': 'DMART'}], 'plan': {'symbol': 'DMART', 'status': 'WAITING', 'buy_above': 101, 'sell_below': 99,
                                                                                  'long_target': 101.6, 'short_target': 98.4, 'long_stop': 99, 'short_stop': 101}}}
    acd = {'week_used': 0, 'week_cap': 2, 'today': {'levels': {'pr_bot': 22846.9}, 'or': {'a_down': 22864.86}, 'last': {'close': 23075.7},
                                                     'instruments': {'fut_symbol': 'NIFTY26OCTFUT'}}}
    out = tn.build({}, mv, acd)
    assert out['take'][0]['bracket'] and out['take'][0]['buy_above'] == 101
    assert any('22864.86' in n and 'NIFTY26OCTFUT' in n for n in out['notes'])
    acd['today']['trade'] = {'entry': 22850.0, 'entry_time': '11:02', 'stop_level': 22890.0, 'live_points': 12.5}
    acd['today']['signal'] = {'side': 'SHORT', 'bar': '11:01'}
    r = tn.build({}, {}, acd)['take'][0]
    assert r['desk'] == 'NIFTY ACD' and r['action'] == 'SELL' and r['stop'] == 22890.0 and r['exit'] == '15:20' and '+12.5' in r['state']


def test_one_broken_desk_does_not_hide_others():
    out = tn.build({'today': 'broken'}, {}, {})
    assert any('unavailable' in n for n in out['notes'])


def test_page_shows_trade_now(monkeypatch):
    import base64
    from app import security, v132_movers, v133_direction
    from app.web import app
    monkeypatch.setenv('DBI_OWNER_PASSWORD', 't'); security.reset_security_state_for_tests()
    real = v133_direction.status()
    monkeypatch.setattr(v133_direction, 'status', lambda: dict(real, **_direction()))
    monkeypatch.setattr(v132_movers, 'status', lambda: {})
    html = app.test_client().get('/admin/intraday', headers={'Authorization': 'Basic ' + base64.b64encode(b'admin:t').decode()}).data.decode()
    assert 'id="tradenow"' in html and 'No trade right now' in html and 'WEAK 0/4' in html


class _Kite:
    def instruments(self, ex):
        if ex == 'NFO': return [{'name': 'AAA', 'instrument_type': 'FUT', 'segment': 'NFO-FUT'}]
        return [{'tradingsymbol': 'AAA', 'instrument_token': 1, 'segment': 'NSE'}]

    def historical_data(self, tok, a, b, interval):
        out = []
        for i, day in enumerate(pd.bdate_range(a.date(), b.date())):
            for k in range(0, 10):
                out.append({'date': dt.datetime.combine(day.date(), dt.time(9, 15)) + dt.timedelta(minutes=5 * k), 'open': 100, 'high': 101, 'low': 99,
                            'close': 100, 'volume': 1000 + i})
        return out


def test_baseline_keeper_seeds_from_kite(tmp_path, monkeypatch):
    from app import v133_direction as d, v136_hierarchy_lab as lab
    monkeypatch.setattr(d, 'ROOT', tmp_path, raising=False)
    monkeypatch.setattr(d, '_path', lambda name: tmp_path / name)
    monkeypatch.setattr(lab.time, 'sleep', lambda s: None)
    assert lab.baseline_short('2026-10-16')[0] == 0
    assert lab.seed_recent_baseline(_Kite(), today=dt.date(2026, 10, 16)) == 1
    have, need = lab.baseline_short('2026-10-16')
    assert have >= need


def test_finished_trade_moves_to_done_and_baseline_status():
    mv = {'today': {'confident': True, 'picks': [{'symbol': 'TECHM'}], 'plan': {'symbol': 'TECHM', 'status': 'STOP', 'side': 'SHORT', 'entry': 1700.38, 'entry_at': '09:25',
          'stop': 1737.68, 'target': 1689.45, 'ret_bps': -229.0, 'buy_above': 1737.68, 'sell_below': 1700.72, 'long_target': 1, 'short_target': 1, 'long_stop': 1, 'short_stop': 1}}}
    d = _direction(); d['v47']['baseline'] = {'days': 20, 'need': 10, 'last_fill': {'at': '2026-10-16 15:44:10'}}
    out = tn.build(d, mv, {})
    assert out['take'] == [] and out['done'][0]['symbol'] == 'TECHM' and 'stopped -229 bps' in out['done'][0]['state']
    assert any('now complete (20 days, filled 15:44)' in n for n in out['notes'])
