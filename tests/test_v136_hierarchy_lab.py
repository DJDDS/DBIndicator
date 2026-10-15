import datetime as dt

import numpy as np
import pandas as pd

from app import v135_hierarchy as h
from app import v136_hierarchy_lab as lab


def _bars(day, prices, vol=1000.0):
    """prices: dict bar index k -> close; other bars flat at the last close."""
    rows, last = [], prices.get(0, 100.0)
    for k in range(75):
        c = prices.get(k, last); last = c
        t = dt.datetime.combine(day, dt.time(9, 15)) + dt.timedelta(minutes=5 * k)
        rows.append({'date': t, 'open': c, 'high': c * 1.001, 'low': c * 0.999, 'close': c, 'volume': vol})
    return pd.DataFrame(rows)


def test_reduce_bars_maps_bars_to_the_live_poll_times():
    day = dt.date(2025, 3, 3)
    r = lab.reduce_bars(_bars(day, {0: 100, 2: 101, 3: 102, 4: 103, 5: 104, 8: 105, 11: 106, 17: 107, 71: 108}))
    row = r.iloc[0]
    assert (row.p930, row.p935, row.p940, row.p945) == (101, 102, 103, 104)
    assert (row.p15, row.p30, row.p60, row.pexit) == (105, 106, 107, 108) and row.v15 == 3000


def _frames(days, n_ind=6, per=20, strong='S1_3', side=1):
    rng = np.random.default_rng(0); frames, ind = {}, {}
    for g in range(n_ind):
        for k in range(per):
            sym = f'S{g}_{k}'; ind[sym] = f'IND{g}'; rows = []
            for d in days:
                drift = side * 0.001 * (1 if g == 1 else 0.3) + rng.normal(0, 1e-4)
                p0 = 100.0; lead = 3 if sym == strong else 1
                p = [p0, p0 * (1 + lead * drift / 3), p0 * (1 + 2 * lead * drift / 3), p0 * (1 + lead * drift)]
                fut = p[3] * (1 + side * (0.01 if sym == strong else 0.0) + rng.normal(0, 0.003))
                rows.append({'date': d, 'p930': p[0], 'p935': p[1], 'p940': p[2], 'p945': p[3], 'v15': 1000.0 * (3 if sym == strong else 1),
                             'p15': fut, 'p30': fut, 'p60': fut, 'pexit': fut, 'hi': max(fut, p[3]) * 1.001, 'lo': min(fut, p[3]) * 0.999})
            frames[sym] = pd.DataFrame(rows)
    return frames, ind


def test_panel_replays_the_live_rule_and_finds_the_up_leader():
    days = [dt.date(2023, 1, 2) + dt.timedelta(days=i) for i in range(40)]
    frames, ind = _frames(days)
    P = lab.build_panel(frames, ind, h.load_settings())
    assert len(P) and set(P.date) <= set(days[10:])                      # needs 10 earlier days of volume, like live
    top = P.sort_values('score').groupby('date').tail(1)
    assert (top.symbol == 'S1_3').all() and (top.side == 1).all() and (top.r_exit.mean() > 0.5)


def test_evaluate_marks_a_side_active_only_on_out_of_sample_evidence():
    days = [d for d in (dt.date(2022, 6, 1) + dt.timedelta(days=i) for i in range(900)) if d.weekday() < 5]
    frames, ind = _frames(days)
    P = lab.build_panel(frames, ind, h.load_settings())
    res = lab.evaluate(P, 12.0, test_from=dt.date(2023, 9, 1))
    assert res['UP']['verdict'] == 'ACTIVE' and res['UP']['best']['test']['net_bps'] > 0
    assert res['DOWN']['verdict'] == 'SHADOW' and res['DOWN']['why']


def test_start_is_refused_in_market_hours():
    r = lab.start(None, now=dt.datetime(2026, 10, 15, 11, 0))
    assert r['started'] is False and 'market hours' in r['reason']
