"""V12.0 forward stock-option recorder primitives.

The recorder is intentionally observational. It captures fixed-clock market
states and executable bid/ask/depth evidence so later research can decide
whether an earnings or final-week option study is feasible. It does not create
Trial 25 and it does not infer a trade direction.
"""
from __future__ import annotations

import datetime as dt
import math
import time
from typing import Iterable

from . import derivative_intelligence

SNAPSHOT_SLOTS = (
    ("OPEN_STABLE", dt.time(9, 30)),
    ("MIDDAY", dt.time(13, 0)),
    ("PRE_CAS", dt.time(15, 10)),
    ("POST_CAS", dt.time(15, 37)),
)
DEFAULT_GRACE_MINUTES = 7
QUOTE_BATCH_SIZE = 400


def _finite(value) -> bool:
    try:
        return value is not None and math.isfinite(float(value))
    except (TypeError, ValueError):
        return False


def _num(value, default=None):
    return float(value) if _finite(value) else default


def _as_date(value):
    if isinstance(value, dt.datetime):
        return value.date()
    return value if isinstance(value, dt.date) else None


def due_snapshot_slot(now: dt.datetime, state: dict | None, grace_minutes: int = DEFAULT_GRACE_MINUTES) -> str | None:
    """Return the currently due fixed slot, never a missed earlier slot."""
    state = state or {}
    day = now.date().isoformat()
    captured = set((state.get("captured_slots") or {}).get(day) or [])
    for name, clock in SNAPSHOT_SLOTS:
        scheduled = dt.datetime.combine(now.date(), clock)
        delta = (now - scheduled).total_seconds() / 60.0
        if 0 <= delta <= max(0, int(grace_minutes)):
            return None if name in captured else name
    return None


def _live_expiries(contracts: Iterable[dict], today: dt.date) -> list[dt.date]:
    expiries = {
        _as_date(row.get("expiry"))
        for row in contracts
        if row.get("instrument_type") in ("CE", "PE") and _as_date(row.get("expiry")) is not None
    }
    return sorted(expiry for expiry in expiries if expiry >= today)[:2]


def _with_underlying(row: dict, symbol: str) -> dict:
    out = dict(row)
    out["underlying"] = symbol
    if isinstance(out.get("expiry"), dt.datetime):
        out["expiry"] = out["expiry"].date()
    return out


def select_broad_atm_contracts(contracts_map: dict, spot_map: dict, *, today: dt.date | None = None) -> list[dict]:
    today = today or dt.date.today()
    selected = {}
    for symbol, raw_contracts in sorted((contracts_map or {}).items()):
        spot = _num((spot_map or {}).get(symbol))
        if spot is None or spot <= 0:
            continue
        contracts = list(raw_contracts or [])
        for expiry in _live_expiries(contracts, today):
            exp_rows = [r for r in contracts if _as_date(r.get("expiry")) == expiry and r.get("instrument_type") in ("CE", "PE") and _finite(r.get("strike"))]
            strikes = sorted({_num(r.get("strike")) for r in exp_rows if _num(r.get("strike")) is not None})
            if not strikes:
                continue
            atm = min(strikes, key=lambda strike: abs(strike - spot))
            for row in exp_rows:
                if _num(row.get("strike")) != atm:
                    continue
                key = row.get("tradingsymbol") or str(row.get("instrument_token"))
                if key:
                    selected[key] = _with_underlying(row, symbol)
    return list(selected.values())


def select_deep_contracts(
    contracts_map: dict,
    spot_map: dict,
    symbols: Iterable[str],
    *,
    today: dt.date | None = None,
    wings: int = 6,
) -> list[dict]:
    today = today or dt.date.today()
    selected = {}
    wings = max(0, int(wings))
    for symbol in symbols or []:
        symbol = str(symbol)
        spot = _num((spot_map or {}).get(symbol))
        contracts = list((contracts_map or {}).get(symbol) or [])
        if spot is None or spot <= 0 or not contracts:
            continue
        for expiry in _live_expiries(contracts, today):
            exp_rows = [r for r in contracts if _as_date(r.get("expiry")) == expiry and r.get("instrument_type") in ("CE", "PE") and _finite(r.get("strike"))]
            strikes = sorted({_num(r.get("strike")) for r in exp_rows if _num(r.get("strike")) is not None})
            if not strikes:
                continue
            atm = min(strikes, key=lambda strike: abs(strike - spot))
            atm_index = strikes.index(atm)
            keep = set(strikes[max(0, atm_index - wings): min(len(strikes), atm_index + wings + 1)])
            for row in exp_rows:
                if _num(row.get("strike")) not in keep:
                    continue
                key = row.get("tradingsymbol") or str(row.get("instrument_token"))
                if key:
                    selected[key] = _with_underlying(row, symbol)
    return list(selected.values())


def rank_deep_symbols(broad_summaries: dict, earnings_symbols: set[str] | None, *, limit: int = 40) -> list[str]:
    broad_summaries = broad_summaries or {}
    earnings = {str(s) for s in (earnings_symbols or set())}
    candidates = set(broad_summaries) | earnings

    def score(symbol):
        value = _num((broad_summaries.get(symbol) or {}).get("liquidity_score"), -1.0)
        return value

    event_names = sorted((s for s in candidates if s in earnings), key=lambda s: (score(s), s), reverse=True)
    normal_names = sorted((s for s in candidates if s not in earnings), key=lambda s: (score(s), s), reverse=True)
    return (event_names + normal_names)[: max(0, int(limit))]


def quote_in_batches(
    kite, keys: Iterable[str], *, batch_size: int = QUOTE_BATCH_SIZE,
    sleep_fn=None, min_interval_seconds: float = 1.02,
) -> tuple[dict, list[dict]]:
    """Fetch full quotes in conservative batches while respecting Kite's 1 r/s limit."""
    keys = list(dict.fromkeys(str(key) for key in (keys or []) if key))
    batch_size = min(400, max(1, int(batch_size)))
    sleep_fn = sleep_fn or time.sleep
    quotes: dict = {}
    errors: list[dict] = []
    for i in range(0, len(keys), batch_size):
        chunk = keys[i:i + batch_size]
        batch_index = i // batch_size + 1
        if batch_index > 1:
            sleep_fn(max(1.0, float(min_interval_seconds)))
        try:
            payload = kite.quote(chunk) or {}
            if isinstance(payload, dict):
                quotes.update(payload)
            else:
                errors.append({"batch_index": batch_index, "size": len(chunk), "error": "non-dict quote payload"})
        except Exception as exc:  # noqa: BLE001 - partial recorder failure is auditable, not fatal
            errors.append({"batch_index": batch_index, "size": len(chunk), "error": str(exc)})
    return quotes, errors


def _normalize_depth(rows) -> list[dict]:
    out = []
    for row in list(rows or [])[:5]:
        if not isinstance(row, dict):
            continue
        out.append({
            "price": _num(row.get("price")),
            "quantity": int(row.get("quantity") or 0),
            "orders": int(row.get("orders") or 0),
        })
    return out


def _best(depth_rows: list[dict]) -> float | None:
    if not depth_rows:
        return None
    value = _num(depth_rows[0].get("price"))
    return value if value is not None and value > 0 else None


def _stale_flag(quote: dict, now: dt.datetime, *, seconds: int = 600):
    raw = quote.get("last_trade_time") or quote.get("timestamp")
    if raw is None:
        return None
    if isinstance(raw, str):
        try:
            raw = dt.datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            return None
    if not isinstance(raw, dt.datetime):
        return None
    if raw.tzinfo is not None and now.tzinfo is None:
        raw = raw.replace(tzinfo=None)
    if raw.tzinfo is None and now.tzinfo is not None:
        now = now.replace(tzinfo=None)
    return (now - raw).total_seconds() > seconds


def normalize_contract_snapshot(contract: dict, quote: dict | None, spot: float, now: dt.datetime, slot: str) -> dict:
    quote = quote or {}
    depth = quote.get("depth") or {}
    buys = _normalize_depth(depth.get("buy"))
    sells = _normalize_depth(depth.get("sell"))
    bid, ask = _best(buys), _best(sells)
    two_sided = bool(bid is not None and ask is not None and ask >= bid)
    mid = (bid + ask) / 2.0 if two_sided else None
    spread_rupees = ask - bid if two_sided else None
    spread_pct = spread_rupees / mid * 100.0 if two_sided and mid and mid > 0 else None

    expiry = _as_date(contract.get("expiry"))
    typ = contract.get("instrument_type")
    strike = _num(contract.get("strike"))
    expiry_dt = dt.datetime.combine(expiry, dt.time(15, 40)) if expiry else None
    t_days = max((expiry_dt - now).total_seconds() / 86400.0, 1.0 / 24.0) if expiry_dt else None
    t = t_days / 365.0 if t_days is not None else None

    def iv(price):
        if not (_finite(price) and _finite(spot) and _finite(strike) and t is not None and t > 0 and typ in ("CE", "PE")):
            return None
        value = derivative_intelligence.implied_volatility(float(price), float(spot), float(strike), t, derivative_intelligence.RISK_FREE_RATE, typ)
        return value * 100.0 if value is not None else None

    bid_iv = iv(bid)
    mid_iv = iv(mid)
    ask_iv = iv(ask)
    greeks = {}
    if mid_iv is not None and t is not None:
        greeks = derivative_intelligence.option_greeks(float(spot), float(strike), t, derivative_intelligence.RISK_FREE_RATE, mid_iv / 100.0, typ)

    return {
        "ts": now.isoformat(timespec="seconds"),
        "slot": str(slot),
        "cas_regime": str(slot) in ("PRE_CAS", "POST_CAS"),
        "underlying": contract.get("underlying") or contract.get("name"),
        "tradingsymbol": contract.get("tradingsymbol"),
        "instrument_token": contract.get("instrument_token"),
        "type": typ,
        "strike": strike,
        "expiry": expiry.isoformat() if expiry else None,
        "dte": max(0, (expiry - now.date()).days) if expiry else None,
        "lot_size": contract.get("lot_size"),
        "spot": _num(spot),
        "last_price": _num(quote.get("last_price")),
        "volume": quote.get("volume"),
        "oi": quote.get("oi"),
        "depth": {"buy": buys, "sell": sells},
        "best_bid": round(bid, 6) if bid is not None else None,
        "best_ask": round(ask, 6) if ask is not None else None,
        "mid": round(mid, 6) if mid is not None else None,
        "spread_rupees": round(spread_rupees, 6) if spread_rupees is not None else None,
        "spread_pct": round(spread_pct, 4) if spread_pct is not None else None,
        "two_sided": two_sided,
        "bid_iv_pct": round(bid_iv, 4) if bid_iv is not None else None,
        "mid_iv_pct": round(mid_iv, 4) if mid_iv is not None else None,
        "ask_iv_pct": round(ask_iv, 4) if ask_iv is not None else None,
        "delta": round(_num(greeks.get("delta")), 6) if _num(greeks.get("delta")) is not None else None,
        "gamma": round(_num(greeks.get("gamma")), 8) if _num(greeks.get("gamma")) is not None else None,
        "theta_per_day": round(_num(greeks.get("theta_per_day")), 6) if _num(greeks.get("theta_per_day")) is not None else None,
        "vega": round(_num(greeks.get("vega")), 6) if _num(greeks.get("vega")) is not None else None,
        "stale": _stale_flag(quote, now),
    }


# --- Persistence / complete fixed-slot capture ---------------------------------

def load_v12_state(path) -> dict:
    """Load recorder state; missing/corrupt files fail to an empty state."""
    import json
    from pathlib import Path
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
        if isinstance(raw, dict):
            raw.setdefault("captured_slots", {})
            raw.setdefault("slot_summaries", [])
            raw.setdefault("symbol_stats", {})
            raw.setdefault("quote_contracts", 0)
            raw.setdefault("stale_contracts", 0)
            raw.setdefault("final_week_samples", 0)
            return raw
    except (OSError, ValueError, TypeError):
        pass
    return {
        "version": 1,
        "captured_slots": {},
        "slot_summaries": [],
        "symbol_stats": {},
        "quote_contracts": 0,
        "stale_contracts": 0,
        "final_week_samples": 0,
        "last_capture_at": None,
        "last_error": None,
    }


def _save_v12_state(path, state: dict) -> None:
    import json
    from pathlib import Path
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(state, default=str, separators=(",", ":"), sort_keys=True), encoding="utf-8")
    tmp.replace(path)


def _append_snapshot(path, record: dict) -> None:
    import json
    from pathlib import Path
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, default=str, separators=(",", ":"), sort_keys=True) + "\n")


def _quote_key(contract: dict) -> str | None:
    symbol = contract.get("tradingsymbol")
    return f"NFO:{symbol}" if symbol else None


def _group_broad_summaries(snapshots: list[dict]) -> dict:
    """Build executable ATM straddle and term-structure summaries per symbol."""
    from statistics import median
    grouped = {}
    for snap in snapshots:
        symbol = str(snap.get("underlying") or "")
        expiry = snap.get("expiry")
        typ = snap.get("type")
        if not symbol or not expiry or typ not in ("CE", "PE"):
            continue
        grouped.setdefault(symbol, {}).setdefault(expiry, {})[typ] = snap

    out = {}
    for symbol, expiries in grouped.items():
        expiry_rows = []
        for expiry in sorted(expiries):
            pair = expiries[expiry]
            call = pair.get("CE") or {}
            put = pair.get("PE") or {}
            bid_ok = _finite(call.get("best_bid")) and _finite(put.get("best_bid"))
            ask_ok = _finite(call.get("best_ask")) and _finite(put.get("best_ask"))
            straddle_bid = float(call["best_bid"]) + float(put["best_bid"]) if bid_ok else None
            straddle_ask = float(call["best_ask"]) + float(put["best_ask"]) if ask_ok else None
            two_sided = bool(straddle_bid is not None and straddle_ask is not None and straddle_ask >= straddle_bid and straddle_bid > 0)
            mid = (straddle_bid + straddle_ask) / 2.0 if two_sided else None
            spread_pct = (straddle_ask - straddle_bid) / mid * 100.0 if two_sided and mid else None
            ivs = [float(x) for x in (call.get("mid_iv_pct"), put.get("mid_iv_pct")) if _finite(x)]
            atm_iv = float(median(ivs)) if ivs else None
            dte = call.get("dte") if call.get("dte") is not None else put.get("dte")
            total_variance = None
            if atm_iv is not None and dte is not None:
                years = max(float(dte), 1.0 / 24.0) / 365.0
                total_variance = (atm_iv / 100.0) ** 2 * years
            expiry_rows.append({
                "expiry": expiry,
                "dte": dte,
                "two_sided": two_sided,
                "straddle_bid": round(straddle_bid, 6) if straddle_bid is not None else None,
                "straddle_ask": round(straddle_ask, 6) if straddle_ask is not None else None,
                "straddle_spread_pct": round(spread_pct, 4) if spread_pct is not None else None,
                "atm_iv_pct": round(atm_iv, 4) if atm_iv is not None else None,
                "total_variance": total_variance,
            })
        primary = expiry_rows[0] if expiry_rows else {}
        spread = primary.get("straddle_spread_pct")
        if spread is None:
            liquidity_score = 0.0
        elif spread <= 1.0:
            liquidity_score = 100.0
        elif spread <= 2.0:
            liquidity_score = 85.0
        elif spread <= 4.0:
            liquidity_score = 65.0
        elif spread <= 5.0:
            liquidity_score = 45.0
        else:
            liquidity_score = max(0.0, 40.0 - min(40.0, spread * 3.0))
        out[symbol] = {
            "primary": primary,
            "expiries": expiry_rows,
            "liquidity_score": round(liquidity_score, 1),
            "term_structure_available": len([x for x in expiry_rows if x.get("atm_iv_pct") is not None]) >= 2,
        }
    return out


def _update_symbol_stats(state: dict, broad_summaries: dict, earnings_symbols: set[str]) -> None:
    stats = state.setdefault("symbol_stats", {})
    for symbol, summary in broad_summaries.items():
        row = stats.setdefault(symbol, {
            "broad_snapshots": 0,
            "two_sided_snapshots": 0,
            "spread_values": [],
            "term_structure_snapshots": 0,
            "earnings_quote_snapshots": 0,
        })
        row["broad_snapshots"] = int(row.get("broad_snapshots") or 0) + 1
        primary = summary.get("primary") or {}
        if primary.get("two_sided"):
            row["two_sided_snapshots"] = int(row.get("two_sided_snapshots") or 0) + 1
            if _finite(primary.get("straddle_spread_pct")):
                values = list(row.get("spread_values") or [])
                values.append(float(primary["straddle_spread_pct"]))
                row["spread_values"] = values[-500:]
            if symbol in earnings_symbols:
                row["earnings_quote_snapshots"] = int(row.get("earnings_quote_snapshots") or 0) + 1
        if summary.get("term_structure_available"):
            row["term_structure_snapshots"] = int(row.get("term_structure_snapshots") or 0) + 1


def refresh_spot_map(kite, results: list[dict], *, sleep_fn=None) -> tuple[dict[str, float], list[dict]]:
    """Refresh NSE cash spots at the option snapshot timestamp.

    The live scanner's last close is only a fallback. This matters especially
    for the 15:37 POST_CAS slot, where the cash auction may have moved the
    underlying after the normal continuous-session scan stopped.
    """
    fallback = {
        str(row.get("symbol")): float(row.get("close"))
        for row in (results or [])
        if row.get("symbol") and not row.get("error") and _finite(row.get("close")) and float(row.get("close")) > 0
    }
    keys = [f"NSE:{symbol}" for symbol in sorted(fallback)]
    quotes, errors = quote_in_batches(kite, keys, batch_size=QUOTE_BATCH_SIZE, sleep_fn=sleep_fn)
    out = dict(fallback)
    for symbol in fallback:
        quote = quotes.get(f"NSE:{symbol}") or {}
        price = _num(quote.get("last_price"))
        if price is not None and price > 0:
            out[symbol] = float(price)
    return out, errors


def record_snapshot(
    kite,
    results: list[dict],
    earnings_symbols: set[str] | None,
    *,
    now: dt.datetime,
    snapshot_file,
    state_file,
    contracts_map: dict | None = None,
    deep_symbol_limit: int = 40,
    grace_minutes: int = DEFAULT_GRACE_MINUTES,
    sleep_fn=None,
) -> dict:
    """Capture one due V12 slot. All quote failures are recorded, never raised."""
    state = load_v12_state(state_file)
    slot = due_snapshot_slot(now, state, grace_minutes=grace_minutes)
    if slot is None:
        return {"status": "NOT_DUE", "slot": None, "quote_errors": 0}

    earnings_symbols = {str(x) for x in (earnings_symbols or set())}
    spot_map, spot_errors = refresh_spot_map(kite, results, sleep_fn=sleep_fn)
    if contracts_map is None:
        contracts_map = derivative_intelligence.get_option_contracts_map(kite)

    broad_contracts = select_broad_atm_contracts(contracts_map or {}, spot_map, today=now.date())
    broad_keys = [_quote_key(c) for c in broad_contracts]
    # quote_in_batches paces calls within a phase. Pace the boundaries too,
    # because Kite applies the same 1 r/s Quote limit across NSE spot and NFO.
    phase_sleep = sleep_fn or time.sleep
    if broad_keys and spot_map:
        phase_sleep(1.02)
    broad_quotes, broad_errors = quote_in_batches(kite, [k for k in broad_keys if k], sleep_fn=sleep_fn)
    broad_snaps = []
    for contract in broad_contracts:
        key = _quote_key(contract)
        broad_snaps.append(normalize_contract_snapshot(
            contract, broad_quotes.get(key, {}), spot_map.get(str(contract.get("underlying"))), now, slot
        ))
    broad_summaries = _group_broad_summaries(broad_snaps)

    deep_symbols = rank_deep_symbols(broad_summaries, earnings_symbols, limit=deep_symbol_limit)
    deep_contracts = select_deep_contracts(contracts_map or {}, spot_map, deep_symbols, today=now.date(), wings=6)
    deep_keys = [_quote_key(c) for c in deep_contracts]
    missing_keys = [k for k in deep_keys if k and k not in broad_quotes]
    if missing_keys and broad_keys:
        phase_sleep(1.02)
    deep_quotes, deep_errors = quote_in_batches(kite, missing_keys, sleep_fn=sleep_fn)
    all_quotes = dict(broad_quotes)
    all_quotes.update(deep_quotes)
    deep_snaps = []
    for contract in deep_contracts:
        key = _quote_key(contract)
        deep_snaps.append(normalize_contract_snapshot(
            contract, all_quotes.get(key, {}), spot_map.get(str(contract.get("underlying"))), now, slot
        ))

    quote_errors = list(spot_errors) + list(broad_errors) + list(deep_errors)
    capture_status = "CAPTURED_PARTIAL" if quote_errors else "CAPTURED"
    slot_summary = {
        "date": now.date().isoformat(),
        "ts": now.isoformat(timespec="seconds"),
        "slot": slot,
        "status": capture_status,
        "broad_symbols": len(broad_summaries),
        "two_sided_symbols": sum(bool((x.get("primary") or {}).get("two_sided")) for x in broad_summaries.values()),
        "deep_symbols": len(deep_symbols),
        "deep_symbol_list": deep_symbols,
        "term_structure_symbols": sum(bool(x.get("term_structure_available")) for x in broad_summaries.values()),
        "earnings_symbols": sorted(earnings_symbols),
        "quote_errors": quote_errors,
    }
    record = {
        "record_type": "V12_OPTION_SLOT",
        "build": "2026-09-05-INSTITUTIONAL-V12.0-OPTION-EDGE-RECORDER-TRADE-CONSOLE",
        "ts": now.isoformat(timespec="seconds"),
        "date": now.date().isoformat(),
        "slot": slot,
        "status": capture_status,
        "trial25_locked": True,
        "broad_contracts": broad_snaps,
        "broad_summaries": broad_summaries,
        "deep_symbols": deep_symbols,
        "deep_contracts": deep_snaps,
        "quote_errors": quote_errors,
    }

    try:
        _append_snapshot(snapshot_file, record)
    except OSError as exc:
        state["last_error"] = f"snapshot write failed: {exc}"
        _save_v12_state(state_file, state)
        return {"status": "WRITE_FAILED", "slot": slot, "quote_errors": len(quote_errors), "error": str(exc)}

    day = now.date().isoformat()
    slots = list((state.setdefault("captured_slots", {})).get(day) or [])
    if slot not in slots:
        slots.append(slot)
    state["captured_slots"][day] = slots
    summaries = list(state.get("slot_summaries") or [])
    summaries.append(slot_summary)
    state["slot_summaries"] = summaries[-500:]
    _update_symbol_stats(state, broad_summaries, earnings_symbols)

    unique_contracts = {}
    for snap in broad_snaps + deep_snaps:
        key = snap.get("tradingsymbol")
        if key:
            unique_contracts[key] = snap
    state["quote_contracts"] = int(state.get("quote_contracts") or 0) + len(unique_contracts)
    state["stale_contracts"] = int(state.get("stale_contracts") or 0) + sum(snap.get("stale") is True for snap in unique_contracts.values())
    state["final_week_samples"] = int(state.get("final_week_samples") or 0) + sum(
        1 for summary in broad_summaries.values()
        if (summary.get("primary") or {}).get("two_sided") and _finite((summary.get("primary") or {}).get("dte")) and float((summary.get("primary") or {}).get("dte")) <= 5
    )
    state["last_capture_at"] = now.isoformat(timespec="seconds")
    state["last_capture_status"] = capture_status
    state["last_error"] = quote_errors[-1]["error"] if quote_errors else None
    _save_v12_state(state_file, state)
    return {
        "status": capture_status,
        "slot": slot,
        "quote_errors": len(quote_errors),
        "broad_symbols": len(broad_summaries),
        "deep_symbols": len(deep_symbols),
    }
