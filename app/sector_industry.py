"""NSE industry classification for the sector page's industry groups (ADMIN/MEMBER read-only analytics).

NSE classifies every listed stock on 4 levels: macro-economic sector > sector > industry > basic industry (~150 groups,
e.g. 'Private Sector Bank' vs 'Public Sector Bank' vs 'Non Banking Financial Company (NBFC)').  The basic industry comes from
NSE's per-stock quote API; it changes rarely, so it is refreshed once a week outside market hours, a stock at a time, and cached.
Fallback when a stock's quote cannot be read: the broad 'Industry' column of NSE's Nifty 500 list.
"""
from __future__ import annotations

import datetime as dt
import io
import logging
import time
from urllib.parse import quote

import pandas as pd

from . import sector_data as data

log = logging.getLogger(__name__)
NIFTY500 = 'https://nsearchives.nseindia.com/content/indices/ind_nifty500list.csv'
QUOTE = 'https://www.nseindia.com/api/quote-equity?symbol='
REFRESH_DAYS = 7
MIN_GROUP = 2


def parse_quote(js) -> dict | None:
    info = (js or {}).get('industryInfo') or {}
    if not info.get('basicIndustry'): return None
    return {'macro': info.get('macro'), 'sector': info.get('sector'), 'industry': info.get('industry'), 'basic': info.get('basicIndustry')}


def parse_nifty500(raw: bytes) -> dict:
    try:
        df = pd.read_csv(io.BytesIO(raw))
        return {str(s).strip(): str(i).strip() for s, i in zip(df['Symbol'], df['Industry']) if isinstance(i, str) and i.strip()}
    except Exception:  # noqa: BLE001
        return {}


def load(root) -> dict:
    return data.load_json(root / 'industry.json', {}) or {}


def stale(entry: dict | None, today: dt.date) -> bool:
    if not entry or not entry.get('basic'): return True
    try: return (today - dt.date.fromisoformat(str(entry.get('at'))[:10])).days >= REFRESH_DAYS
    except ValueError: return True


def needs_refresh(root, symbols, today: dt.date) -> bool:
    cls = load(root)
    return any(stale(cls.get(s), today) for s in symbols)


def refresh(root, symbols, nse, today: dt.date, budget_s: float = 900, pause: float = 1.0) -> dict:
    """Fill / renew the classification of `symbols`; saves every 25 stocks so an interrupted run keeps its progress."""
    cls = load(root)
    raw = nse.get(NIFTY500)
    broad = parse_nifty500(raw) if raw else {}
    for s, ind in broad.items():
        cls.setdefault(s, {}); cls[s]['broad'] = ind
    t0 = time.time(); done = 0
    for s in sorted(symbols):
        if time.time() - t0 > budget_s: break
        if not stale(cls.get(s), today): continue
        try:
            q = parse_quote(nse.get(QUOTE + quote(s), json_ok=True))
        except Exception:  # noqa: BLE001
            q = None
        if q:
            cls.setdefault(s, {}).update(q, at=today.isoformat()); done += 1
            if done % 25 == 0: data.save_json(root / 'industry.json', cls)
        time.sleep(pause)
    data.save_json(root / 'industry.json', cls)
    return cls


def group_of(entry: dict | None) -> tuple[str, str] | None:
    """(group name, parent sector label): basic industry when known, else the broad Nifty 500 industry."""
    if not entry: return None
    if entry.get('basic'): return entry['basic'], entry.get('sector') or entry.get('macro') or ''
    if entry.get('broad'): return entry['broad'], ''
    return None
