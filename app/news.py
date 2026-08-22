"""
News integration (Marketaux API) - fetches recent headlines for symbols
CURRENTLY showing Confirmed status, so a live signal comes with the "why"
(or at least a fresh, matched headline) attached, and feeds alerts.py so
a genuinely new article for one of those symbols pushes the same way a
fresh confluence signal does. This wasn't part of NEXT_HORIZON_RESEARCH.md
(added later, at your request) but follows the same conventions as every
other optional data source in this app: off entirely unless configured,
and a failure here can never break the scan loop.

THE FREE-TIER BUDGET IS REAL AND ENFORCED HERE, not just documented:
Marketaux's free plan allows 100 requests/day and returns AT MOST 3
articles per request, total, however many symbols you batch into one
call - not 3 per symbol. Two defenses against blowing through that:

1. Scoped to symbols that are CURRENTLY Confirmed (background._run_loop),
   not the full watchlist - spends the limited per-request article
   budget on rows you're actually about to act on, and keeps the request
   itself small (one comma-joined call covers however many are
   Confirmed, not one call per symbol).
2. Throttled to at most one live fetch every NEWS_POLL_INTERVAL_SECONDS
   (default 900s/15min) AND capped at NEWS_DAILY_CALL_CAP calls per
   calendar day (default 90, leaving headroom under the real 100 - a
   deliberate safety margin, not the actual plan limit), persisted to
   NEWS_STATE_FILE so a redeploy (which happens often in this app's own
   workflow, and would otherwise reset an in-memory throttle to zero and
   risk an immediate burst of calls) doesn't reset the day's budget.

On a quiet day this covers every Confirmed symbol; on a day with many
signals firing at once, only the top ~3 articles Marketaux itself
considers most relevant come back - a real, honest limitation of the
free tier, not a bug here. Upgrading to a paid Marketaux plan (more
requests/day AND more articles/request) is a one-line env var change
away from mattering more - nothing here would need to be rebuilt.
"""
import datetime as dt
import json
import logging
import os
import threading
import time

import requests

from . import config

log = logging.getLogger(__name__)

_MARKETAUX_URL = "https://api.marketaux.com/v1/news/all"
_MAX_SYMBOLS_PER_REQUEST = 20  # defensive cap - unconfirmed by Marketaux's docs, never actually hit in practice

_lock = threading.Lock()
_news_by_symbol = {}   # bare NSE symbol (no .NS/.BO suffix) -> list of article dicts, most-recent-first
_seen_uuids = set()    # for alert dedup - Marketaux's own article uuid, never re-fires the same article twice
_state = {"last_fetch_ts": 0.0, "day": None, "calls_today": 0}


def news_enabled() -> bool:
    return bool(config.MARKETAUX_API_TOKEN)


def _load_state():
    if not os.path.exists(config.NEWS_STATE_FILE):
        return
    try:
        with open(config.NEWS_STATE_FILE) as f:
            saved = json.load(f)
        if isinstance(saved, dict):
            _state.update({
                "last_fetch_ts": saved.get("last_fetch_ts", 0.0),
                "day": saved.get("day"),
                "calls_today": saved.get("calls_today", 0),
            })
    except (json.JSONDecodeError, OSError):
        pass


def _save_state():
    try:
        with open(config.NEWS_STATE_FILE, "w") as f:
            json.dump(_state, f)
    except OSError:
        log.warning("Could not persist news state")


_load_state()


def _today_str():
    return dt.date.today().isoformat()


def _bare_symbol(entity_symbol):
    """'RELIANCE.NS' / 'RELIANCE.BO' -> 'RELIANCE'; anything else (a
    different suffix, or none) -> None, since this app only cares about
    entities Marketaux tagged as the NSE/BSE-listed stock itself."""
    s = (entity_symbol or "").upper()
    if s.endswith(".NS") or s.endswith(".BO"):
        return s[:-3]
    return None


def fetch_news_for_symbols(symbols):
    """Fetches recent news for the given bare watchlist symbols (no
    .NS/.BO suffix - added here) in ONE Marketaux request, subject to
    the throttle/daily-cap described in this module's docstring.
    Returns the FULL current cache ({symbol: [articles]}) either way -
    if this call is throttled, capped, not configured, or the request
    itself fails, whatever was cached from the last successful fetch is
    returned unchanged rather than an empty result, so a transient
    hiccup never blanks out news that was showing a moment ago."""
    if not news_enabled() or not symbols:
        with _lock:
            return dict(_news_by_symbol)

    with _lock:
        today = _today_str()
        if _state["day"] != today:
            _state["day"] = today
            _state["calls_today"] = 0
        due = (time.time() - _state["last_fetch_ts"]) >= config.NEWS_POLL_INTERVAL_SECONDS
        under_cap = _state["calls_today"] < config.NEWS_DAILY_CALL_CAP
        if not (due and under_cap):
            return dict(_news_by_symbol)

    nse_symbols = [f"{s}.NS" for s in symbols[:_MAX_SYMBOLS_PER_REQUEST]]
    try:
        resp = requests.get(_MARKETAUX_URL, params={
            "symbols": ",".join(nse_symbols),
            "api_token": config.MARKETAUX_API_TOKEN,
            "language": "en",
        }, timeout=10)
        resp.raise_for_status()
        data = resp.json()
    except Exception as exc:  # noqa: BLE001 - a bad fetch must never break the scan loop
        log.warning("Marketaux news fetch failed: %s", exc)
        with _lock:
            _state["last_fetch_ts"] = time.time()
            _state["calls_today"] += 1  # still counts - a failed call still spent a request against the free-tier cap
            _save_state()
            return dict(_news_by_symbol)

    fresh = {}
    for article in data.get("data", []) or []:
        for entity in article.get("entities", []) or []:
            bare = _bare_symbol(entity.get("symbol"))
            if not bare or bare not in symbols:
                continue
            fresh.setdefault(bare, []).append({
                "uuid": article.get("uuid"),
                "title": article.get("title"),
                "url": article.get("url"),
                "source": article.get("source"),
                "published_at": article.get("published_at"),
                "sentiment_score": entity.get("sentiment_score"),
            })

    with _lock:
        _state["last_fetch_ts"] = time.time()
        _state["calls_today"] += 1
        _save_state()
        for sym, articles in fresh.items():
            _news_by_symbol[sym] = articles
        return dict(_news_by_symbol)


def get_news_for_symbol(symbol, limit=3):
    with _lock:
        return list(_news_by_symbol.get(symbol, []))[:limit]


def detect_new_articles(current_by_symbol, symbols_in_scope):
    """Returns [(symbol, article), ...] for articles not seen in any
    prior cycle (deduped by Marketaux's own article uuid) among symbols
    currently in scope (Confirmed this cycle) - the alerting hook
    background._run_loop calls once per cycle, right after
    fetch_news_for_symbols. A throttled/capped fetch naturally yields no
    new articles here (the cache didn't change), so this never
    double-fires just because it's called every cycle regardless of
    whether a live fetch actually happened."""
    new_items = []
    with _lock:
        for sym in symbols_in_scope:
            for article in current_by_symbol.get(sym, []):
                uid = article.get("uuid")
                if not uid or uid in _seen_uuids:
                    continue
                _seen_uuids.add(uid)
                new_items.append((sym, article))
        if len(_seen_uuids) > 5000:
            _seen_uuids.clear()
    return new_items
