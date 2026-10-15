import datetime as dt

import pandas as pd

from app import v133_direction as d
from app import v138_pool_lab as lab

DAY = '2026-10-14'


def _bars(p925, path):
    """path: candle index -> close; high/low +-0.2% around it; candle 1 closes at p925."""
    out, last = [], p925
    for k in range(75):
        c = path.get(k, last if k > 1 else p925); last = c
        out.append([k, c, c * 1.002, c * 0.998, c, 1000])
    return out


def _X():
    rows = []
    for i, (sym, p) in enumerate([('D1', 0.30), ('D2', 0.32), ('D3', 0.35), ('U1', 0.72), ('U2', 0.69), ('U3', 0.60), ('N1', 0.51)]):
        rows.append({'symbol': sym, 'p925': 100.0, **{f'p_{h}': p for h in d.HORIZONS}})
    X = pd.DataFrame(rows); X['p'] = X[[f'p_{h}' for h in d.HORIZONS]].mean(axis=1)
    X['conf'] = (X.p - 0.5).abs(); X['side'] = X.p.gt(0.5).map({True: 'UP', False: 'DOWN'})
    return X


def test_outcome_hold_and_book():
    b = _bars(100, {10: 101.5, 25: 101.2, 71: 99.5})
    up = lab.outcome(b, 100, 'UP', lab.K_1125)
    assert abs(up['hold'] - (100 * 0.011929 - 0.06)) < 0.01 and up['book'] == 0.94
    up15 = lab.outcome(b, 100, 'UP', lab.K_1515)
    assert up15['hold'] < 0 and up15['book'] == 0.94                    # booked at +1% before fading
    dn = lab.outcome(b, 100, 'DOWN', lab.K_1515)
    assert dn['hold'] > 0 and dn['book'] == dn['hold']                  # never 1% in its favour


def test_rules_pick_the_right_side_and_exit():
    X = _X()
    a = lab.day_picks(X, lab.VARIANTS['A. DOWN only, 15:15 (current)'], False, 'DOWN')
    assert [p[0] for p in a] == ['D1', 'D2', 'D3'] and all(p[3] == lab.K_1515 for p in a)
    c = lab.day_picks(X, lab.VARIANTS['C. both sides, UP exits 11:25'], False, 'DOWN')
    assert [p[0] for p in c] == ['U1', 'D1', 'U2'] and c[0][3] == lab.K_1125 and c[1][3] == lab.K_1515   # by confidence .22 .20 .19
    f = lab.VARIANTS['F. DOWN 15:15; on ★ UP-lean days UP 11:25']
    assert [p[0] for p in lab.day_picks(X, f, True, 'UP')] == ['U1', 'U2', 'U3']
    assert [p[0] for p in lab.day_picks(X, f, False, 'UP')] == ['D1', 'D2', 'D3']
    X.loc[X.symbol == 'U2', 'conf'] = 0.13                          # below the 0.14 bar
    g = lab.day_picks(X, lab.VARIANTS['G. both sides, UP 11:25, conf >= 0.14'], False, None)
    assert [p[0] for p in g] == ['U1', 'D1', 'D2']


def test_refresh_replays_recorded_days_once(tmp_path, monkeypatch):
    monkeypatch.setattr(d, 'ROOT', tmp_path)
    X = _X().drop(columns=['p', 'conf', 'side'])
    d._write_gz(f'scores_{DAY}.json.gz', X.to_dict('split'))
    bars = {s: _bars(100, {25: 101.0 if s.startswith('U') else 99.0, 71: 100.5 if s.startswith('U') else 98.5}) for s in X.symbol}
    d._write_gz(f'bars_{DAY}.json.gz', bars)
    d._rewrite_jsonl('days.jsonl', [{'date': DAY, 'lists': {'hc': {'strong': True, 'lean': 'UP'}}}])
    out = lab.refresh()
    s = {r['variant']: r for r in out['summary']}
    assert s['A. DOWN only, 15:15 (current)']['picks'] == 3 and s['A. DOWN only, 15:15 (current)']['hold']['win'] == 100.0
    assert s['C. both sides, UP exits 11:25']['up_n'] == 2 and s['F. DOWN 15:15; on ★ UP-lean days UP 11:25']['up_n'] == 3
    assert s['E. UP only, 15:15']['hold']['avg_bps'] < s['D. UP only, 11:25']['hold']['avg_bps']
    st = lab.status()
    assert 'per_day' not in st and st['first'] == DAY and st['summary'][0]['days'] == 1
