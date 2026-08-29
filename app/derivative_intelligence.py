"""Live derivative-intelligence layer for V8.2.

This module does **not** pretend that stock direction and option P&L are the
same problem.  It is applied only after V8.1 has shortlisted an underlying.
It then asks whether a liquid near-ATM option is sensibly priced for buying,
looks premium-rich, or is too expensive/illiquid to express the stock view.

Kite does not expose historical point-in-time option chains or signed trade
aggressor data through the normal instrument/quote endpoints.  Consequently
all option-chain readings here are LIVE/SHADOW evidence; they are never mixed
into the historical underlying backtest as if they had existed there.
"""
from __future__ import annotations

import datetime as dt
import json
import math
import os
import threading
from statistics import median

RISK_FREE_RATE = float(os.getenv("OPTION_RISK_FREE_RATE", "0.06"))
MAX_DTE = int(os.getenv("OPTION_MAX_DTE", "45"))
SHADOW_FILE = os.getenv("OPTION_SHADOW_FILE", "option_shadow.jsonl")
SHADOW_STATE_FILE = os.getenv("OPTION_SHADOW_STATE_FILE", "option_shadow_state.json")

_cache = {"date": None, "map": {}}
_shadow_lock = threading.Lock()


def _finite(v):
    try:
        return v is not None and math.isfinite(float(v))
    except (TypeError, ValueError):
        return False


def _f(v, default=None):
    return float(v) if _finite(v) else default


def _norm_cdf(x):
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _norm_pdf(x):
    return math.exp(-0.5 * x * x) / math.sqrt(2.0 * math.pi)


def black_scholes_price(spot, strike, t, rate, vol, option_type):
    spot, strike, t, rate, vol = map(float, (spot, strike, t, rate, vol))
    if spot <= 0 or strike <= 0 or t <= 0 or vol <= 0:
        return None
    d1 = (math.log(spot / strike) + (rate + 0.5 * vol * vol) * t) / (vol * math.sqrt(t))
    d2 = d1 - vol * math.sqrt(t)
    disc = math.exp(-rate * t)
    if option_type == "CE":
        return spot * _norm_cdf(d1) - strike * disc * _norm_cdf(d2)
    return strike * disc * _norm_cdf(-d2) - spot * _norm_cdf(-d1)


def implied_volatility(price, spot, strike, t, rate, option_type):
    """Bisection IV. Returns decimal volatility, or None for invalid quotes."""
    price = _f(price)
    if price is None or price <= 0 or spot <= 0 or strike <= 0 or t <= 0:
        return None
    intrinsic = max(0.0, spot - strike) if option_type == "CE" else max(0.0, strike - spot)
    if price < intrinsic - 1e-9:
        return None
    lo, hi = 0.01, 5.0
    plo = black_scholes_price(spot, strike, t, rate, lo, option_type)
    phi = black_scholes_price(spot, strike, t, rate, hi, option_type)
    if plo is None or phi is None or price < plo - 1e-6 or price > phi + 1e-6:
        return None
    for _ in range(80):
        mid = (lo + hi) / 2.0
        pmid = black_scholes_price(spot, strike, t, rate, mid, option_type)
        if pmid is None:
            return None
        if pmid > price:
            hi = mid
        else:
            lo = mid
    return (lo + hi) / 2.0


def option_greeks(spot, strike, t, rate, vol, option_type):
    if not all(_finite(x) for x in (spot, strike, t, rate, vol)) or min(spot, strike, t, vol) <= 0:
        return {}
    s, k, t, r, v = map(float, (spot, strike, t, rate, vol))
    d1 = (math.log(s / k) + (r + 0.5 * v * v) * t) / (v * math.sqrt(t))
    d2 = d1 - v * math.sqrt(t)
    pdf = _norm_pdf(d1)
    disc = math.exp(-r * t)
    if option_type == "CE":
        delta = _norm_cdf(d1)
        theta = (-s * pdf * v / (2 * math.sqrt(t)) - r * k * disc * _norm_cdf(d2)) / 365.0
    else:
        delta = _norm_cdf(d1) - 1.0
        theta = (-s * pdf * v / (2 * math.sqrt(t)) + r * k * disc * _norm_cdf(-d2)) / 365.0
    gamma = pdf / (s * v * math.sqrt(t))
    vega = s * pdf * math.sqrt(t) / 100.0
    return {"delta": delta, "gamma": gamma, "theta_per_day": theta, "vega": vega}


def _best_bid_ask(q):
    depth = (q or {}).get("depth") or {}
    buy, sell = depth.get("buy") or [], depth.get("sell") or []
    bid = _f((buy[0] or {}).get("price")) if buy else None
    ask = _f((sell[0] or {}).get("price")) if sell else None
    return bid, ask


def _mid_and_spread(q):
    bid, ask = _best_bid_ask(q)
    last = _f((q or {}).get("last_price"))
    if bid is not None and ask is not None and bid > 0 and ask > 0 and ask >= bid:
        mid = (bid + ask) / 2.0
        spread = (ask - bid) / mid * 100.0 if mid > 0 else None
        return mid, spread, bid, ask
    return last, None, bid, ask


def _contract_snapshot(contract, q, spot, now):
    expiry = contract.get("expiry")
    if isinstance(expiry, dt.datetime):
        expiry = expiry.date()
    if not isinstance(expiry, dt.date):
        return None
    # Treat expiry as end-of-session; minimum positive fraction avoids a 0-DTE divide-by-zero.
    expiry_dt = dt.datetime.combine(expiry, dt.time(15, 30))
    t_days = max((expiry_dt - now).total_seconds() / 86400.0, 1.0 / 24.0)
    t = t_days / 365.0
    mid, spread, bid, ask = _mid_and_spread(q)
    strike = _f(contract.get("strike"))
    typ = contract.get("instrument_type")
    iv = implied_volatility(mid, spot, strike, t, RISK_FREE_RATE, typ) if mid and strike and typ in ("CE", "PE") else None
    greeks = option_greeks(spot, strike, t, RISK_FREE_RATE, iv, typ) if iv else {}
    return {
        "symbol": contract.get("tradingsymbol"), "type": typ, "strike": strike,
        "expiry": expiry.isoformat(), "dte": max(0, (expiry - now.date()).days),
        "mid": round(mid, 4) if mid is not None else None,
        "bid": bid, "ask": ask,
        "spread_pct": round(spread, 3) if spread is not None else None,
        "iv_pct": round(iv * 100.0, 2) if iv else None,
        "delta": round(greeks.get("delta"), 4) if _finite(greeks.get("delta")) else None,
        "gamma": round(greeks.get("gamma"), 6) if _finite(greeks.get("gamma")) else None,
        "theta_per_day": round(greeks.get("theta_per_day"), 4) if _finite(greeks.get("theta_per_day")) else None,
        "vega": round(greeks.get("vega"), 4) if _finite(greeks.get("vega")) else None,
        "volume": (q or {}).get("volume"), "oi": (q or {}).get("oi"),
    }


def analyze_option_quotes(symbol, direction, spot, contracts, quotes, *, now=None, min_dte=0):
    now = now or dt.datetime.now()
    live = [c for c in (contracts or []) if c.get("instrument_type") in ("CE", "PE") and c.get("expiry")]
    if not live or not _finite(spot):
        return {"status": "NO OPTION DATA", "directional": None}
    today = now.date()
    expiries = sorted({
        c["expiry"] for c in live
        if c["expiry"] >= today
        and int(min_dte) <= (c["expiry"] - today).days <= MAX_DTE
    })
    if not expiries:
        return {"status": "NO LIVE EXPIRY", "directional": None}
    expiry = expiries[0]
    exp_contracts = [c for c in live if c["expiry"] == expiry]
    strikes = sorted({float(c.get("strike")) for c in exp_contracts if _finite(c.get("strike"))})
    if not strikes:
        return {"status": "NO STRIKES", "directional": None}
    atm = min(strikes, key=lambda k: abs(k - float(spot)))
    # Analyze at-the-money plus the nearest strike on either side. This avoids lottery OTM selection.
    atm_i = strikes.index(atm)
    chosen_strikes = set(strikes[max(0, atm_i - 1): min(len(strikes), atm_i + 2)])
    snaps = []
    for c in exp_contracts:
        if float(c.get("strike") or -1) not in chosen_strikes:
            continue
        q = quotes.get(f"NFO:{c.get('tradingsymbol')}", {})
        snap = _contract_snapshot(c, q, float(spot), now)
        if snap and snap.get("mid"):
            snaps.append(snap)
    if not snaps:
        return {"status": "NO QUOTED OPTIONS", "directional": None}
    ce_atm = min((x for x in snaps if x["type"] == "CE"), key=lambda x: abs(x["strike"] - atm), default=None)
    pe_atm = min((x for x in snaps if x["type"] == "PE"), key=lambda x: abs(x["strike"] - atm), default=None)
    ivs = [x["iv_pct"] for x in (ce_atm, pe_atm) if x and _finite(x.get("iv_pct"))]
    atm_iv = median(ivs) if ivs else None
    straddle_move = None
    if ce_atm and pe_atm and _finite(ce_atm.get("mid")) and _finite(pe_atm.get("mid")):
        straddle_move = (ce_atm["mid"] + pe_atm["mid"]) / float(spot) * 100.0
    wanted = "CE" if direction == "Bullish" else "PE"
    dir_snaps = [x for x in snaps if x["type"] == wanted]
    # Prefer liquid near-ATM, then tighter spread; do not chase far OTM contracts.
    directional = min(
        dir_snaps,
        key=lambda x: (abs(x["strike"] - float(spot)), x["spread_pct"] if _finite(x.get("spread_pct")) else 999.0),
        default=None,
    )
    call_put_iv_spread = None
    if ce_atm and pe_atm and _finite(ce_atm.get("iv_pct")) and _finite(pe_atm.get("iv_pct")):
        call_put_iv_spread = ce_atm["iv_pct"] - pe_atm["iv_pct"]

    atm_volume_pcr = None
    atm_oi_pcr = None
    if ce_atm and pe_atm:
        cv, pv = _f(ce_atm.get("volume")), _f(pe_atm.get("volume"))
        co, po = _f(ce_atm.get("oi")), _f(pe_atm.get("oi"))
        if cv is not None and cv > 0 and pv is not None:
            atm_volume_pcr = pv / cv
        if co is not None and co > 0 and po is not None:
            atm_oi_pcr = po / co

    # 25-delta-ish skew proxy from the quoted neighborhood.  This is context
    # only: total option volume/OI is unsigned and must never be mistaken for
    # buyer-initiated opening flow.
    put_candidates = [x for x in snaps if x.get("type") == "PE" and _finite(x.get("iv_pct")) and _finite(x.get("delta")) and x.get("strike", 0) <= float(spot)]
    call_candidates = [x for x in snaps if x.get("type") == "CE" and _finite(x.get("iv_pct")) and _finite(x.get("delta")) and x.get("strike", 0) >= float(spot)]
    put25 = min(put_candidates, key=lambda x: abs(abs(float(x["delta"])) - 0.25), default=None)
    call25 = min(call_candidates, key=lambda x: abs(abs(float(x["delta"])) - 0.25), default=None)
    put_skew = (float(put25["iv_pct"]) - float(atm_iv)) if put25 and atm_iv is not None else None
    call_skew = (float(call25["iv_pct"]) - float(atm_iv)) if call25 and atm_iv is not None else None

    return {
        "status": "OK", "expiry": expiry.isoformat(), "dte": max(0, (expiry - today).days),
        "atm_strike": atm, "atm_iv_pct": round(atm_iv, 2) if atm_iv is not None else None,
        "straddle_move_pct": round(straddle_move, 2) if straddle_move is not None else None,
        "call_put_iv_spread_pct": round(call_put_iv_spread, 2) if call_put_iv_spread is not None else None,
        "atm_volume_pcr": round(atm_volume_pcr, 3) if atm_volume_pcr is not None else None,
        "atm_oi_pcr": round(atm_oi_pcr, 3) if atm_oi_pcr is not None else None,
        "put_skew_pct": round(put_skew, 2) if put_skew is not None else None,
        "call_skew_pct": round(call_skew, 2) if call_skew is not None else None,
        "directional": directional, "contracts": snaps,
    }


def _liquidity_score(contract):
    if not contract:
        return None
    sp = _f(contract.get("spread_pct"))
    vol = _f(contract.get("volume"), 0.0)
    oi = _f(contract.get("oi"), 0.0)
    spread_score = 100.0 if sp is not None and sp <= 1 else 80.0 if sp is not None and sp <= 2 else 60.0 if sp is not None and sp <= 4 else 30.0 if sp is not None else 40.0
    activity = 100.0 if vol >= 5000 and oi >= 10000 else 80.0 if vol >= 1000 and oi >= 5000 else 60.0 if vol >= 250 and oi >= 1000 else 35.0
    return (spread_score + activity) / 2.0


def classify_option_expression(row, chain, *, horizon="intraday"):
    """Decide how (or whether) to express a V8 underlying signal in options.

    This is intentionally an expression layer, not a new underlying gate.
    """
    directional = (chain or {}).get("directional")
    if not directional:
        return {"option_action": "OPTION DATA INSUFFICIENT", "option_edge": "NONE", "buyer_score": None}
    if horizon == "swing":
        alpha = _f(row.get("v8_swing_alpha"), _f(row.get("v8_decision_score", row.get("v8_alpha")), 50.0))
        state = row.get("v8_swing_state")
    else:
        alpha = _f(row.get("v8_decision_score", row.get("v8_alpha")), 50.0)
        state = row.get("v8_state")
    participation = _f(row.get("v8_participation"), 50.0)
    rv = _f(row.get("realized_vol_20d"))
    iv = _f(directional.get("iv_pct"), _f((chain or {}).get("atm_iv_pct")))
    ratio = iv / rv if iv is not None and rv is not None and rv > 0 else None
    liquidity = _liquidity_score(directional)
    dte = int((chain or {}).get("dte") or 0)
    delta = abs(_f(directional.get("delta"), 0.0))

    iv_value = 50.0
    if ratio is not None:
        iv_value = 90.0 if ratio <= 1.0 else 75.0 if ratio <= 1.2 else 55.0 if ratio <= 1.5 else 25.0
    min_hold_dte = 3 if horizon == "swing" else 0
    gamma_eff = 85.0 if 0.40 <= delta <= 0.65 and dte >= min_hold_dte else 65.0 if 0.30 <= delta <= 0.75 and dte >= min_hold_dte else 40.0
    buyer_score = median([alpha, participation, liquidity or 40.0, iv_value, gamma_eff])

    spread = _f(directional.get("spread_pct"))
    expensive = ratio is not None and ratio > 1.5
    illiquid = spread is not None and spread > 5.0
    strong_underlying = state == "TRADE CANDIDATE" or alpha >= 80
    if strong_underlying and (expensive or illiquid):
        action, edge = "UNDERLYING GOOD - OPTION EXPENSIVE", "LOW"
    elif strong_underlying and buyer_score >= 70 and not illiquid:
        action, edge = "OPTION BUYER EDGE", "HIGH"
    elif strong_underlying and buyer_score >= 55 and not illiquid:
        action, edge = "OPTION BUYER EDGE", "MEDIUM"
    elif ratio is not None and ratio >= 1.35 and participation < 70:
        action, edge = "PREMIUM RICH - DEFINED-RISK SELLING BIAS", "MEDIUM"
    else:
        action, edge = "UNDERLYING ONLY / WAIT", "LOW"
    reasons = []
    if ratio is not None:
        reasons.append(f"IV/RV {ratio:.2f}x")
    if spread is not None:
        reasons.append(f"Spread {spread:.1f}%")
    if _finite(chain.get("straddle_move_pct")):
        reasons.append(f"ATM straddle {chain['straddle_move_pct']:.1f}% to expiry")
    reasons.append(f"DTE {dte}")
    return {
        "option_action": action, "option_edge": edge, "buyer_score": round(buyer_score, 1),
        "iv_rv_ratio": round(ratio, 2) if ratio is not None else None,
        "liquidity_score": round(liquidity, 1) if liquidity is not None else None,
        "contract": directional, "atm_iv_pct": chain.get("atm_iv_pct"),
        "straddle_move_pct": chain.get("straddle_move_pct"),
        "call_put_iv_spread_pct": chain.get("call_put_iv_spread_pct"),
        "atm_volume_pcr": chain.get("atm_volume_pcr"),
        "atm_oi_pcr": chain.get("atm_oi_pcr"),
        "put_skew_pct": chain.get("put_skew_pct"),
        "call_skew_pct": chain.get("call_skew_pct"),
        "horizon": horizon,
        "dte": dte, "reasons": reasons,
        "historical_validation": "LIVE SHADOW ONLY - no point-in-time historical option chain in Kite",
    }


def _option_contracts_map(kite):
    today = dt.date.today()
    key = today.isoformat()
    if _cache["date"] == key and _cache["map"]:
        return _cache["map"]
    grouped = {}
    for r in kite.instruments("NFO"):
        if r.get("instrument_type") not in ("CE", "PE"):
            continue
        name = (r.get("name") or "").strip()
        expiry = r.get("expiry")
        if not name or not expiry or expiry < today:
            continue
        grouped.setdefault(name, []).append({
            "tradingsymbol": r.get("tradingsymbol"), "instrument_token": r.get("instrument_token"),
            "instrument_type": r.get("instrument_type"), "strike": r.get("strike"), "expiry": expiry,
            "lot_size": r.get("lot_size"),
        })
    _cache["date"] = key
    _cache["map"] = grouped
    return grouped


def enrich_shortlisted_options(kite, rows, *, now=None, max_candidates=6):
    """Attach live option intelligence to only the strongest V8 candidates."""
    now = now or dt.datetime.now()
    pool = [r for r in (rows or []) if not r.get("error") and r.get("v8_direction") in ("Bullish", "Bearish") and r.get("v8_state") in ("TRADE CANDIDATE", "WATCH")]
    # Preserve the user's two-sided objective: option API budget is split across
    # bullish and bearish leaders instead of letting one side consume all slots.
    per_side = max(1, int(max_candidates) // 2)
    candidates = []
    for direction in ("Bullish", "Bearish"):
        side = [r for r in pool if r.get("v8_direction") == direction]
        side.sort(key=lambda r: (1 if r.get("v8_state") == "TRADE CANDIDATE" else 0, _f(r.get("v8_decision_score"), -1)), reverse=True)
        candidates.extend(side[:per_side])
    if not candidates:
        return rows
    cmap = _option_contracts_map(kite)
    request = []
    selected_contracts = {}
    for r in candidates:
        symbol = r.get("symbol")
        spot = _f(r.get("close"))
        contracts = cmap.get(symbol) or []
        if not contracts or spot is None:
            continue
        today = now.date()
        expiries = sorted({c["expiry"] for c in contracts if c["expiry"] >= today and (c["expiry"] - today).days <= MAX_DTE})
        if not expiries:
            continue
        subset = []
        for exp in expiries[:2]:
            expc = [c for c in contracts if c["expiry"] == exp]
            strikes = sorted({float(c["strike"]) for c in expc if _finite(c.get("strike"))})
            if not strikes:
                continue
            atm = min(strikes, key=lambda x: abs(x - spot)); idx = strikes.index(atm)
            keep = set(strikes[max(0, idx-3): min(len(strikes), idx+4)])
            subset.extend(c for c in expc if float(c.get("strike") or -1) in keep)
        selected_contracts[symbol] = subset
        request.extend(f"NFO:{c['tradingsymbol']}" for c in subset if c.get("tradingsymbol"))
    quotes = {}
    for i in range(0, len(request), 400):
        try:
            quotes.update(kite.quote(request[i:i+400]))
        except Exception:
            continue
    for r in candidates:
        symbol = r.get("symbol")
        contracts = selected_contracts.get(symbol, [])
        chain = analyze_option_quotes(symbol, r.get("v8_direction"), _f(r.get("close"), 0), contracts, quotes, now=now, min_dte=0)
        swing_chain = analyze_option_quotes(symbol, r.get("v8_direction"), _f(r.get("close"), 0), contracts, quotes, now=now, min_dte=3)
        intel = classify_option_expression(r, chain, horizon="intraday")
        swing_intel = classify_option_expression(r, swing_chain, horizon="swing")
        r["option_intelligence"] = intel
        r["option_swing_intelligence"] = swing_intel
        r["option_action"] = intel.get("option_action")
        r["option_edge"] = intel.get("option_edge")
        r["option_buyer_score"] = intel.get("buyer_score")
        r["option_contract"] = (intel.get("contract") or {}).get("symbol")
        r["option_iv_pct"] = (intel.get("contract") or {}).get("iv_pct")
        r["option_spread_pct"] = (intel.get("contract") or {}).get("spread_pct")
        r["option_delta"] = (intel.get("contract") or {}).get("delta")
        r["option_theta_day"] = (intel.get("contract") or {}).get("theta_per_day")
        r["option_atm_iv_pct"] = intel.get("atm_iv_pct")
        r["option_straddle_move_pct"] = intel.get("straddle_move_pct")
        r["option_iv_rv_ratio"] = intel.get("iv_rv_ratio")
        r["option_dte"] = intel.get("dte")
        r["option_atm_volume_pcr"] = intel.get("atm_volume_pcr")
        r["option_atm_oi_pcr"] = intel.get("atm_oi_pcr")
        r["option_put_skew_pct"] = intel.get("put_skew_pct")

        r["option_swing_action"] = swing_intel.get("option_action")
        r["option_swing_edge"] = swing_intel.get("option_edge")
        r["option_swing_buyer_score"] = swing_intel.get("buyer_score")
        r["option_swing_contract"] = (swing_intel.get("contract") or {}).get("symbol")
        r["option_swing_iv_pct"] = (swing_intel.get("contract") or {}).get("iv_pct")
        r["option_swing_spread_pct"] = (swing_intel.get("contract") or {}).get("spread_pct")
        r["option_swing_delta"] = (swing_intel.get("contract") or {}).get("delta")
        r["option_swing_theta_day"] = (swing_intel.get("contract") or {}).get("theta_per_day")
        r["option_swing_iv_rv_ratio"] = swing_intel.get("iv_rv_ratio")
        r["option_swing_dte"] = swing_intel.get("dte")
        r["option_swing_straddle_move_pct"] = swing_intel.get("straddle_move_pct")
        record_shadow_snapshot(r, now=now)
    return rows


def record_shadow_snapshot(row, *, now=None):
    """Append live option evidence for future walk-forward validation."""
    intel = row.get("option_intelligence") or {}
    if not intel or not row.get("symbol"):
        return
    record = {
        "ts": (now or dt.datetime.now()).isoformat(timespec="seconds"),
        "symbol": row.get("symbol"), "direction": row.get("v8_direction"),
        "underlying_close": row.get("close"), "v8_score": row.get("v8_decision_score"),
        "v8_state": row.get("v8_state"), "participation": row.get("v8_participation"),
        "realized_vol_20d": row.get("realized_vol_20d"),
        "option": intel,
    }
    try:
        with _shadow_lock:
            with open(SHADOW_FILE, "a", encoding="utf-8") as f:
                f.write(json.dumps(record, default=str, separators=(",", ":")) + "\n")
    except OSError:
        pass
    register_shadow_signal(row, now=now)
    if row.get("option_swing_intelligence"):
        register_shadow_signal(
            row, now=now, intel_key="option_swing_intelligence", signal_kind="swing",
            state_key="v8_swing_state", score_key="v8_swing_alpha",
        )


def load_shadow_state():
    """Load forward option-signal state. Corrupt/missing files degrade to empty."""
    try:
        with open(SHADOW_STATE_FILE, "r", encoding="utf-8") as f:
            state = json.load(f)
        if isinstance(state, dict) and isinstance(state.get("signals"), list):
            return state
    except (OSError, ValueError, TypeError):
        pass
    return {"signals": []}


def _save_shadow_state(state):
    tmp = SHADOW_STATE_FILE + ".tmp"
    try:
        with _shadow_lock:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(state, f, default=str, separators=(",", ":"))
            os.replace(tmp, SHADOW_STATE_FILE)
    except OSError:
        try:
            if os.path.exists(tmp):
                os.unlink(tmp)
        except OSError:
            pass


def _signal_timestamp(row, now):
    raw = row.get("timestamp") or row.get("signal_time") or row.get("entry_time")
    if isinstance(raw, dt.datetime):
        return raw
    if raw:
        try:
            return dt.datetime.fromisoformat(str(raw).replace("Z", "+00:00")).replace(tzinfo=None)
        except ValueError:
            pass
    # Dedup fallback: the current closed 15-minute bucket.
    minute = (now.minute // 15) * 15
    return now.replace(minute=minute, second=0, microsecond=0)


def register_shadow_signal(row, *, now=None, intel_key="option_intelligence", signal_kind="intraday", state_key="v8_state", score_key="v8_decision_score"):
    """Register actual long-option entry evidence for future premium outcomes.

    Only rows with a concrete quoted contract are registered.  We keep all
    expression labels (not only HIGH buyer edge) so future data can tell us
    whether the labels were actually useful instead of self-selecting winners.
    """
    now = now or dt.datetime.now()
    intel = row.get(intel_key) or {}
    contract = intel.get("contract") or {}
    symbol = contract.get("symbol")
    entry_mid = _f(contract.get("mid"))
    if not row.get("symbol") or not symbol or entry_mid is None or entry_mid <= 0:
        return None
    signal_ts = _signal_timestamp(row, now)
    sid = "|".join([
        str(row.get("symbol")), str(row.get("v8_direction")), str(signal_kind), signal_ts.isoformat(timespec="minutes"), str(symbol)
    ])
    state = load_shadow_state()
    if any(x.get("id") == sid for x in state["signals"]):
        return sid
    state["signals"].append({
        "id": sid,
        "symbol": row.get("symbol"),
        "direction": row.get("v8_direction"),
        "signal_ts": signal_ts.isoformat(timespec="seconds"),
        "underlying_entry": _f(row.get("close")),
        "signal_kind": signal_kind,
        "v8_score": _f(row.get(score_key, row.get("v8_alpha"))),
        "v8_state": row.get(state_key),
        "participation": _f(row.get("v8_participation")),
        "option_action": intel.get("option_action"),
        "option_edge": intel.get("option_edge"),
        "contract": symbol,
        "option_type": contract.get("type"),
        "strike": contract.get("strike"),
        "expiry": contract.get("expiry"),
        "entry_mid": entry_mid,
        "entry_iv_pct": _f(contract.get("iv_pct")),
        "entry_spread_pct": _f(contract.get("spread_pct")),
        "entry_delta": _f(contract.get("delta")),
        "entry_theta_day": _f(contract.get("theta_per_day")),
        "entry_iv_rv_ratio": _f(intel.get("iv_rv_ratio")),
        "outcomes": {},
    })
    # Keep a bounded history in the container state file; JSONL remains the raw archive.
    if len(state["signals"]) > 5000:
        state["signals"] = state["signals"][-5000:]
    _save_shadow_state(state)
    return sid


def _due_horizons(signal, now):
    try:
        start = dt.datetime.fromisoformat(signal["signal_ts"])
    except (KeyError, ValueError, TypeError):
        return []
    outcomes = signal.get("outcomes") or {}
    due = []
    # Intraday marks must come from the same trading date. If a late-day
    # signal's 30m/2h horizon extends past the close, we leave that horizon
    # unmeasured rather than silently substituting the next day's gap quote.
    if now.date() == start.date():
        if "30m" not in outcomes and now >= start + dt.timedelta(minutes=30):
            due.append("30m")
        if "2h" not in outcomes and now >= start + dt.timedelta(hours=2):
            due.append("2h")
    # EOD is the first usable mark after 15:15 on the signal date.
    if "EOD" not in outcomes and now.date() == start.date() and now.time() >= dt.time(15, 15):
        due.append("EOD")
    # 1D means next trading session at/after the original signal clock time.
    # Weekend/holiday gaps naturally wait until the scanner next runs.
    if "1D" not in outcomes and now.date() > start.date() and now.time() >= start.time():
        due.append("1D")
    return due


def resolve_shadow_outcomes(kite, *, now=None):
    """Mark real option-premium outcomes for previously registered live signals."""
    now = now or dt.datetime.now()
    state = load_shadow_state()
    due = []
    for sig in state["signals"]:
        hs = _due_horizons(sig, now)
        if hs and sig.get("contract") and _finite(sig.get("entry_mid")):
            due.append((sig, hs))
    if not due:
        return state
    keys = sorted({f"NFO:{sig['contract']}" for sig, _ in due})
    quotes = {}
    for i in range(0, len(keys), 400):
        try:
            quotes.update(kite.quote(keys[i:i+400]))
        except Exception:
            continue
    changed = False
    for sig, horizons in due:
        q = quotes.get(f"NFO:{sig['contract']}") or {}
        mid, spread, _bid, _ask = _mid_and_spread(q)
        entry = _f(sig.get("entry_mid"))
        if mid is None or entry is None or entry <= 0:
            continue
        ret = round((mid / entry - 1.0) * 100.0, 3)
        sig.setdefault("outcomes", {})
        for h in horizons:
            sig["outcomes"][h] = {
                "ts": now.isoformat(timespec="seconds"),
                "mid": round(mid, 4),
                "premium_return_pct": ret,
                "spread_pct": round(spread, 3) if spread is not None else None,
            }
            changed = True
    if changed:
        _save_shadow_state(state)
    return state


def get_shadow_stats(kind="intraday"):
    """Aggregate forward option-buyer results by horizon; no backfill or look-ahead."""
    state = load_shadow_state()
    out = {}
    for horizon in ("30m", "2h", "EOD", "1D"):
        vals = []
        for sig in state["signals"]:
            if kind is not None and sig.get("signal_kind", "intraday") != kind:
                continue
            result = (sig.get("outcomes") or {}).get(horizon) or {}
            v = _f(result.get("premium_return_pct"))
            if v is not None:
                vals.append(v)
        if vals:
            out[horizon] = {
                "count": len(vals),
                "win_rate_pct": round(sum(v > 0 for v in vals) / len(vals) * 100.0, 1),
                "avg_premium_return_pct": round(sum(vals) / len(vals), 3),
            }
        else:
            out[horizon] = {"count": 0, "win_rate_pct": None, "avg_premium_return_pct": None}
    out["registered"] = sum(1 for sig in state["signals"] if kind is None or sig.get("signal_kind", "intraday") == kind)
    return out
