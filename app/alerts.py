"""
Alerting: fires whenever a fresh Bullish/Bearish confluence signal shows
up on the last CLOSED candle for a symbol AND that signal is "confirmed"
(indicators.compute_signal's signal_confirmed - volume above its own
20-bar average, plus higher-timeframe agreement where that's computed).
This mirrors the dashboard's own opt-in "Confirmed" filter, so an alert
means the same thing there and here - added specifically to cut down on
false-signal pings on noisy 15-min candles.

Two channels:
1. Telegram - a message pushed to your phone, works even if the
   dashboard tab/browser is closed. Uses your own bot (TELEGRAM_BOT_TOKEN
   / TELEGRAM_CHAT_ID in .env - see README). Disabled automatically if
   not configured.
2. In-app - every fresh, confirmed signal is kept in a small rolling log
   regardless of Telegram config; the dashboard polls /api/alerts/recent
   and shows a toast + beep for anything new while the tab is open.

Dedup'd per (symbol, timeframe, candle timestamp, direction) so the same
signal doesn't re-fire every scan interval while it's still the most
recently closed candle.
"""
import collections
import datetime as dt
import logging
import threading

import requests

from . import config
from .config import settings
from .scanner import now_ist

log = logging.getLogger(__name__)

_lock = threading.Lock()
_seen = set()
_recent = collections.deque(maxlen=100)
_recent_oi = collections.deque(maxlen=100)
_recent_news = collections.deque(maxlen=100)

_TELEGRAM_API = "https://api.telegram.org/bot{token}/{method}"


def telegram_enabled() -> bool:
    return bool(config.TELEGRAM_BOT_TOKEN and config.TELEGRAM_CHAT_ID)


def _telegram_call(method, **params):
    url = _TELEGRAM_API.format(token=config.TELEGRAM_BOT_TOKEN, method=method)
    resp = requests.post(url, json=params, timeout=10) if params else requests.get(url, timeout=10)
    resp.raise_for_status()
    return resp.json()


def send_test_alert():
    if not telegram_enabled():
        return {"error": "Telegram isn't configured - set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID in .env, then restart."}
    try:
        _telegram_call("sendMessage", chat_id=config.TELEGRAM_CHAT_ID,
                        text="✅ Scanner test alert - Telegram is wired up correctly.")
        return {"ok": True}
    except Exception as exc:  # noqa: BLE001
        log.warning("Telegram test alert failed: %s", exc)
        return {"error": str(exc)}


def discover_chat_id():
    """Helper for the Settings page: after you message your bot once,
    this reads Telegram's getUpdates so you can find your chat_id
    without hand-parsing raw JSON yourself."""
    if not config.TELEGRAM_BOT_TOKEN:
        return {"error": "Set TELEGRAM_BOT_TOKEN in .env first, then restart the app."}
    try:
        url = _TELEGRAM_API.format(token=config.TELEGRAM_BOT_TOKEN, method="getUpdates")
        resp = requests.get(url, timeout=10)
        resp.raise_for_status()
        updates = resp.json().get("result", [])
        found, seen_ids = [], set()
        for u in reversed(updates):
            chat = ((u.get("message") or {}).get("chat")) or {}
            cid = chat.get("id")
            if cid is not None and cid not in seen_ids:
                seen_ids.add(cid)
                found.append({"chat_id": cid, "from": chat.get("username") or chat.get("first_name") or "unknown"})
        if not found:
            return {"error": "No messages found yet - send any message to your bot on Telegram, then click this again."}
        return {"chat_ids": found}
    except Exception as exc:  # noqa: BLE001
        log.warning("Telegram getUpdates failed: %s", exc)
        return {"error": str(exc)}


def _format_message(r, timeframe):
    alert_direction = r.get("trade_direction") or r.get("breakout_direction") or r.get("entry_trigger") or r.get("fresh_signal") or r.get("direction")
    arrow = "\U0001F53A" if alert_direction == "Bullish" else "\U0001F53B"
    score = r.get("movement_score")
    oi60 = r.get("oi_chg_60m_pct")
    accel = r.get("oi_acceleration")
    tod = r.get("tod_rvol")
    rs = r.get("rs_pct")
    score_note = f"Score {score}" if score is not None else "Score —"
    oi_note = f"OI60 {oi60:+.2f}%" if oi60 is not None else "OI60 —"
    accel_note = f"Accel {accel:+.2f}pp" if accel is not None else "Accel —"
    tod_note = f"TOD RVOL {tod}x" if tod is not None else "TOD RVOL —"
    rs_note = f"RS {rs:+.2f}pp" if rs is not None else "RS —"
    htf_note = r.get("htf_direction") or "—"
    source = r.get("breakout_source") or "15m range"
    level = r.get("breakout_level")
    oi_status = r.get("oi_status") or "—"
    ext = r.get("breakout_extension_atr")
    level_note = f" @ {level}" if level is not None else ""
    ext_note = f" | Ext {ext:.2f} ATR" if isinstance(ext, (int, float)) else ""
    return (
        f"{arrow} {r['symbol']} - {alert_direction} F&O Breakout Entry ({timeframe})\n"
        f"{source} breakout{level_note} | Close {r['close']} | {score_note}\n"
        f"{oi_note} | {accel_note} | OI {oi_status} | {tod_note}\n"
        f"{rs_note} | HTF {htf_note}{ext_note} | VWAP + anti-chase passed"
    )


def process_scan_results(results, timeframe):
    """Call once after every completed scan. Alerts are generated only for
    rows already ranked in Best Entries, so Telegram/in-app alerts cannot
    bypass the same fresh-trigger, OI, anti-chase and evidence-quality rules
    used by the dashboard shortlist."""
    for r in results or []:
        if r.get("error"):
            continue
        # Alerts and the dashboard must use the same definition of a best
        # entry. Otherwise the user is pinged for rows the shortlist itself
        # rejected as late, weak-OI or low-quality.
        if not r.get("shortlist_rank"):
            continue
        alert_direction = r.get("trade_direction") or r.get("breakout_direction") or r.get("entry_trigger") or r.get("fresh_signal") or r.get("direction")
        if alert_direction not in ("Bullish", "Bearish"):
            continue
        key = (r["symbol"], timeframe, str(r.get("timestamp")), alert_direction)
        with _lock:
            if key in _seen:
                continue
            _seen.add(key)
            if len(_seen) > 5000:
                _seen.clear()

        text = _format_message(r, timeframe)
        entry = {
            "symbol": r["symbol"], "direction": alert_direction, "timeframe": timeframe,
            "close": r["close"], "aligned": r.get("aligned"), "movement_score": r.get("movement_score"), "text": text,
            "candle_timestamp": str(r.get("timestamp")),
        }
        with _lock:
            _recent.append(entry)

        if telegram_enabled():
            try:
                _telegram_call("sendMessage", chat_id=config.TELEGRAM_CHAT_ID, text=text)
            except Exception as exc:  # noqa: BLE001 - never let alerting break the scan loop
                log.warning("Telegram alert failed for %s: %s", r["symbol"], exc)



# --------------------------------------------------------------------------
# Daily BTST/STBT publish
#
# Every other alert in this module is event-driven - something crossed, so
# fire. This one is TIME-driven, because the thing it reports only becomes
# true near the close: the BTST/STBT panel's hard gate is a strong close on
# the DAILY bar, and that bar is still being written until 15:30. A candidate
# at 11am is a statement about where price sits in a range that has six hours
# left to move, which is a different quantity that happens to share a name.
#
# So this fires once per trading day at BTST_ALERT_TIME (15:15 IST by
# default): late enough that the close position is essentially settled, early
# enough to still place an order before the bell. Once per day, deduped on the
# calendar date, so the remaining scan cycles before 15:30 cannot re-send it.
# --------------------------------------------------------------------------

_btst_sent_date = None


def _format_btst_message(rows, now):
    lines = [f"BTST / STBT candidates - {now.strftime('%d %b %Y, %H:%M')} IST", ""]
    if not rows:
        lines.append("No candidates. Nothing closed strong enough in its own direction today.")
        lines.append("")
        lines.append("An empty list is a result, not a failure - it means the screen "
                      "found nothing worth carrying overnight.")
        return "\n".join(lines)
    for r in rows:
        lines.append(f"{r['btst_side']}  {r['symbol']}  {r.get('close')}   "
                      f"({r.get('btst_score')} of {r.get('btst_max') or len(r.get('btst_reasons') or [])} checks held)")
        for w in (r.get("btst_reasons") or []):
            mark = "+" if w.get("ok") is True else ("-" if w.get("ok") is False else "?")
            lines.append(f"   {mark} {w.get('text')}")
        lines.append("")
    lines.append("The daily bar is nearly closed but not quite - the last few minutes "
                  "can still move a close position. Check before you place.")
    return "\n".join(lines)


def publish_btst_candidates(results, now):
    """Call once per scan cycle. Fires only inside the publish window and only
    once per calendar day; returns True if it actually sent.

    The window is [BTST_ALERT_TIME, BTST_ALERT_TIME + 10 min) rather than an
    exact time, because scans land on their own cadence (every
    SCAN_INTERVAL_SECONDS) and would otherwise step straight over a single
    instant and never fire at all."""
    global _btst_sent_date
    if not settings.BTST_ALERT_ENABLED:
        return False
    try:
        hh, mm = (int(x) for x in str(settings.BTST_ALERT_TIME).split(":"))
    except (ValueError, AttributeError):
        log.warning("BTST_ALERT_TIME is not HH:MM (%r) - skipping", settings.BTST_ALERT_TIME)
        return False

    target = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
    if not (target <= now < target + dt.timedelta(minutes=10)):
        return False

    today = now.date().isoformat()
    with _lock:
        if _btst_sent_date == today:
            return False
        _btst_sent_date = today

    rows = sorted(
        [r for r in (results or []) if not r.get("error") and r.get("btst_side")],
        key=lambda r: r.get("btst_score") or 0, reverse=True,
    )
    text = _format_btst_message(rows, now)
    with _lock:
        _recent.append({"symbol": "BTST/STBT", "direction": "Daily publish",
                         "timeframe": "day", "close": None,
                         "aligned": len(rows), "text": text,
                         "candle_timestamp": now.isoformat(timespec="seconds")})
    if telegram_enabled():
        try:
            _telegram_call("sendMessage", chat_id=config.TELEGRAM_CHAT_ID, text=text)
        except Exception as exc:  # noqa: BLE001 - alerting must never break the scan loop
            log.warning("BTST publish failed: %s", exc)
    return True


def get_recent(limit=20):
    with _lock:
        items = list(_recent)[-limit:]
    return list(reversed(items))


def _format_oi_message(r, timeframe):
    break_note = f" - {r['oi_break_signal']}" if r.get("oi_break_signal") else ""
    chg = r.get("oi_chg_today_pct")
    chg_note = f", {chg:+.1f}% today" if chg is not None else ""
    accel = r.get("oi_acceleration")
    accel_note = f" - accel {accel:+.2f}pp (30m)" if accel is not None else ""
    dir_note = f" ({r['direction']} confluence)" if r.get("direction") else ""
    label = r.get("oi_accel_label") or "Accelerating"
    return f"\U0001F4C8 {r['symbol']} OI just started {label}{break_note}{chg_note}{accel_note}{dir_note} on {timeframe}"


def process_oi_events(events, timeframe):
    """Records OI-acceleration transition events (background.py's
    _detect_oi_accel_events - fires once when a symbol's OI trend just
    turned "Strong acceleration" or "Moderate acceleration", not every
    scan while it stays that way) to a small in-app rolling log the
    dashboard polls via /api/alerts/oi_recent for a toast pop. Kept
    separate from the price-signal alerts above since these fire on a
    different condition (OI acceleration, not RSI/MACD/EMA confluence)
    and In-app only for now - not pushed to Telegram, to avoid doubling
    up phone notifications for something that isn't a trade signal by
    itself."""
    for r in events or []:
        entry = {
            "symbol": r.get("symbol"), "timeframe": timeframe, "close": r.get("close"),
            "direction": r.get("direction"), "oi": r.get("oi"),
            "oi_chg_today_pct": r.get("oi_chg_today_pct"),
            "oi_acceleration": r.get("oi_acceleration"),
            "oi_accel_label": r.get("oi_accel_label"),
            "oi_break_signal": r.get("oi_break_signal"),
            "text": _format_oi_message(r, timeframe),
            "detected_at": now_ist().isoformat(timespec="seconds"),
        }
        with _lock:
            _recent_oi.append(entry)


def get_recent_oi(limit=20):
    with _lock:
        items = list(_recent_oi)[-limit:]
    return list(reversed(items))


def _format_news_message(symbol, article):
    src = article.get("source") or "news"
    sent = article.get("sentiment_score")
    sent_note = f" (sentiment {sent:+.2f})" if isinstance(sent, (int, float)) else ""
    return f"\U0001F4F0 {symbol}: {article.get('title')}{sent_note}\n{src} — {article.get('url')}"


def process_news_articles(new_items, timeframe):
    """new_items: [(symbol, article_dict), ...] from news.
    detect_new_articles - fires once per genuinely new article (deduped
    by Marketaux's own article uuid) for a symbol that was Confirmed at
    the time it was fetched (see background._run_loop). Sent to BOTH
    Telegram and the in-app rolling log - unlike OI-acceleration events
    above (in-app only, since those aren't a trade signal by
    themselves), fresh news on a Confirmed symbol IS exactly the "move
    fast" signal this feature was built for, so it gets the same
    push-notification treatment as a fresh confluence signal."""
    for symbol, article in new_items or []:
        entry = {
            "symbol": symbol, "timeframe": timeframe, "title": article.get("title"),
            "url": article.get("url"), "source": article.get("source"),
            "published_at": article.get("published_at"),
            "sentiment_score": article.get("sentiment_score"),
            "text": _format_news_message(symbol, article),
            "detected_at": now_ist().isoformat(timespec="seconds"),
        }
        with _lock:
            _recent_news.append(entry)
        if telegram_enabled():
            try:
                _telegram_call("sendMessage", chat_id=config.TELEGRAM_CHAT_ID, text=entry["text"])
            except Exception as exc:  # noqa: BLE001 - alerting must never break scanning
                log.warning("Telegram news alert failed for %s: %s", symbol, exc)


def get_recent_news(limit=20):
    with _lock:
        items = list(_recent_news)[-limit:]
    return list(reversed(items))
