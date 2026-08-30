import datetime as dt
import math

from app import derivative_intelligence as di


def test_implied_volatility_recovers_black_scholes_input():
    t = 30 / 365
    price = di.black_scholes_price(100, 100, t, 0.06, 0.20, 'CE')
    iv = di.implied_volatility(price, 100, 100, t, 0.06, 'CE')
    assert iv is not None
    assert abs(iv - 0.20) < 1e-3


def test_option_expression_prefers_buy_when_iv_not_rich_and_liquidity_good():
    row = {
        'v8_state': 'TRADE CANDIDATE', 'v8_direction': 'Bullish',
        'v8_decision_score': 92, 'v8_participation': 90, 'realized_vol_20d': 34.0,
    }
    chain = {
        'atm_iv_pct': 30.0, 'straddle_move_pct': 3.2, 'dte': 12,
        'directional': {'symbol': 'ABCSEP100CE', 'type': 'CE', 'strike': 100,
                        'mid': 5.0, 'spread_pct': 1.2, 'iv_pct': 29.0,
                        'delta': 0.54, 'gamma': 0.06, 'theta_per_day': -0.10,
                        'vega': 0.09, 'volume': 5000, 'oi': 20000},
    }
    out = di.classify_option_expression(row, chain)
    assert out['option_action'] == 'OPTION BUYER EDGE'
    assert out['option_edge'] in ('HIGH', 'MEDIUM')
    assert out['buyer_score'] >= 55


def test_option_expression_flags_expensive_option_instead_of_forcing_call_buy():
    row = {
        'v8_state': 'TRADE CANDIDATE', 'v8_direction': 'Bullish',
        'v8_decision_score': 94, 'v8_participation': 92, 'realized_vol_20d': 22.0,
    }
    chain = {
        'atm_iv_pct': 55.0, 'straddle_move_pct': 7.0, 'dte': 6,
        'directional': {'symbol': 'ABCSEP100CE', 'type': 'CE', 'strike': 100,
                        'mid': 8.0, 'spread_pct': 1.0, 'iv_pct': 58.0,
                        'delta': 0.52, 'gamma': 0.08, 'theta_per_day': -0.35,
                        'vega': 0.06, 'volume': 9000, 'oi': 40000},
    }
    out = di.classify_option_expression(row, chain)
    assert out['option_action'] == 'UNDERLYING GOOD - OPTION EXPENSIVE'
    assert out['iv_rv_ratio'] > 2


def test_directional_contract_is_call_for_bull_and_put_for_bear():
    expiry = dt.date.today() + dt.timedelta(days=10)
    contracts = [
        {'tradingsymbol': 'ABCX100CE', 'strike': 100.0, 'instrument_type': 'CE', 'expiry': expiry},
        {'tradingsymbol': 'ABCX100PE', 'strike': 100.0, 'instrument_type': 'PE', 'expiry': expiry},
    ]
    quotes = {
        'NFO:ABCX100CE': {'last_price': 5.1, 'volume': 1000, 'oi': 5000, 'depth': {'buy':[{'price':5.0}], 'sell':[{'price':5.2}]}},
        'NFO:ABCX100PE': {'last_price': 4.9, 'volume': 1100, 'oi': 5200, 'depth': {'buy':[{'price':4.8}], 'sell':[{'price':5.0}]}},
    }
    bull = di.analyze_option_quotes('ABC', 'Bullish', 100.0, contracts, quotes, now=dt.datetime.now())
    bear = di.analyze_option_quotes('ABC', 'Bearish', 100.0, contracts, quotes, now=dt.datetime.now())
    assert bull['directional']['type'] == 'CE'
    assert bear['directional']['type'] == 'PE'


def test_shadow_signal_resolves_actual_option_premium_at_30m(tmp_path, monkeypatch):
    monkeypatch.setattr(di, 'SHADOW_STATE_FILE', str(tmp_path / 'shadow_state.json'))
    row = {
        'symbol':'ABC','timestamp':'2026-08-29T10:00:00','v8_direction':'Bullish','v8_state':'TRADE CANDIDATE',
        'close':100,'v8_decision_score':92,'v8_participation':90,
        'option_intelligence': {
            'option_action':'OPTION BUYER EDGE','option_edge':'HIGH',
            'contract': {'symbol':'ABCSEP100CE','mid':5.0,'iv_pct':30.0,'spread_pct':1.0},
        },
    }
    di.register_shadow_signal(row, now=dt.datetime(2026,8,29,10,0))
    class K:
        def quote(self, keys):
            return {'NFO:ABCSEP100CE': {'last_price':6.0,'depth':{'buy':[{'price':5.9}],'sell':[{'price':6.1}]}}}
    di.resolve_shadow_outcomes(K(), now=dt.datetime(2026,8,29,10,31))
    state = di.load_shadow_state()
    sig = state['signals'][0]
    assert sig['outcomes']['30m']['premium_return_pct'] == 20.0
    stats = di.get_shadow_stats()
    assert stats['30m']['count'] == 1
    assert stats['30m']['win_rate_pct'] == 100.0


def test_zero_depth_price_does_not_create_fake_midpoint():
    q = {'last_price':5.0,'depth':{'buy':[{'price':0.0}],'sell':[{'price':5.2}]}}
    mid, spread, bid, ask = di._mid_and_spread(q)
    assert mid == 5.0
    assert spread is None


def test_live_option_api_budget_is_split_across_bull_and_bear(monkeypatch):
    expiry = dt.date.today() + dt.timedelta(days=10)
    cmap = {}
    rows = []
    for side, prefix in [('Bullish','B'), ('Bearish','S')]:
        for i in range(4):
            sym = f'{prefix}{i}'
            rows.append({'symbol':sym,'v8_direction':side,'v8_state':'TRADE CANDIDATE','v8_decision_score':99-i,'v8_participation':90,'close':100,'realized_vol_20d':30})
            cmap[sym] = [
                {'tradingsymbol':f'{sym}100CE','strike':100.0,'instrument_type':'CE','expiry':expiry},
                {'tradingsymbol':f'{sym}100PE','strike':100.0,'instrument_type':'PE','expiry':expiry},
            ]
    monkeypatch.setattr(di, '_option_contracts_map', lambda kite: cmap)
    monkeypatch.setattr(di, 'record_shadow_snapshot', lambda row, now=None: None)
    class K:
        def quote(self, keys):
            out = {}
            for key in keys:
                out[key] = {'last_price':5.0,'volume':5000,'oi':10000,'depth':{'buy':[{'price':4.9}],'sell':[{'price':5.1}]}}
            return out
    di.enrich_shortlisted_options(K(), rows, max_candidates=6, now=dt.datetime.now())
    enriched_bull = [r for r in rows if r['v8_direction']=='Bullish' and r.get('option_intelligence')]
    enriched_bear = [r for r in rows if r['v8_direction']=='Bearish' and r.get('option_intelligence')]
    assert len(enriched_bull) == 3
    assert len(enriched_bear) == 3


def test_analyze_option_quotes_can_skip_too_near_expiry_for_swing():
    today = dt.date.today()
    e1 = today + dt.timedelta(days=1)
    e2 = today + dt.timedelta(days=10)
    contracts = []
    quotes = {}
    for expiry, tag in [(e1,'N'),(e2,'F')]:
        for typ in ('CE','PE'):
            ts = f'ABC{tag}100{typ}'
            contracts.append({'tradingsymbol':ts,'strike':100.0,'instrument_type':typ,'expiry':expiry})
            quotes[f'NFO:{ts}'] = {'last_price':5.0,'volume':1000,'oi':5000,'depth':{'buy':[{'price':4.9}],'sell':[{'price':5.1}]}}
    swing = di.analyze_option_quotes('ABC','Bullish',100.0,contracts,quotes,now=dt.datetime.now(),min_dte=3)
    assert swing['expiry'] == e2.isoformat()
    assert swing['dte'] == 10


def test_chain_reports_atm_pcr_and_skew_context_without_using_it_as_direction_gate():
    expiry = dt.date.today() + dt.timedelta(days=12)
    strikes = [95.0,100.0,105.0]
    contracts=[]; quotes={}
    for strike in strikes:
        for typ in ('CE','PE'):
            ts=f'ABC{int(strike)}{typ}'
            contracts.append({'tradingsymbol':ts,'strike':strike,'instrument_type':typ,'expiry':expiry})
            last = 6.0 if strike == 100 else 3.0
            quotes[f'NFO:{ts}']={'last_price':last,'volume':2000 if typ=='PE' else 1000,'oi':8000 if typ=='PE' else 4000,'depth':{'buy':[{'price':last-0.1}],'sell':[{'price':last+0.1}]}}
    out=di.analyze_option_quotes('ABC','Bullish',100.0,contracts,quotes,now=dt.datetime.now())
    assert out['atm_volume_pcr'] == 2.0
    assert out['atm_oi_pcr'] == 2.0
    assert 'put_skew_pct' in out
    assert 'call_skew_pct' in out


def test_enrichment_attaches_separate_intraday_and_swing_option_expression(monkeypatch):
    today = dt.date.today(); e1=today+dt.timedelta(days=1); e2=today+dt.timedelta(days=10)
    contracts=[]
    for expiry, tag in [(e1,'N'),(e2,'F')]:
        for typ in ('CE','PE'):
            contracts.append({'tradingsymbol':f'ABC{tag}100{typ}','strike':100.0,'instrument_type':typ,'expiry':expiry})
    monkeypatch.setattr(di, '_option_contracts_map', lambda kite: {'ABC':contracts})
    monkeypatch.setattr(di, 'record_shadow_snapshot', lambda row, now=None: None)
    class K:
        def quote(self, keys):
            return {k:{'last_price':5.0,'volume':5000,'oi':10000,'depth':{'buy':[{'price':4.9}],'sell':[{'price':5.1}]}} for k in keys}
    row={'symbol':'ABC','v8_direction':'Bullish','v8_state':'TRADE CANDIDATE','v8_swing_state':'TRADE CANDIDATE','v8_decision_score':92,'v8_swing_alpha':88,'v8_participation':90,'close':100,'realized_vol_20d':30}
    di.enrich_shortlisted_options(K(), [row], max_candidates=6, now=dt.datetime.now())
    assert row['option_dte'] == 1
    assert row['option_swing_dte'] == 10
    assert row['option_contract'].endswith('CE')
    assert row['option_swing_contract'].endswith('CE')


def test_shadow_30m_is_not_falsely_marked_from_next_day_quote(tmp_path, monkeypatch):
    monkeypatch.setattr(di, 'SHADOW_STATE_FILE', str(tmp_path / 'shadow_state.json'))
    row = {
        'symbol':'ABC','timestamp':'2026-08-29T15:20:00','v8_direction':'Bullish','v8_state':'TRADE CANDIDATE',
        'close':100,'v8_decision_score':92,'v8_participation':90,
        'option_intelligence': {'option_action':'OPTION BUYER EDGE','option_edge':'HIGH',
            'contract': {'symbol':'ABCSEP100CE','mid':5.0,'iv_pct':30.0,'spread_pct':1.0}},
    }
    di.register_shadow_signal(row, now=dt.datetime(2026,8,29,15,20))
    class K:
        def quote(self, keys):
            return {'NFO:ABCSEP100CE': {'last_price':8.0,'depth':{'buy':[{'price':7.9}],'sell':[{'price':8.1}]}}}
    di.resolve_shadow_outcomes(K(), now=dt.datetime(2026,8,30,15,21))
    sig = di.load_shadow_state()['signals'][0]
    assert '30m' not in sig['outcomes']
    assert '2h' not in sig['outcomes']
    assert '1D' in sig['outcomes']


def test_shadow_registers_intraday_and_swing_option_contracts_separately(tmp_path, monkeypatch):
    monkeypatch.setattr(di, 'SHADOW_STATE_FILE', str(tmp_path / 'shadow_state.json'))
    monkeypatch.setattr(di, 'SHADOW_FILE', str(tmp_path / 'shadow.jsonl'))
    row = {
        'symbol':'ABC','timestamp':'2026-08-29T10:00:00','v8_direction':'Bullish','v8_state':'TRADE CANDIDATE',
        'v8_swing_state':'TRADE CANDIDATE','v8_decision_score':92,'v8_swing_alpha':88,'close':100,'v8_participation':90,
        'option_intelligence': {'option_action':'OPTION BUYER EDGE','option_edge':'HIGH','contract':{'symbol':'ABCNEARCE','mid':5.0}},
        'option_swing_intelligence': {'option_action':'OPTION BUYER EDGE','option_edge':'HIGH','contract':{'symbol':'ABCFARCE','mid':7.0}},
    }
    di.record_shadow_snapshot(row, now=dt.datetime(2026,8,29,10,0))
    signals = di.load_shadow_state()['signals']
    assert {s['signal_kind'] for s in signals} == {'intraday','swing'}
    assert {s['contract'] for s in signals} == {'ABCNEARCE','ABCFARCE'}


def test_v9_failed_breakout_bear_uses_put_direction_in_option_analysis(monkeypatch):
    today = dt.date.today(); expiry = today + dt.timedelta(days=10)
    monkeypatch.setattr(di, '_option_contracts_map', lambda kite: {
        'ABC': [
            {'tradingsymbol':'ABC100CE','strike':100.0,'instrument_type':'CE','expiry':expiry},
            {'tradingsymbol':'ABC100PE','strike':100.0,'instrument_type':'PE','expiry':expiry},
        ]
    })
    seen = []
    def fake_analyze(symbol, direction, spot, contracts, quotes, **kwargs):
        seen.append(direction)
        return {'directional': None, 'dte': 10}
    monkeypatch.setattr(di, 'analyze_option_quotes', fake_analyze)
    monkeypatch.setattr(di, 'record_shadow_snapshot', lambda row, now=None: None)
    class K:
        def quote(self, keys):
            return {k:{'last_price':5.0} for k in keys}
    row = {
        'symbol':'ABC', 'failed_breakout_direction':'Bearish', 'v9_intraday_state':'TRADE CANDIDATE',
        'v9_intraday_score':88, 'close':100, 'realized_vol_20d':30,
    }
    di.enrich_shortlisted_options(K(), [row], max_candidates=6, now=dt.datetime.now())
    assert seen and all(direction == 'Bearish' for direction in seen)
