"""CALL_EXECUTION_CANDIDATE_V1.

Pure, read-only execution-quality scorer derived from the frozen V3 research
model (Apr-May train, June gate selection, Jul-Sep historical evaluation).

Important product contract:
- CALL / Bullish only.
- The underlying candidate engine remains authoritative.
- This module never places orders and does not mutate the underlying lifecycle.
- ATM-1 / ATM / ATM+1 in the preferred monthly expiry compete on the same
  frozen execution probability.
- Gate: probability >= 0.5929846109883474.
- Evaluation horizon: 5 minutes (research candidate; forward confirmation still
  required because the 5-minute horizon was selected after reviewing OOS).
"""
from __future__ import annotations

import base64
import datetime as dt
import json
import math
import lzma

MODEL_THRESHOLD = 0.5929846109883474
EVALUATION_HORIZON_SECONDS = 300
MAX_LIVE_SPREAD_PCT = 4.0
RECORDER_MEDIAN_SPREAD_PCT = 1.8059
HISTORY_TOLERANCE_SECONDS = 20.0
MODEL_LABEL = "CALL_EXECUTION_CANDIDATE_V1"
VALIDATION_LABEL = "RESEARCH CANDIDATE · FORWARD CONFIRMATION REQUIRED"

_MODEL_B64 = None
_MODEL = None


def _finite(v):
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    return x if math.isfinite(x) else None


def _dt(v):
    if isinstance(v, dt.datetime):
        return v
    if not v:
        return None
    try:
        return dt.datetime.fromisoformat(str(v).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None


def _model():
    global _MODEL, _MODEL_B64
    if _MODEL is None:
        if _MODEL_B64 is None:
            from pathlib import Path
            _MODEL_B64 = Path(__file__).with_name("v3_call_execution_model.b64").read_text(encoding="ascii").strip()
        raw = lzma.decompress(base64.b64decode(_MODEL_B64.encode("ascii")))
        _MODEL = json.loads(raw.decode("utf-8"))
    return _MODEL


def score_features(features: dict) -> float:
    """Return the exact frozen HistGradientBoosting probability in pure Python."""
    model = _model()
    values = []
    for i, name in enumerate(model["features"]):
        x = _finite((features or {}).get(name))
        if x is None:
            x = _finite(model["imputer_medians"][i])
        values.append(x)

    score = float(model["baseline"])
    for nodes in model["trees"]:
        idx = 0
        while not int(nodes[idx]["leaf"]):
            node = nodes[idx]
            fi = int(node["f"])
            value = values[fi]
            if value is None:
                go_left = bool(node["m"])
            else:
                go_left = value <= float(node["t"])
            idx = int(node["l"] if go_left else node["r"])
        score += float(nodes[idx]["v"])
    if score >= 0:
        z = math.exp(-score)
        return 1.0 / (1.0 + z)
    z = math.exp(score)
    return z / (1.0 + z)


def _sample_at_or_before(samples, target, tolerance=HISTORY_TOLERANCE_SECONDS):
    target = _dt(target)
    if target is None:
        return None
    best = None
    best_age = None
    for raw in reversed(list(samples or [])):
        ts = _dt((raw or {}).get("ts"))
        if ts is None:
            continue
        if ts.tzinfo is not None and target.tzinfo is None:
            ts = ts.replace(tzinfo=None)
        elif ts.tzinfo is None and target.tzinfo is not None:
            ts = ts.replace(tzinfo=target.tzinfo)
        age = (target - ts).total_seconds()
        if age < 0:
            continue
        if age <= tolerance and (best_age is None or age < best_age):
            best, best_age = raw, age
        if age > tolerance:
            break
    return dict(best) if best is not None else None


def _pct(a, b):
    a, b = _finite(a), _finite(b)
    if a is None or b is None or b == 0:
        return None
    return (a / b - 1.0) * 100.0


def _business_days_inclusive(start, end):
    if isinstance(start, dt.datetime):
        start = start.date()
    if isinstance(end, dt.datetime):
        end = end.date()
    if end < start:
        return 0
    out = 0
    day = start
    while day <= end:
        if day.weekday() < 5:
            out += 1
        day += dt.timedelta(days=1)
    return out


def _preferred_expiry(snaps, now):
    expiries = []
    for snap in snaps or []:
        raw = str((snap or {}).get("expiry") or "")
        try:
            expiry = dt.date.fromisoformat(raw[:10])
        except ValueError:
            continue
        expiries.append(expiry)
    expiries = sorted(set(expiries))
    if not expiries:
        return None, None
    if len(expiries) >= 2 and _business_days_inclusive(now.date(), expiries[0]) <= 5:
        return expiries[1], 2
    return expiries[0], 1


def _minute_volume(samples, now, minutes_ago=0):
    end = now - dt.timedelta(minutes=minutes_ago)
    start = end - dt.timedelta(minutes=1)
    e = _sample_at_or_before(samples, end)
    s = _sample_at_or_before(samples, start)
    ev = _finite((e or {}).get("cum_volume"))
    sv = _finite((s or {}).get("cum_volume"))
    if ev is None or sv is None:
        return None
    return max(0.0, ev - sv)


def _features_for_contract(snap, samples, cash_samples, now, *, offset, code):
    mid = _finite(snap.get("mid"))
    spot = _finite(snap.get("spot"))
    strike = _finite(snap.get("strike"))
    iv = _finite(snap.get("iv_pct"))
    oi = _finite(snap.get("oi"))
    delta = abs(_finite(snap.get("delta")) or 0.0)
    dte = _finite(snap.get("dte"))
    if None in (mid, spot, strike, iv, dte) or mid <= 0 or spot <= 0 or strike <= 0 or delta <= 0:
        return None, False

    current = _sample_at_or_before(samples, now)
    if current is None:
        return None, False

    out = {"iv": iv, "moneyness_pct": (spot - strike) / spot * 100.0,
            "abs_delta": delta, "dte_days": dte,
            "premium_pct_spot": mid / spot * 100.0,
            "elasticity": delta * spot / mid,
            "required_med_bps": mid * (RECORDER_MEDIAN_SPREAD_PCT / 100.0) / delta / spot * 10000.0,
            "minute_of_day": now.hour * 60 + now.minute - (9 * 60 + 15),
            "offset": float(offset), "code": float(code)}

    history_ready = True
    for lag in (1, 3, 5):
        prev = _sample_at_or_before(samples, now - dt.timedelta(minutes=lag))
        prev_spot = _sample_at_or_before(cash_samples, now - dt.timedelta(minutes=lag))
        if prev is None or prev_spot is None:
            history_ready = False

        pmid = _finite((prev or {}).get("mid"))
        piv = _finite((prev or {}).get("iv_pct"))
        poi = _finite((prev or {}).get("oi"))
        pspot = _finite((prev_spot or {}).get("price"))

        out[f"prem_ret{lag}m_pct"] = _pct(mid, pmid)
        out[f"iv_chg{lag}m"] = (iv - piv) if piv is not None else None
        out[f"oi_chg{lag}m_pct"] = _pct(oi, poi) if poi is not None and poi > 0 else None
        out[f"spot_ret{lag}m_bps"] = ((spot / pspot - 1.0) * 10000.0) if pspot is not None and pspot > 0 else None

    r1, r3 = out.get("prem_ret1m_pct"), out.get("prem_ret3m_pct")
    s1, s3 = out.get("spot_ret1m_bps"), out.get("spot_ret3m_bps")
    out["prem_accel"] = (r1 - r3 / 3.0) if r1 is not None and r3 is not None else None
    out["spot_accel_bps"] = (s1 - s3 / 3.0) if s1 is not None and s3 is not None else None

    current_minute_vol = _minute_volume(samples, now, 0)
    prior = [_minute_volume(samples, now, j) for j in range(1, 11)]
    prior = [x for x in prior if x is not None]
    if current_minute_vol is not None and prior:
        ordered = sorted(prior)
        n = len(ordered)
        median = ordered[n // 2] if n % 2 else (ordered[n // 2 - 1] + ordered[n // 2]) / 2.0
        out["vol_ratio10"] = current_minute_vol / median if median > 0 else None
    else:
        out["vol_ratio10"] = None
    out["log_volume"] = math.log1p(max(0.0, current_minute_vol or 0.0))
    out["log_oi"] = math.log1p(max(0.0, oi or 0.0))
    return out, history_ready


def evaluate_call_candidates(option_snapshots, option_history, cash_samples, *, now, direction, spot):
    """Score live CE ATM-1/ATM/ATM+1 contracts without controlling the trade state."""
    if str(direction or "") != "Bullish":
        return {
            "label": MODEL_LABEL, "state": "NOT_APPLICABLE", "pass": False,
            "reason": "CALL-only candidate; bearish/PUT path intentionally disabled",
            "threshold": MODEL_THRESHOLD, "evaluation_horizon_seconds": EVALUATION_HORIZON_SECONDS,
            "validation_label": VALIDATION_LABEL,
        }

    snaps = [dict(x) for x in (option_snapshots or []) if str((x or {}).get("type") or "") == "CE"]
    expiry, code = _preferred_expiry(snaps, now)
    spot = _finite(spot)
    if expiry is None or code is None or spot is None or spot <= 0:
        return {
            "label": MODEL_LABEL, "state": "WARMING", "pass": False,
            "reason": "preferred CALL expiry/spot unavailable",
            "threshold": MODEL_THRESHOLD, "evaluation_horizon_seconds": EVALUATION_HORIZON_SECONDS,
            "validation_label": VALIDATION_LABEL,
        }

    exp_snaps = [x for x in snaps if str(x.get("expiry") or "")[:10] == expiry.isoformat()]
    strikes = sorted(set(_finite(x.get("strike")) for x in exp_snaps if _finite(x.get("strike")) is not None))
    if not strikes:
        return {
            "label": MODEL_LABEL, "state": "WARMING", "pass": False,
            "reason": "ATM neighborhood unavailable",
            "threshold": MODEL_THRESHOLD, "evaluation_horizon_seconds": EVALUATION_HORIZON_SECONDS,
            "validation_label": VALIDATION_LABEL,
        }
    atm = min(strikes, key=lambda x: abs(x - spot))
    ai = strikes.index(atm)
    keep = set(strikes[max(0, ai - 1):min(len(strikes), ai + 2)])

    scored = []
    diag = {"atm_neighbourhood": 0, "no_live_quote": 0, "no_iv_or_delta": 0, "no_history_sample": 0}
    for snap in exp_snaps:
        strike = _finite(snap.get("strike"))
        symbol = str(snap.get("symbol") or "")
        if strike not in keep or not symbol:
            continue
        diag["atm_neighbourhood"] += 1
        if _finite(snap.get("spot")) is None:
            # Live contract snapshots (derivative_intelligence.contract_snapshot)
            # carry no underlying price. Without this every contract failed
            # feature construction and CALL V1 was permanently BLOCKED live.
            snap = dict(snap, spot=spot)
        offset = strikes.index(strike) - ai
        hist = (option_history or {}).get(symbol) or []
        features, ready = _features_for_contract(
            snap, hist, cash_samples or [], now, offset=offset, code=code,
        )
        if features is None:
            mid = _finite(snap.get("mid"))
            if mid is None or mid <= 0:
                diag["no_live_quote"] += 1
            elif _finite(snap.get("iv_pct")) is None or not _finite(snap.get("delta")):
                diag["no_iv_or_delta"] += 1
            else:
                diag["no_history_sample"] += 1
            continue
        probability = score_features(features)
        bid, ask = _finite(snap.get("bid")), _finite(snap.get("ask"))
        spread = _finite(snap.get("spread_pct"))
        quote_ok = bool(
            bid is not None and ask is not None and bid > 0 and ask >= bid
            and spread is not None and spread <= MAX_LIVE_SPREAD_PCT
        )
        scored.append({
            "contract": symbol, "strike": strike, "expiry": expiry.isoformat(),
            "offset": int(offset), "probability": round(probability, 6),
            "history_ready": bool(ready), "quote_ok": quote_ok,
            "spread_pct": spread, "delta_abs": abs(_finite(snap.get("delta")) or 0.0),
            "dte": snap.get("dte"), "mid": snap.get("mid"), "bid": bid, "ask": ask,
        })

    eligible = [x for x in scored if x["history_ready"] and x["quote_ok"]]
    eligible.sort(key=lambda x: (x["probability"], -abs(int(x["offset"]))), reverse=True)
    if not eligible:
        if scored:
            reason = "5-minute same-contract history still warming"
        elif diag["atm_neighbourhood"] and diag["no_live_quote"] == diag["atm_neighbourhood"]:
            # Subscribed contracts but no ticks yet: typical right after a
            # service restart or a fresh subscription, not an illiquid chain.
            reason = "no live ATM±1 CALL ticks yet (subscribed, waiting for quotes)"
        elif diag["no_iv_or_delta"]:
            reason = "ATM±1 CALL quoted but IV/delta could not be solved"
        else:
            reason = "no quoted ATM±1 CALL contract"
        return {
            "label": MODEL_LABEL, "state": "WARMING" if scored else "BLOCKED", "pass": False,
            "reason": reason,
            "diagnostics": diag,
            "threshold": MODEL_THRESHOLD, "evaluation_horizon_seconds": EVALUATION_HORIZON_SECONDS,
            "preferred_expiry": expiry.isoformat(), "expiry_code": code,
            "top_contracts": sorted(scored, key=lambda x: x["probability"], reverse=True)[:3],
            "validation_label": VALIDATION_LABEL,
            "controls_trading": False,
        }

    best = eligible[0]
    passed = best["probability"] >= MODEL_THRESHOLD
    return {
        "label": MODEL_LABEL,
        "state": "PASS" if passed else "BELOW_GATE",
        "pass": bool(passed),
        "reason": (
            f"frozen V3 CALL probability {best['probability']:.3f} >= {MODEL_THRESHOLD:.3f}"
            if passed else
            f"frozen V3 CALL probability {best['probability']:.3f} < {MODEL_THRESHOLD:.3f}"
        ),
        "probability": best["probability"], "threshold": MODEL_THRESHOLD,
        "selected_contract": best["contract"], "selected_strike": best["strike"],
        "selected_offset": best["offset"], "selected_expiry": best["expiry"],
        "selected_spread_pct": best["spread_pct"], "selected_delta_abs": best["delta_abs"],
        "selected_mid": best["mid"], "selected_bid": best["bid"], "selected_ask": best["ask"],
        "evaluation_horizon_seconds": EVALUATION_HORIZON_SECONDS,
        "preferred_expiry": expiry.isoformat(), "expiry_code": code,
        "top_contracts": eligible[:3],
        "validation_label": VALIDATION_LABEL,
        "controls_trading": False,
    }
