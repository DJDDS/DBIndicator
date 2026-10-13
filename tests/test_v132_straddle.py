import datetime as dt
import pandas as pd
from app import v132_movers as m


def _chain():
    return {'expiry': '2026-10-27', 'lot': 500,
            'CE': [[990.0, 'X26OCT990CE', 1], [1000.0, 'X26OCT1000CE', 2], [1010.0, 'X26OCT1010CE', 3]],
            'PE': [[990.0, 'X26OCT990PE', 4], [1000.0, 'X26OCT1000PE', 5], [1010.0, 'X26OCT1010PE', 6]]}


def _q(bid, ask): return {'depth': {'buy': [{'price': bid}], 'sell': [{'price': ask}]}}


def test_straddle_uses_atm_strike_and_ask_prices():
    qs = {'X26OCT1000CE': _q(19.5, 20.5), 'X26OCT1000PE': _q(17.5, 18.5)}
    st = m.straddle_entry(_chain(), 1003.0, qs)
    assert st['status'] == 'OPEN' and st['strike'] == 1000.0 and st['entry'] == 39.0 and st['ce']['token'] == 2


def test_wide_or_missing_quotes_are_refused():
    assert m.straddle_entry(_chain(), 1000.0, {'X26OCT1000CE': _q(10, 20), 'X26OCT1000PE': _q(17.5, 18.5)})['status'] == 'NO_OPTION'
    assert m.straddle_entry(_chain(), 1000.0, {})['status'] == 'NO_OPTION'


def _bars(close):
    ts = pd.date_range('2026-10-13 09:15', periods=75, freq='5min')
    return pd.DataFrame({'date': ts, 'close': close})


def test_straddle_outcome_sells_at_bid_proxy():
    st = {'entry': 39.0, 'ce': {'spread_pct': 4.0}, 'pe': {'spread_pct': 4.0}}
    out = m.straddle_outcome(_bars([30.0] * 75), _bars([20.0] * 75), st)
    assert out['exit'] == 49.0 and out['win'] and out['ret_pct'] == round((49.0 / 39.0 - 1) * 100, 2)
    assert m.straddle_outcome(None, _bars([20.0] * 75), st) is None


def test_scorecard_splits_hit_and_miss():
    rows = [{'date': 'd1', 'picks': [{'symbol': 'A', 'hit': True}, {'symbol': 'B', 'hit': False}],
             'straddle': {'s1': {'A': {'ret_pct': 40.0}, 'B': {'ret_pct': -30.0}}}}]
    s = m.scorecard(rows)['straddle_s1']
    assert (s['n'], s['win'], s['avg_hit'], s['avg_miss']) == (2, 50, 40.0, -30.0)


def test_page_renders_straddle_table(monkeypatch):
    import base64
    from app import security, v132_movers
    from app.web import app
    monkeypatch.setenv("DBI_OWNER_PASSWORD", "t"); security.reset_security_state_for_tests()
    st = {'status': 'OPEN', 'strike': 400.0, 'entry': 21.5, 'entry_pct_of_spot': 5.4, 'ce': {'spread_pct': 2.1}, 'pe': {'spread_pct': 3.0}, 'ret_pct': 35.2, 'win': True}
    real = v132_movers.status()
    today = {'date': '2026-10-13', 'scored': 213, 'market_gap_pct': 0.0, 'confident': True, 'top_prob_pct': 81.6, 'threshold_pct': 67.1, 'picks': [],
             'news_ok': True, 'board_ok': True, 'straddle': {'s1': {'RBLBANK': st, 'XYZ': {'status': 'NO_OPTION', 'reason': 'CE spread 9%'}}}}
    fake = dict(real, today=today, score=dict(real.get('score') or {}, straddle_s1={'n': 3, 'win': 67, 'avg_pct': 12.3, 'avg_hit': 30.0, 'avg_miss': -20.0}, straddle_s2={}))
    monkeypatch.setattr(v132_movers, 'status', lambda: fake)
    owner = {"Authorization": "Basic " + base64.b64encode(b"admin:t").decode()}
    html = app.test_client().get('/admin/v130', headers=owner).data.decode()
    assert 'Paper ATM straddle' in html and '+35.2%' in html and 'CE spread 9%' in html and 'Straddle paper record' in html
