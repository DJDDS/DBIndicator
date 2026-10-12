"""MCX NATURALGAS live layer for the NG Commodity Desk (ADMIN ONLY, spotting / paper - no orders).

Turns the NYMEX-researched signal into MCX terms the trader actually uses:
  - front MCX future (NATURALGAS or NATGASMINI) with LTP / bid / ask / OI / lot
  - levels in rupees on the MCX future: entry = LTP, T1/T2/stop = LTP * (1 +/- k * sigma_h)
  - option chain slice for the signal side (CE for CALL, PE for PUT): ITM2..OTM2 on the nearest
    expiry that leaves enough days, with bid/ask/spread/OI, Black-76 IV and delta, and the
    estimated option value at T1 / T2 / stop after the holding period (same IV, time decayed)
  - a suggested strike (ATM, or 1 ITM if the ATM spread is too wide) - the final choice is the trader's
All Kite calls are fail-soft: any error returns {"error": ...} and the page still renders.
"""
from __future__ import annotations

import json
import math
from datetime import date, datetime, timedelta

from . import commodity_desk as cd

IST_CLOSE_HOUR = 23.5          # options/futures treated as expiring at 23:30 IST on expiry date


# ----------------------------------------------------------------------------- Black-76 (r = 0)
def _n(x: float) -> float:
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


def b76(F: float, K: float, T: float, v: float, call: bool) -> float:
    if T <= 0 or v <= 0:
        return max(F - K, 0.0) if call else max(K - F, 0.0)
    s = v * math.sqrt(T); d1 = (math.log(F / K) + 0.5 * s * s) / s
    c = F * _n(d1) - K * _n(d1 - s)
    return c if call else c - (F - K)


def b76_delta(F: float, K: float, T: float, v: float, call: bool) -> float | None:
    if T <= 0 or v <= 0:
        return None
    d1 = (math.log(F / K) + 0.5 * v * v * T) / (v * math.sqrt(T))
    return _n(d1) if call else _n(d1) - 1


def implied_vol(price: float, F: float, K: float, T: float, call: bool) -> float | None:
    intrinsic = max(F - K, 0.0) if call else max(K - F, 0.0)
    if not (price > 0 and F > 0 and K > 0 and T > 0) or price <= intrinsic + 1e-9:
        return None
    lo, hi = 0.01, 5.0
    if b76(F, K, T, hi, call) < price:
        return None
    for _ in range(80):
        mid = (lo + hi) / 2
        if b76(F, K, T, mid, call) > price:
            hi = mid
        else:
            lo = mid
    return (lo + hi) / 2


def years_to(expiry: date, now: datetime) -> float:
    exp_dt = datetime.combine(expiry, datetime.min.time()) + timedelta(hours=IST_CLOSE_HOUR)
    return max((exp_dt - now).total_seconds(), 0) / (365 * 86400)


# ----------------------------------------------------------------------------- instruments (cached daily)
def _as_date(x):
    if isinstance(x, datetime):
        return x.date()
    if isinstance(x, date):
        return x
    return datetime.strptime(str(x)[:10], "%Y-%m-%d").date()


def instruments(kite, today: date) -> list[dict]:
    f = cd._p("mcx_instruments.json")
    try:
        cached = json.loads(f.read_text())
        if cached.get("date") == str(today) and cached.get("rows"):
            return cached["rows"]
    except Exception:
        pass
    rows = []
    for r in kite.instruments("MCX"):
        if r.get("name") in ("NATURALGAS", "NATGASMINI") and r.get("instrument_type") in ("FUT", "CE", "PE"):
            rows.append({"tradingsymbol": r["tradingsymbol"], "instrument_token": r.get("instrument_token"), "name": r["name"],
                         "type": r["instrument_type"], "strike": float(r.get("strike") or 0), "expiry": str(_as_date(r["expiry"])),
                         "lot_size": r.get("lot_size"), "tick_size": r.get("tick_size")})
    f.write_text(json.dumps({"date": str(today), "rows": rows}))
    return rows


def pick_future(rows: list[dict], name: str, today: date, roll_days: int = 2) -> dict | None:
    futs = sorted([r for r in rows if r["name"] == name and r["type"] == "FUT" and _as_date(r["expiry"]) >= today], key=lambda r: r["expiry"])
    for r in futs:
        if (_as_date(r["expiry"]) - today).days > roll_days:
            return r
    return futs[-1] if futs else None


def pick_option_expiry(rows: list[dict], name: str, today: date, min_days: int) -> date | None:
    exps = sorted({_as_date(r["expiry"]) for r in rows if r["name"] == name and r["type"] in ("CE", "PE") and _as_date(r["expiry"]) >= today})
    for e in exps:
        if (e - today).days >= min_days:
            return e
    return exps[-1] if exps else None


def strike_slice(rows: list[dict], name: str, typ: str, expiry: date, F: float, width: int = 2) -> list[tuple[str, dict]]:
    """Labelled strikes around ATM for one side: ITM-width..OTM+width (ITM for CE = below F, for PE = above F)."""
    chain = sorted([r for r in rows if r["name"] == name and r["type"] == typ and _as_date(r["expiry"]) == expiry], key=lambda r: r["strike"])
    if not chain:
        return []
    atm = min(range(len(chain)), key=lambda i: abs(chain[i]["strike"] - F))
    out = []
    for off in range(-width, width + 1):
        i = atm + off
        if 0 <= i < len(chain):
            # for CE a lower strike is ITM; for PE a higher strike is ITM
            k = -off if typ == "CE" else off
            lab = "ATM" if off == 0 else (f"ITM{k}" if k > 0 else f"OTM{-k}")
            out.append((lab, chain[i]))
    return out


# ----------------------------------------------------------------------------- live snapshot
def _bidask(q: dict) -> tuple[float | None, float | None]:
    dp = q.get("depth") or {}
    bid = (dp.get("buy") or [{}])[0].get("price") or None
    ask = (dp.get("sell") or [{}])[0].get("price") or None
    return bid, ask


def snapshot(kite, settings: dict, side: int | None, hold: int | None, sigma_d: float, now: datetime) -> dict:
    """side +1 CALL / -1 PUT / None (no signal: show both ATM CE and PE for context)."""
    today = now.date()
    name = settings.get("mcx_symbol", "NATURALGAS")
    rows = instruments(kite, today)
    fut = pick_future(rows, name, today, int(settings.get("mcx_roll_days", 2)))
    if not fut:
        return {"error": f"No live {name} future in the MCX instrument list"}
    fsym = "MCX:" + fut["tradingsymbol"]
    fq = (kite.quote([fsym]) or {}).get(fsym) or {}
    F = fq.get("last_price")
    if not F:
        return {"error": f"No price for {fut['tradingsymbol']} (market closed or no quote yet)"}
    fb, fa = _bidask(fq)
    out = {"symbol": name, "future": {"tradingsymbol": fut["tradingsymbol"], "expiry": fut["expiry"], "ltp": F, "bid": fb, "ask": fa,
                                      "oi": fq.get("oi"), "lot_size": fut.get("lot_size"), "change_pct": None,
                                      "close": (fq.get("ohlc") or {}).get("close")}}
    if out["future"]["close"]:
        out["future"]["change_pct"] = round((F / out["future"]["close"] - 1) * 100, 2)

    h = int(hold or 1)
    sh = sigma_d * math.sqrt(h)                       # holding-period sigma as a fraction
    out["sigma_h_pct"] = round(sh * 100, 2)
    if side:
        lv = lambda k: round(F * (1 + side * k * sh), 1)
        out["levels"] = {"entry": round(F, 1), "t1": lv(settings["t1"]), "t2": lv(settings["t2"]), "stop": lv(-settings["stop"]),
                         "t1_rupees": round(abs(lv(settings["t1"]) - F), 1), "stop_rupees": round(abs(F - lv(-settings["stop"])), 1)}

    exp = pick_option_expiry(rows, name, today, int(settings.get("opt_min_days", 4)))
    if not exp:
        out["options_error"] = "No option expiry available"
        return out
    T = years_to(exp, now)
    T_after = max(T - h * 1.4 / 365, 1e-6)            # trading sessions -> calendar days (~7/5)
    out["option_expiry"] = str(exp); out["option_days"] = (exp - today).days
    sides = [("CE" if side > 0 else "PE")] if side else ["CE", "PE"]
    width = 2 if side else 0
    picks = [(typ, lab, r) for typ in sides for lab, r in strike_slice(rows, name, typ, exp, F, width)]
    qs = kite.quote(["MCX:" + r["tradingsymbol"] for _, _, r in picks]) if picks else {}
    chain = []
    for typ, lab, r in picks:
        q = qs.get("MCX:" + r["tradingsymbol"]) or {}
        bid, ask = _bidask(q); ltp = q.get("last_price")
        mid = (bid + ask) / 2 if bid and ask and ask >= bid else (ltp or None)
        call = typ == "CE"; K = r["strike"]
        iv = implied_vol(mid, F, K, T, call) if mid else None
        row = {"type": typ, "label": lab, "tradingsymbol": r["tradingsymbol"], "instrument_token": r.get("instrument_token"),
               "strike": K, "ltp": ltp, "bid": bid, "ask": ask, "mid": round(mid, 2) if mid else None,
               "spread_pct": round((ask - bid) / mid * 100, 2) if bid and ask and mid else None,
               "oi": q.get("oi"), "volume": q.get("volume"), "lot_size": r.get("lot_size"),
               "iv_pct": round(iv * 100, 1) if iv else None,
               "delta": round(b76_delta(F, K, T, iv, call), 2) if iv else None,
               "premium_per_lot": round(mid * (r.get("lot_size") or 1), 0) if mid else None}
        if side and iv and mid:
            est = {k: b76(out["levels"][k], K, T_after, iv, call) for k in ("t1", "t2", "stop")}
            row["est"] = {k: round(v, 2) for k, v in est.items()}
            row["est_pct"] = {k: round((v / mid - 1) * 100, 1) for k, v in est.items()}
            row["reward_risk"] = round((est["t1"] - mid) / (mid - est["stop"]), 2) if mid > est["stop"] else None
        chain.append(row)
    out["chain"] = chain
    if side:
        out["suggested"] = suggest(chain, float(settings.get("max_spread_pct", 3.0)))
    return out


def suggest(chain: list[dict], max_spread: float) -> dict | None:
    """ATM by default (research: 0.5 sigma is reached ~60% of the time, 1.5 sigma only ~20%); step to ITM1 when ATM
    is too wide or has no two-sided quote. Returns the chosen row label + reason. The trader makes the final call."""
    by = {r["label"]: r for r in chain}
    ok = lambda r: r and r.get("mid") and r.get("spread_pct") is not None and r["spread_pct"] <= max_spread
    for lab, why in (("ATM", "ATM: best balance of delta and premium for a 0.5-1 sigma move"),
                     ("ITM1", f"ATM spread above {max_spread}% - 1 ITM is tighter"),
                     ("OTM1", "ATM/ITM1 illiquid - OTM1 is the only tight quote (lower hit rate)")):
        if ok(by.get(lab)):
            return {"label": lab, "tradingsymbol": by[lab]["tradingsymbol"], "reason": why}
    return {"label": None, "reason": f"No strike with a two-sided quote under {max_spread}% spread - use the future or skip"}


def day_close(kite, token, d: date) -> float | None:
    """Close of one day's candle for an instrument (used to score the MCX paper ledger)."""
    try:
        rows = kite.historical_data(token, d, d + timedelta(days=1), "day") or []
        for r in rows:
            if _as_date(r["date"]) == d:
                return float(r["close"])
    except Exception:
        return None
    return None
