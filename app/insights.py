"""
Optional "AI Insights" panel: sends the current scan snapshot to Claude
via the Anthropic API and asks for a short natural-language read of
what's happening. Uses YOUR OWN Anthropic API key (ANTHROPIC_API_KEY in
.env) - this app never ships or shares a key on your behalf, and the
panel is simply hidden/disabled if you don't set one.

Results are cached per scan (keyed on the background scanner's
last_scan timestamp) so a page auto-refresh every 60s doesn't fire a
fresh API call every time - only when there's actually new data.
"""
import logging
import threading

from . import config

log = logging.getLogger(__name__)

_lock = threading.Lock()
_cache = {"last_scan": None, "text": None}


def insights_enabled() -> bool:
    return bool(config.ANTHROPIC_API_KEY)


def _build_prompt(results, timeframe, min_required):
    lines = []
    for r in results or []:
        if r.get("error"):
            continue
        lines.append(
            f"- {r['symbol']}: close={r['close']}, RSI={r['rsi']} ({r['rsi_state']}), "
            f"MACD {r['macd_params']} ({r['macd_state']}), EMA9-vs-BBmid ({r['ema_bb_state']}), "
            f"aligned={r['aligned']}/3, fresh_signal={r.get('fresh_signal') or 'none'}"
        )
    data_block = "\n".join(lines) if lines else "(no usable data in this scan yet)"
    return (
        "You are assisting a retail trader reviewing a technical scanner's latest "
        f"output for NSE F&O stocks on the {timeframe} timeframe. The scanner's signal "
        f"rule requires {min_required}-of-3 indicators aligned: RSI(9) vs its 9-period "
        "smoothing line, MACD vs its signal line, and 9 EMA vs the 20-period Bollinger "
        "middle band.\n\n"
        f"Latest scan snapshot:\n{data_block}\n\n"
        "In under 180 words, plain prose (no headers, no bullet list): call out any "
        "stocks with a fresh Bullish or Bearish signal and what's driving it; note any "
        "stocks at 2-of-3 alignment worth watching for the third indicator to confirm; "
        "and mention whether this scan looks unusually quiet or unusually active. Be "
        "strictly factual and specific to the numbers given above - do not invent price "
        "history, news, or fundamentals you don't have. End with one short sentence "
        "reminding this is not investment advice."
    )


def generate_insights(results, timeframe, min_required, last_scan):
    """Returns {"text": ...} or {"error": ...}. Cached per last_scan."""
    if not insights_enabled():
        return {"error": "AI Insights is off - add ANTHROPIC_API_KEY to your .env to enable it."}

    if not results:
        return {"error": "No scan results yet - insights will appear after the first scan."}

    with _lock:
        if _cache["last_scan"] == last_scan and _cache["text"] is not None:
            return {"text": _cache["text"], "cached": True}

    try:
        import anthropic
    except ImportError:
        return {"error": "The 'anthropic' package isn't installed - run: pip install anthropic"}

    try:
        client = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)
        resp = client.messages.create(
            model=config.ANTHROPIC_MODEL,
            max_tokens=400,
            messages=[{"role": "user", "content": _build_prompt(results, timeframe, min_required)}],
        )
        text = "".join(
            block.text for block in resp.content if getattr(block, "type", "") == "text"
        ).strip()
        if not text:
            return {"error": "Claude returned an empty response - try again shortly."}
        with _lock:
            _cache["last_scan"] = last_scan
            _cache["text"] = text
        return {"text": text}
    except Exception as exc:  # noqa: BLE001
        log.warning("AI insights generation failed: %s", exc)
        return {"error": f"Couldn't generate insights right now ({exc})."}
