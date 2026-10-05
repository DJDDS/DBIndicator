"""Observational sector arithmetic. No trading decisions or external I/O."""
from __future__ import annotations
import math
import pandas as pd

PERIODS = ('today', '1W', '1M', '3M', '6M', '52W', 'QTD', 'Q1', 'Q2', 'Q3', 'Q4')
MA_PERIODS = (20, 50, 100, 200)


def finite(value):
    try:
        x = float(value)
        return x if math.isfinite(x) else None
    except (TypeError, ValueError):
        return None


def change(value, base):
    value, base = finite(value), finite(base)
    return (value / base - 1) * 100 if value is not None and base and base > 0 else None


def frame(candles):
    if not candles:
        return pd.DataFrame(columns=['open', 'high', 'low', 'close', 'volume'], index=pd.DatetimeIndex([]))
    df = pd.DataFrame(candles)
    # Kite dates are IST aware; normalize to an IST-naive timeline.
    dates = pd.to_datetime(df['date'], utc=True, errors='coerce').dt.tz_convert('Asia/Kolkata').dt.tz_localize(None)
    df.index = dates
    for c in ('open', 'high', 'low', 'close', 'volume'):
        df[c] = pd.to_numeric(df[c], errors='coerce') if c in df else 0
    df = df[~df.index.isna()].sort_index()
    return df[~df.index.duplicated(keep='last')].dropna(subset=['close'])


def summarize(candles, now, quote=None):
    now = pd.Timestamp(now)
    if now.tzinfo:
        now = now.tz_convert('Asia/Kolkata').tz_localize(None)
    df = frame(candles)
    # Intraday daily bars never contaminate the completed-history averages.
    cutoff = now.normalize() if now.hour >= 16 else now.normalize() - pd.Timedelta(days=1)
    df = df[df.index.normalize() <= cutoff]
    close = df.close
    historical_price = finite(close.iloc[-1]) if len(close) else None
    quote = quote or {}
    price = finite(quote.get('last_price')) or historical_price
    returns = dict.fromkeys(PERIODS)
    sma = {str(n): finite(close.rolling(n).mean().iloc[-1]) if len(close) >= n else None for n in MA_PERIODS}
    ema = {str(n): finite(close.ewm(span=n, adjust=False, min_periods=n).mean().iloc[-1]) if len(close) >= n else None for n in MA_PERIODS}

    def at(boundary):
        values = close[close.index.normalize() <= pd.Timestamp(boundary).normalize()]
        return finite(values.iloc[-1]) if len(values) and (pd.Timestamp(boundary).normalize()-values.index[-1].normalize()).days <= 10 else None

    returns['today'] = change(quote.get('last_price'), (quote.get('ohlc') or {}).get('close'))
    anchor = now.normalize() if quote.get('last_price') else (df.index[-1].normalize() if len(df) else now.normalize())
    for p, offset in [('1W', pd.DateOffset(weeks=1)), ('1M', pd.DateOffset(months=1)), ('3M', pd.DateOffset(months=3)), ('6M', pd.DateOffset(months=6)), ('52W', pd.DateOffset(weeks=52))]:
        returns[p] = change(price, at(anchor - offset))
    current_q = now.to_period('Q')
    returns['QTD'] = change(price, at(current_q.start_time - pd.Timedelta(days=1)))
    labels = {}
    for i in range(1, 5):
        q = current_q - i
        labels[f'Q{i}'] = f'{q.year} Q{q.quarter}'
        returns[f'Q{i}'] = change(at(q.end_time), at(q.start_time - pd.Timedelta(days=1)))
    year = df[df.index >= anchor - pd.Timedelta(weeks=52)]
    hi = finite(year.high.max()) if len(year) else None
    lo = finite(year.low.min()) if len(year) else None
    prev = close.shift(1)
    tr = pd.concat([df.high - df.low, (df.high - prev).abs(), (df.low - prev).abs()], axis=1).max(axis=1)
    atr = finite(tr.rolling(14).mean().iloc[-1]) if len(df) >= 14 else None
    discontinuity = bool((close.pct_change().abs().tail(260) > .30).any())
    recent = df.tail(20)
    previous = df.tail(6).head(5) if len(df) and df.index[-1].date() == now.date() else df.tail(5)
    levels = {'previous_high': finite(previous.high.iloc[-1]) if len(previous) else None,
              'previous_low': finite(previous.low.iloc[-1]) if len(previous) else None,
              'week_high': finite(previous.high.max()) if len(previous) else None,
              'week_low': finite(previous.low.min()) if len(previous) else None,
              'swing_high': finite(recent.high.max()) if len(recent) else None,
              'swing_low': finite(recent.low.min()) if len(recent) else None}
    return {'price': price, 'returns': returns, 'returns_asof': anchor.date().isoformat(), 'quarter_labels': labels, 'sma': sma, 'ema': ema,
            'sma_distance': {k: change(price, v) for k, v in sma.items()},
            'ema_distance': {k: change(price, v) for k, v in ema.items()},
            'above_sma': sum(price > v for v in sma.values() if price is not None and v is not None),
            'above_ema': sum(price > v for v in ema.values() if price is not None and v is not None),
            'ma_coverage': sum(v is not None for v in sma.values()), 'atr14': atr,
            'extension_atr': (price - sma['20']) / atr if price is not None and sma['20'] is not None and atr else None,
            'high52': hi, 'low52': lo, 'below_high52': change(price, hi), 'above_low52': change(price, lo),
            'position52': (price-lo)/(hi-lo)*100 if price is not None and hi is not None and lo is not None and hi > lo else None,
            'levels': levels, 'volume_ratio': None,
            'history_date': df.index[-1].date().isoformat() if len(df) else None, 'bars': len(df),
            'quality': {'adjustment': 'Kite vendor OHLC; independent split/bonus adjustment not verified', 'discontinuity': discontinuity}}


def relative_returns(row, benchmark):
    a, b = row.get('returns', {}), benchmark.get('returns', {})
    same_date = not row.get('returns_asof') or not benchmark.get('returns_asof') or row['returns_asof'] == benchmark['returns_asof']
    return {p: a[p]-b[p] if (same_date or p in ('Q1','Q2','Q3','Q4')) and finite(a.get(p)) is not None and finite(b.get(p)) is not None else None for p in PERIODS}


def rank_members(rows, period, relative=False):
    key = 'vs_sector' if relative else 'returns'
    valid = [r for r in rows if finite(r.get(key, {}).get(period)) is not None]
    return {'top': sorted(valid, key=lambda r: (-r[key][period], r['symbol']))[:2],
            'bottom': sorted(valid, key=lambda r: (r[key][period], r['symbol']))[:2], 'covered': len(valid)}


def vwap_series(candles):
    df = frame(candles)
    out, session, pv, volume = [], None, 0., 0.
    for date, r in df.iterrows():
        if session != date.date():
            session, pv, volume = date.date(), 0., 0.
        v = finite(r.volume) or 0
        if v > 0:
            pv += float((r.high+r.low+r.close)/3) * v
            volume += v
        out.append({'time': date.tz_localize('Asia/Kolkata').isoformat(), 'value': pv/volume if volume else None})
    return out


def chart_series(candles):
    df = frame(candles)
    result = {'candles': [], 'sma': {}, 'ema': {}}
    for date, r in df.iterrows():
        if all(finite(r[k]) is not None for k in ('open', 'high', 'low', 'close')):
            result['candles'].append({'time': date.date().isoformat(), **{k: float(r[k]) for k in ('open','high','low','close','volume')}})
    for n in MA_PERIODS:
        for key, values in [('sma', df.close.rolling(n).mean()), ('ema', df.close.ewm(span=n, adjust=False, min_periods=n).mean())]:
            result[key][str(n)] = [{'time': d.date().isoformat(), 'value': float(v)} for d,v in values.items() if finite(v) is not None]
    return result


def breadth_series(histories):
    """Current-membership historical breadth, with a per-day denominator."""
    frames = [frame(b) for b in histories if b]
    if not frames:
        return []
    panels = {}
    for n in MA_PERIODS:
        columns = []
        for df in frames:
            ma = df.close.rolling(n).mean()
            columns.append((df.close > ma).astype(float).where(ma.notna()))
        panels[str(n)] = pd.concat(columns, axis=1)
    dates = sorted(set().union(*(set(p.index) for p in panels.values())))
    out = []
    for date in dates[-500:]:
        item = {'time': date.date().isoformat()}
        for n,p in panels.items():
            vals = p.loc[date].dropna() if date in p.index else pd.Series(dtype=float)
            item[n] = float(vals.mean()*100) if len(vals) else None
            item[f'coverage_{n}'] = len(vals)
        out.append(item)
    return out


def rotation_series(sector_candles, benchmark_candles, window=20):
    a, b = frame(sector_candles), frame(benchmark_candles)
    if a.empty or b.empty:
        return []
    pair = pd.concat([a.close.rename('sector'), b.close.rename('benchmark')], axis=1).dropna()
    ratio = pair.sector / pair.benchmark
    rs = ratio.pct_change(window, fill_method=None)*100
    momentum = rs - rs.shift(window)
    return [{'time': d.date().isoformat(), 'x': float(rs.loc[d]), 'y': float(momentum.loc[d])}
            for d in pair.index[-6:] if finite(rs.loc[d]) is not None and finite(momentum.loc[d]) is not None]


def advance_decline(members):
    """Fresh today's price changes only; missing is never an unchanged stock."""
    values=[finite(r.get('returns',{}).get('today')) for r in members]
    covered=[v for v in values if v is not None]
    advances=sum(v>0 for v in covered)
    declines=sum(v<0 for v in covered)
    return {'advances':advances,'declines':declines,'unchanged':sum(v==0 for v in covered),
            'unavailable':len(values)-len(covered),'covered':len(covered),'total':len(values),
            'ratio':advances/declines if declines else None,
            'net':advances-declines if covered else None}
