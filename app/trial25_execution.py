"""Pure Trial-25 option structure, execution-quality and charge helpers."""
from __future__ import annotations

import datetime as dt
import math


FEE_MODEL_VERSION = "ZERODHA_NSE_EQ_OPT_2026_04_V1"
MAX_QUOTE_LATENCY_SECONDS = 15.0
BROKERAGE_PER_ORDER = 20.0
STT_SELL_RATE = 0.0015
NSE_TXN_RATE = 0.0003553
SEBI_RATE = 10.0 / 10_000_000.0
STAMP_BUY_RATE = 0.00003
GST_RATE = 0.18


def _finite(value) -> bool:
    try:
        return value is not None and math.isfinite(float(value))
    except (TypeError, ValueError):
        return False


def _as_date(value) -> dt.date | None:
    if isinstance(value, dt.datetime):
        return value.date()
    if isinstance(value, dt.date):
        return value
    try:
        return dt.date.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None


def _as_dt(value) -> dt.datetime | None:
    if isinstance(value, dt.datetime):
        return value
    if value is None:
        return None
    try:
        return dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None


def _age_seconds(value, now: dt.datetime) -> float | None:
    parsed = _as_dt(value)
    if parsed is None:
        return None
    if parsed.tzinfo is not None and now.tzinfo is None:
        parsed = parsed.replace(tzinfo=None)
    elif parsed.tzinfo is None and now.tzinfo is not None:
        parsed = parsed.replace(tzinfo=now.tzinfo)
    return max(0.0, (now - parsed).total_seconds())


def _identity(row: dict) -> dict:
    return {
        "tradingsymbol": row.get("tradingsymbol"),
        "instrument_token": row.get("instrument_token"),
        "type": row.get("instrument_type"),
        "strike": float(row.get("strike")),
        "expiry": _as_date(row.get("expiry")).isoformat(),
        "lot_size": int(row.get("lot_size") or 0),
    }


def select_structure(
    contracts: list[dict],
    spot: float,
    entry_date: dt.date,
    exit_date: dt.date,
    *,
    implied_move_points: float,
) -> dict:
    """Select the fixed four contracts without relaxing any wing boundary."""
    if not _finite(spot) or float(spot) <= 0 or not _finite(implied_move_points) or float(implied_move_points) <= 0:
        return {"status": "UNAVAILABLE_ATM_CONTRACT"}
    live = []
    for row in contracts or []:
        expiry = _as_date(row.get("expiry"))
        typ = row.get("instrument_type")
        if typ not in ("CE", "PE") or expiry is None or not _finite(row.get("strike")):
            continue
        if expiry <= exit_date:
            continue
        if (expiry - entry_date).days < 5:
            continue
        live.append((expiry, row))
    expiries = sorted({expiry for expiry, _ in live})
    if not expiries:
        return {"status": "UNAVAILABLE_EXPIRY"}
    expiry = expiries[0]
    rows = [row for exp, row in live if exp == expiry]
    strikes = sorted({float(row["strike"]) for row in rows})
    if not strikes:
        return {"status": "UNAVAILABLE_ATM_CONTRACT"}
    atm = min(strikes, key=lambda strike: (abs(strike - float(spot)), strike))

    def exact(typ, strike):
        return next((
            row for row in rows
            if row.get("instrument_type") == typ and float(row.get("strike")) == float(strike)
        ), None)

    atm_call = exact("CE", atm)
    atm_put = exact("PE", atm)
    if atm_call is None or atm_put is None:
        return {"status": "UNAVAILABLE_ATM_CONTRACT"}

    move = float(implied_move_points)
    lower_candidates = [strike for strike in strikes if strike <= atm - 2.0 * move]
    upper_candidates = [strike for strike in strikes if strike >= atm + 2.0 * move]
    if not lower_candidates or not upper_candidates:
        return {"status": "UNAVAILABLE_WING_CONTRACT"}
    lower_strike = max(lower_candidates)
    upper_strike = min(upper_candidates)
    lower_put = exact("PE", lower_strike)
    upper_call = exact("CE", upper_strike)
    if lower_put is None or upper_call is None:
        return {"status": "UNAVAILABLE_WING_CONTRACT"}

    identities = {
        "atm_call": _identity(atm_call),
        "atm_put": _identity(atm_put),
        "lower_put": _identity(lower_put),
        "upper_call": _identity(upper_call),
    }
    return {
        "status": "OK",
        "expiry": expiry.isoformat(),
        "atm_strike": float(atm),
        "implied_move_points": move,
        "atm_call": dict(atm_call),
        "atm_put": dict(atm_put),
        "lower_put": dict(lower_put),
        "upper_call": dict(upper_call),
        "contract_identities": identities,
        "lot_size": int(atm_call.get("lot_size") or atm_put.get("lot_size") or 0),
    }


def _top(rows) -> tuple[float | None, int]:
    rows = list(rows or [])
    if not rows:
        return None, 0
    row = rows[0] or {}
    price = float(row["price"]) if _finite(row.get("price")) and float(row["price"]) > 0 else None
    return price, int(row.get("quantity") or 0)


def normalize_live_quote(
    contract: dict,
    quote: dict | None,
    requested_at: dt.datetime,
    received_at: dt.datetime,
) -> dict:
    quote = quote or {}
    depth = quote.get("depth") or {}
    bid, bid_qty = _top(depth.get("buy"))
    ask, ask_qty = _top(depth.get("sell"))
    two_sided = bool(bid is not None and ask is not None and ask >= bid)
    latency_ms = max(0.0, (received_at - requested_at).total_seconds() * 1000.0)
    quote_age = _age_seconds(quote.get("timestamp"), received_at)
    last_trade_age = _age_seconds(quote.get("last_trade_time"), received_at)
    return {
        **_identity(contract),
        "quote_request_at": requested_at.isoformat(timespec="milliseconds"),
        "quote_received_at": received_at.isoformat(timespec="milliseconds"),
        "api_latency_ms": round(latency_ms, 3),
        "quote_timestamp": str(quote.get("timestamp")) if quote.get("timestamp") is not None else None,
        "last_trade_time": str(quote.get("last_trade_time")) if quote.get("last_trade_time") is not None else None,
        "quote_timestamp_age_s": round(quote_age, 3) if quote_age is not None else None,
        "last_trade_age_s": round(last_trade_age, 3) if last_trade_age is not None else None,
        "last_trade_stale_600s": bool(last_trade_age is not None and last_trade_age > 600.0),
        "best_bid": bid,
        "best_bid_qty": bid_qty,
        "best_ask": ask,
        "best_ask_qty": ask_qty,
        "two_sided": two_sided,
    }


def validate_leg(side: str, snap: dict, lot_size: int) -> tuple[bool, str | None]:
    if float(snap.get("api_latency_ms") or 0.0) > MAX_QUOTE_LATENCY_SECONDS * 1000.0:
        return False, "QUOTE_LATENCY"
    if not bool(snap.get("two_sided")):
        return False, "ONE_SIDED_BOOK"
    side = str(side).upper()
    if side not in ("BUY", "SELL"):
        return False, "INVALID_SIDE"
    qty = snap.get("best_bid_qty") if side == "SELL" else snap.get("best_ask_qty")
    if int(qty or 0) < int(lot_size or 0):
        return False, "INSUFFICIENT_TOP_QTY"
    price = snap.get("best_bid") if side == "SELL" else snap.get("best_ask")
    if not _finite(price) or float(price) <= 0:
        return False, "INVALID_EXECUTION_PRICE"
    return True, None


def calculate_option_charges(fills: list[dict]) -> dict:
    """Calculate one version-stamped NSE equity-option fee basket."""
    normalized = []
    for fill in fills or []:
        side = str(fill.get("side") or "").upper()
        price = float(fill.get("price") or 0.0)
        quantity = int(fill.get("quantity") or 0)
        if side not in ("BUY", "SELL") or price <= 0 or quantity <= 0:
            raise ValueError("fills require BUY/SELL, positive price and positive quantity")
        normalized.append((side, price * quantity))
    turnover = sum(value for _, value in normalized)
    sell_turnover = sum(value for side, value in normalized if side == "SELL")
    buy_turnover = sum(value for side, value in normalized if side == "BUY")
    brokerage = BROKERAGE_PER_ORDER * len(normalized)
    stt = sell_turnover * STT_SELL_RATE
    exchange = turnover * NSE_TXN_RATE
    sebi = turnover * SEBI_RATE
    stamp = buy_turnover * STAMP_BUY_RATE
    gst = (brokerage + exchange + sebi) * GST_RATE
    total = brokerage + stt + exchange + sebi + stamp + gst
    return {
        "model_version": FEE_MODEL_VERSION,
        "turnover": round(turnover, 8),
        "brokerage": round(brokerage, 8),
        "stt": round(stt, 8),
        "exchange_transaction_charge": round(exchange, 8),
        "sebi_fee": round(sebi, 8),
        "stamp_duty": round(stamp, 8),
        "gst": round(gst, 8),
        "total": round(total, 8),
    }
