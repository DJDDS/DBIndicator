import numpy as np, pandas as pd
from app import v133_direction as v


def _series(n=120, seed=3):
    rng = np.random.default_rng(seed); c = 100 * np.exp(np.cumsum(rng.normal(0.0003, 0.012, n)))
    o = c * (1 + rng.normal(0, 0.004, n)); h = np.maximum(o, c) * (1 + np.abs(rng.normal(0, 0.006, n))); l = np.minimum(o, c) * (1 - np.abs(rng.normal(0, 0.006, n)))
    return o, h, l, c


def test_indicators_match_research_formulas():
    o, h, l, c = _series(); r = v._entry_indicators(o, h, l, c)
    s = pd.Series(c); d = s.diff(); up = d.clip(lower=0).ewm(alpha=1 / 14, adjust=False).mean(); dn = (-d.clip(upper=0)).ewm(alpha=1 / 14, adjust=False).mean()
    assert abs(r['ec_rsi'] - (100 - 100 / (1 + up / dn)).iloc[-1]) < 1e-9
    hi, lo = pd.Series(h).rolling(20).max().iloc[-1], pd.Series(l).rolling(20).min().iloc[-1]
    assert abs(r['ec_stoch'] - (c[-1] - lo) / (hi - lo)) < 1e-9
    assert abs(r['ec_bbz'] - ((s - s.rolling(20).mean()) / s.rolling(20).std()).iloc[-1]) < 1e-9
    assert abs(r['ec_ema50'] - s.ewm(span=50, adjust=False).mean().iloc[-1]) < 1e-9
    pcv = s.shift(); tr = np.maximum(pd.Series(h) - pd.Series(l), np.maximum((pd.Series(h) - pcv).abs(), (pd.Series(l) - pcv).abs()))
    assert abs(r['ec_atr14'] - tr.rolling(14).mean().iloc[-1]) < 1e-9
    assert all(x is None for x in v._entry_indicators(o[:30], h[:30], l[:30], c[:30]).values())


def test_entry_check_labels_and_scope():
    s = v.load_settings(); hp = {'pc': 100.0, 'ec_rsi': 60.0, 'ec_stoch': 0.6, 'ec_bbz': 1.0, 'ec_ema50': 98.0, 'ec_atr14': 2.0}
    good = v.entry_check(hp, 101.0, 'DOWN', s); assert good['label'] == 'GOOD' and good['score'] == 4
    weak = v.entry_check({**hp, 'ec_rsi': 85.0, 'ec_stoch': 0.99, 'ec_bbz': 3.0}, 101.0, 'DOWN', s); assert weak['label'] == 'WEAK' and weak['score'] == 1
    border = v.entry_check({**hp, 'ec_rsi': 85.0, 'ec_stoch': 0.99}, 101.0, 'DOWN', s); assert border['label'] == 'BORDERLINE'
    assert v.entry_check(hp, 101.0, 'UP', s)['label'] == 'n/a' and v.entry_check(None, 101.0, 'DOWN', s)['label'] == 'n/a'
    assert v.entry_check(hp, 101.0, 'DOWN', {**s, 'entry_check': False})['label'] == 'off'


def test_settings_are_adjustable_and_sanitised(tmp_path, monkeypatch):
    s = v.load_settings(); assert s['ec_min_score'] == 3 and s['ec_rsi'] == [46.6, 71.3]
    monkeypatch.setattr(v, '_read_json', lambda n, d=None: {'ec_rsi': [80, 40], 'ec_min_score': 9, 'ec_stoch': 'bad'} if n == 'settings.json' else d)
    s = v.load_settings(); assert s['ec_rsi'] == [40.0, 80.0] and s['ec_min_score'] == 4 and s['ec_stoch'] == [0.385, 0.943]


def test_paper_hold_plus_stop_result():
    import datetime as dt
    idx = pd.date_range('2026-10-13 09:15', periods=75, freq='5min')
    base = 100.0
    def bars(highs):
        return pd.DataFrame({'date': idx, 'open': base, 'high': highs, 'low': base - 0.2, 'close': base})
    h_ok = [base + 0.1] * 75; h_stop = [base + 0.1] * 10 + [base + 1.6] + [base + 0.1] * 64
    ok = v.outcome_from_bars(bars(h_ok), entry=base, p925=base, side='DOWN', k_exit=74, stop_pct=1.0)
    st = v.outcome_from_bars(bars(h_stop), entry=base, p925=base, side='DOWN', k_exit=74, stop_pct=1.0)
    assert ok['stopped'] is False and ok['stop_ret_pct'] == ok['ret_pct']
    assert st['stopped'] is True and st['stop_ret_pct'] < -1.0 and st['stop_win'] is False
    assert 'stopped' not in v.outcome_from_bars(bars(h_ok), entry=base, p925=base, side='DOWN', k_exit=74)
    assert v.load_settings()['stop_k'] == 1.0


def test_market_leg_and_vs_nifty_result():
    import pandas as pd
    from app import v133_direction as d
    ts = pd.date_range('2026-10-13 09:15', periods=75, freq='5min')
    nifty = pd.DataFrame({'date': ts, 'open': 100.0, 'high': 101.0, 'low': 99.0, 'close': [100.0 + 0.02 * i for i in range(75)], 'volume': 1})
    mk = d.market_pct_from_bars(nifty, 74)
    assert mk == round(__import__('math').log(nifty.close[74] / nifty.close[1]) * 100, 3) and mk > 1.4
    assert d.market_pct_from_bars(None, 74) is None


def test_trailing_stop_locks_in_profit_and_never_loosens():
    idx = pd.date_range('2026-10-13 09:15', periods=75, freq='5min'); base = 100.0
    lows = [base - 0.2] * 10 + [base - 3.0] + [base - 0.2] * 64                      # a deep dip (short is in profit by ~3%)
    highs = [base + 0.1] * 12 + [base - 1.0] * 3 + [base - 1.0] * 60                  # then price bounces back up to 99.0 (above the trail)
    highs[13] = base - 1.0; highs = [min(x, base + 0.1) for x in highs]
    df = pd.DataFrame({'date': idx, 'open': base, 'high': highs, 'low': lows, 'close': base - 2.0})
    df.loc[20:, 'high'] = base - 1.0; df.loc[30, 'high'] = base - 0.5               # bounce through the trailing stop (best 97.0 + 0.75%)
    plain = v.outcome_from_bars(df, entry=base, p925=base, side='DOWN', k_exit=74, stop_pct=1.0)
    tr = v.outcome_from_bars(df, entry=base, p925=base, side='DOWN', k_exit=74, stop_pct=1.0, trail_pct=0.75)
    assert plain['stopped'] is False                                               # fixed 1% stop is never touched
    assert tr['stopped'] is True and tr['trail_pct'] == 0.75 and tr['stop_ret_pct'] > 1.5 and tr['stop_win'] is True   # the trail banked most of the dip
    up = v.outcome_from_bars(df.assign(open=base, high=base + 3.0, low=base - 0.1, close=base), entry=base, p925=base, side='UP', k_exit=74, stop_pct=1.0, trail_pct=0.75)
    assert up['stopped'] is False or up['stop_ret_pct'] > -2.0


def test_pool_can_be_limited_to_down_picks_and_settings_validate(monkeypatch):
    X = pd.DataFrame({'symbol': list('ABCDEF'), 'o': 1.0}); p = np.array([0.95, 0.9, 0.2, 0.3, 0.4, 0.6])
    both = v.select(X, p, [0.5] * 300, {'strong_quantile': 0.7, 'window': 250, 'top_n': 3})
    down = v.select(X, p, [0.5] * 300, {'strong_quantile': 0.7, 'window': 250, 'top_n': 3, 'only_side': 'DOWN'})
    assert set(both['list'].side) == {'UP', 'DOWN'} and list(down['list'].symbol) == ['C', 'D', 'E'] and set(down['list'].side) == {'DOWN'}
    assert down['view'] == both['view']                                           # the day view still uses every stock
    s = v.load_settings(); assert s['hc_down_only'] is False and s['trail_k'] == 0.75   # pool takes both sides (88-session replay, Oct 2026)
    monkeypatch.setattr(v, '_read_json', lambda n, d=None: {'trail_k': 99, 'hc_down_only': 1} if n == 'settings.json' else d)
    s = v.load_settings(); assert s['trail_k'] == 5.0 and s['hc_down_only'] is True                 # a saved DOWN-only choice still wins


def test_pick_of_the_day_grade_and_minimum_trades():
    s = v.load_settings(); assert s['grade_a_conf'] == 0.14 and s['hc_min_trades'] == 1
    mk = lambda c, sc: {'conf': c, 'entry_check': {'score': sc}, 'status': 'OPEN'}
    picks = [mk(0.10, 4), mk(0.09, 4), mk(0.08, 2)]; v.grade_picks(picks, s)
    assert [p['pod'] for p in picks] == [True, False, False] and [p['grade'] for p in picks] == ['B', 'B', 'B']
    assert [p['status'] for p in picks] == ['OPEN', 'WATCH', 'WATCH']              # a quiet day still gets its one trade
    picks = [mk(0.2, 4), mk(0.2, 3), mk(0.2, 2)]; v.grade_picks(picks, s)
    assert [p['grade'] for p in picks] == ['A', 'A', 'B'] and [p['status'] for p in picks] == ['OPEN', 'OPEN', 'WATCH']
    picks = [mk(0.2, 4), mk(0.2, 3), mk(0.2, 2)]; v.grade_picks(picks, {**s, 'hc_min_trades': 3}); assert all(p['status'] == 'OPEN' for p in picks)
