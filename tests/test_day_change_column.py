import numpy as np
import pandas as pd

from app import indicators as ind
from app.oi_view import _OI_NUMERIC_FIELDS


def _df():
    idx = (pd.date_range('2026-10-09 09:15', periods=25, freq='15min').append(pd.date_range('2026-10-12 09:15', periods=25, freq='15min'))
           .append(pd.date_range('2026-10-13 09:15', periods=8, freq='15min')))
    c = np.concatenate([np.linspace(410, 418, 25), np.linspace(418, 418, 25), np.linspace(396, 397, 8)])
    return pd.DataFrame({'open': c, 'high': c + 1, 'low': c - 1, 'close': c, 'volume': 1000.0}, index=idx)


def test_day_change_is_against_the_previous_sessions_close_not_the_previous_candle():
    s = ind.compute_signal(_df(), '15minute')
    assert s['prev_day_close'] == 418.0 and s['day_chg_pct'] == -5.02          # gapped down on results: the day is -5%, not "+0.9% since the first scan"
    assert abs(s['close'] - s['prev_close']) < 1                                # the old prev_close is just the previous 15-minute candle


def test_oi_screener_carries_and_shows_day_change():
    assert 'day_chg_pct' in _OI_NUMERIC_FIELDS
    html = open('app/templates/oi_screener.html', encoding='utf-8').read()
    assert 'Day Chg %' in html and 'Since-open Chg %' in html and 'r.day_chg_pct' in html and html.count('colspan="14"') == 2
    cols = [int(x) for x in __import__('re').findall(r'<th data-col="(\d+)"', html)]
    assert cols == list(range(14))
