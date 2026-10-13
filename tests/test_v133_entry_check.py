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
