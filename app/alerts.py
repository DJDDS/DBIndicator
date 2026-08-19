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
import logging
import threading

import requests

from . import config

log = logging.getLogger(__name__)

_lock = threading.Lock()
_seen = set()
_recent = collections.deque(maxlen=100)

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
    arrow = "\U0001F53A" if r["fresh_signal"] == "Bullish" else "\U0001F53B"
    vol_note = f"Vol {r['vol_multiple']}x" if r.get("vol_multiple") is not None else "Vol confirmed"
    htf_note = ", higher-timeframe trend agrees" if r.get("htf_direction") else ""
    return (
        f"{arrow} {r['symbol']} - {r['fresh_signal']} confluence on {timeframe} (confirmed: {vol_note}{htf_note})\n"
        f"Close: {r['close']} | RSI {r['rsi']} ({r['rsi_state']}) | "
        f"MACD {r['macd_params']} ({r['macd_state']}) | EMA/BB ({r['ema_bb_state']}) | "
        f"Aligned {r['aligned']}/3"
    )


def process_scan_results(results, timeframe):
    """Call once after every completed scan. Sends Telegram alerts (if
    configured) and always records fresh, CONFIRMED signals to the
    in-app log - fresh_signal alone (a bare 2-of-3 or 3-of-3 crossover)
    is no longer enough; vol_confirmed and htf_agrees (see
    indicators.compute_signal) must also hold, same bar as the
    dashboard's own "Confirmed" filter, so you don't get pinged for
    something the dashboard itself wouldn't flag as solid."""
    for r in results or []:
        if r.get("error") or not r.get("fresh_signal"):
            continue
        if not (r.get("vol_confirmed") and r.get("htf_agrees", True)):
            continue
        key = (r["symbol"], timeframe, str(r.get("timestamp")), r["fresh_signal"])
        with _lock:
            if key in _seen:
                continue
            _seen.add(key)
            if len(_seen) > 5000:
                _seen.clear()

        text = _format_message(r, timeframe)
        entry = {
            "symbol": r["symbol"], "direction": r["fresh_signal"], "timeframe": timeframe,
            "close": r["close"], "aligned": r["aligned"], "text": text,
            "candle_timestamp": str(r.get("timestamp")),
        }
        with _lock:
            _recent.append(entry)

        if telegram_enabled():
            try:
                _telegram_call("sendMessage", chat_id=config.TELEGRAM_CHAT_ID, text=text)
            except Exception as exc:  # noqa: BLE001 - never let alerting break the scan loop
                log.warning("Telegram alert failed for %s: %s", r["symbol"], exc)


def get_recent(limit=20):
    with _lock:
        items = list(_recent)[-limit:]
    return list(reversed(items))
