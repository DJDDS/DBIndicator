"""
Optional "AI Insights" panel: sends the current scan snapshot to Claude
via the Anthropic API and asks for a short natural-language read of
what's happening. Uses YOUR OWN Anthropic API key (ANTHROPIC_API_KEY in
.env) - this app never ships or shares a key on your behalf, and the
panel is simply hidden/disabled if you don't set one.

Results are cached per scan (keyed on the background scanner's
last_scan/last_scan_4h timestamps) so a page auto-refresh every 60s
doesn't fire a fresh API call every time - only when there's actually
new data from either the main-timeframe scan or the 4-hour scan.
"""
import logging
import threading

from . import config

log = logging.getLogger(__name__)

_lock = threading.Lock()
_cache = {"key": None, "text": None}


def insights_enabled() -> bool:
    return bool(config.ANTHROPIC_API_KEY)


def _format_oi(r):
    oi = r.get("oi")
    if oi is None:
        return ""
    hi, lo = r.get("oi_day_high"), r.get("oi_day_low")
    if hi is not None and lo is not None:
        return f", OI={oi} (day range {lo}-{hi})"
    return f", OI={oi}"


def _format_rows(results):
    lines = []
    for r in results or []:
        if r.get("error"):
            continue
        lines.append(
            f"- {r['symbol']}: close={r['close']}, RSI={r['rsi']} ({r['rsi_state']}), "
            f"MACD {r['macd_params']} ({r['macd_state']}), EMA9-vs-BBmid ({r['ema_bb_state']}), "
            f"aligned={r['aligned']}/3, fresh_signal={r.get('fresh_signal') or 'none'}"
            f"{_format_oi(r)}"
        )
    return "\n".join(lines) if lines else "(no usable data in this scan yet)"


def _build_prompt(results, timeframe, min_required, results_4h):
    data_block = _format_rows(results)
    fourh_block = _format_rows(results_4h) if results_4h else None

    sections = [
        "You are assisting a retail trader reviewing a technical scanner's latest "
        f"output for NSE F&O stocks. The scanner's signal rule requires "
        f"{min_required}-of-3 indicators aligned: RSI vs its smoothing line, MACD vs "
        "its signal line, and EMA vs the Bollinger middle band. Where shown, OI is "
        "the stock's current Open Interest from its near-month futures contract.",
        f"Latest scan snapshot ({timeframe} timeframe):\n{data_block}",
    ]
    if fourh_block:
        sections.append(f"Separate 4-hour timeframe scan (same watchlist):\n{fourh_block}")

    instructions = (
        "In under 220 words, plain prose (no headers, no bullet list): call out any "
        f"stocks with a fresh Bullish or Bearish signal on the {timeframe} scan and "
        "what's driving it; note any stocks at 2-of-3 alignment worth watching for the "
        "third indicator to confirm; if OI figures are present, mention any stock "
        "where OI looks notably high/low within its day range alongside a signal, "
        "since that combination (price signal + heavy OI) is often more significant "
        "than either alone."
    )
    if fourh_block:
        instructions += (
            " Then, specifically call out which stocks are currently 3-of-3 (or "
            f"{min_required}-of-3) aligned Bullish or Bearish on the separate 4-hour "
            "scan above - the 4-hour reads matter more for the bigger-picture trend "
            "than the faster timeframe, so flag any disagreement between the two "
            "(e.g. a stock bullish intraday but bearish on 4-hour, or vice versa) as "
            "worth extra caution."
        )
    instructions += (
        " Mention whether this scan looks unusually quiet or unusually active. Be "
        "strictly factual and specific to the numbers given above - do not invent "
        "price history, news, or fundamentals you don't have. End with one short "
        "sentence reminding this is not investment advice."
    )
    sections.append(instructions)
    return "\n\n".join(sections)


def generate_insights(results, timeframe, min_required, last_scan, results_4h=None, last_scan_4h=None):
    """Returns {"text": ...} or {"error": ...}. Cached per (last_scan,
    last_scan_4h) pair, so a fresh call fires whenever either the main
    or the 4-hour scan produces new data."""
    if not insights_enabled():
        return {"error": "AI Insights is off - add ANTHROPIC_API_KEY to your .env to enable it."}

    if not results:
        return {"error": "No scan results yet - insights will appear after the first scan."}

    cache_key = (last_scan, last_scan_4h)
    with _lock:
        if _cache["key"] == cache_key and _cache["text"] is not None:
            return {"text": _cache["text"], "cached": True}

    try:
        import anthropic
    except ImportError:
        return {"error": "The 'anthropic' package isn't installed - run: pip install anthropic"}

    try:
        client = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)
        resp = client.messages.create(
            model=config.ANTHROPIC_MODEL,
            max_tokens=450,
            messages=[{"role": "user", "content": _build_prompt(results, timeframe, min_required, results_4h)}],
        )
        text = "".join(
            block.text for block in resp.content if getattr(block, "type", "") == "text"
        ).strip()
        if not text:
            return {"error": "Claude returned an empty response - try again shortly."}
        with _lock:
            _cache["key"] = cache_key
            _cache["text"] = text
        return {"text": text}
    except Exception as exc:  # noqa: BLE001
        log.warning("AI insights generation failed: %s", exc)
        return {"error": f"Couldn't generate insights right now ({exc})."}
