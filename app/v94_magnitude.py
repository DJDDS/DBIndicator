"""V9.4 live magnitude research helpers.

Trial 14 is deliberately directionless: a point-in-time daily OI anomaly from
an already-completed session plus a *fresh* intraday compression onset is used
only to register an ATM long-straddle shadow observation.  Nothing here can
create a production TRADE/WATCH signal.
"""
from __future__ import annotations

import datetime as dt
import json
import math
import os
import pickle
import threading
from pathlib import Path

from . import derivative_intelligence as di

DAILY_OI_Z_MIN = 1.5
COMPRESSION_MIN = 60.0
MAX_CACHE_AGE_DAYS = 7
MAX_LIVE_CANDIDATES = int(os.getenv("V94_MAX_LIVE_MAGNITUDE_CANDIDATES", "6"))
CACHE_FILE = Path(os.getenv(
    "V94_DAILY_OI_CACHE_FILE",
    str(Path(os.getenv("RESEARCH_STATE_DIR", ".dbindicator-research")) / "v94-daily-oi-live.json"),
))

_state_lock = threading.Lock()
_compression_active: dict[str, bool] = {}


def _finite(v):
    try:
        return v is not None and math.isfinite(float(v))
    except (TypeError, ValueError):
        return False


def _atomic_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as fh:
        json.dump(payload, fh, default=str, separators=(",", ":"))
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


def load_daily_oi_snapshot() -> dict:
    try:
        with Path(CACHE_FILE).open("r", encoding="utf-8") as fh:
            payload = json.load(fh)
        if isinstance(payload, dict) and isinstance(payload.get("symbols"), dict):
            return payload
    except (OSError, ValueError, TypeError):
        pass
    return {"generated_at": None, "symbols": {}}


def _last_valid(frame, column):
    if frame is None or getattr(frame, "empty", True) or column not in frame.columns:
        return None, None
    series = frame[column]
    try:
        series = series.dropna()
    except Exception:
        return None, None
    if series.empty:
        return None, None
    value = series.iloc[-1]
    if not _finite(value):
        return None, None
    return float(value), series.index[-1]


def persist_daily_oi_snapshot_from_shards(shard_map: dict) -> dict:
    """Stream research shards and persist only the latest point-in-time daily OI read.

    This is intentionally tiny.  The live scanner never reopens 210 historical
    research shards and never refetches 210 daily OI histories during market
    hours.  It consumes this one compact snapshot instead.
    """
    symbols = {}
    latest_ts = None
    for symbol, path in (shard_map or {}).items():
        try:
            with Path(path).open("rb") as fh:
                payload = pickle.load(fh)
            frame = payload.get("compact_frame")
            z, z_ts = _last_valid(frame, "daily_oi_z_pti")
            chg, _ = _last_valid(frame, "daily_oi_chg_pct_pti")
            if z is None:
                continue
            ts_text = z_ts.isoformat() if hasattr(z_ts, "isoformat") else str(z_ts)
            symbols[str(symbol)] = {
                "daily_oi_z": z,
                "daily_oi_chg_pct": chg,
                "feature_ts": ts_text,
            }
            try:
                parsed = dt.datetime.fromisoformat(ts_text)
                if latest_ts is None or parsed > latest_ts:
                    latest_ts = parsed
            except (TypeError, ValueError):
                pass
        except Exception:
            continue
    now = dt.datetime.now(dt.timezone.utc).astimezone()
    out = {
        "generated_at": now.isoformat(timespec="seconds"),
        "latest_feature_ts": latest_ts.isoformat() if latest_ts is not None else None,
        "symbols": symbols,
        "source": "V9.4 point-in-time daily continuous-futures OI research cache",
        "research_only": True,
    }
    _atomic_json(Path(CACHE_FILE), out)
    return out


def _cache_entry_fresh(entry: dict, now: dt.datetime) -> bool:
    raw = (entry or {}).get("feature_ts")
    if not raw:
        return False
    try:
        ts = dt.datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        if ts.tzinfo is not None and now.tzinfo is None:
            ts = ts.replace(tzinfo=None)
        if ts.tzinfo is None and now.tzinfo is not None:
            now = now.replace(tzinfo=None)
        return (now.date() - ts.date()).days <= MAX_CACHE_AGE_DAYS
    except (TypeError, ValueError):
        return False


def fresh_trial14_candidates(rows, *, now=None) -> list[dict]:
    """Return fresh compression onsets whose cached *daily* OI z-score is abnormal.

    First observation initializes state and cannot manufacture an onset after a
    process restart.  Remaining compressed on subsequent scans also cannot fire
    repeatedly; a symbol must first leave compression and re-enter.
    """
    now = now or dt.datetime.now()
    cache = load_daily_oi_snapshot()
    symbol_cache = cache.get("symbols") or {}
    out = []
    with _state_lock:
        for row in rows or []:
            symbol = str(row.get("symbol") or "")
            if not symbol or row.get("error"):
                continue
            comp = row.get("compression_score")
            active = bool(_finite(comp) and float(comp) >= COMPRESSION_MIN)
            previous = _compression_active.get(symbol)
            _compression_active[symbol] = active
            if previous is None or not active or previous is True:
                continue
            evidence = symbol_cache.get(symbol) or {}
            z = evidence.get("daily_oi_z")
            if not (_finite(z) and float(z) >= DAILY_OI_Z_MIN and _cache_entry_fresh(evidence, now)):
                continue
            candidate = dict(row)
            candidate.update({
                "v94_trial14_live_candidate": True,
                "trial": "Trial 14",
                "precursor": "Daily OI anomaly + fresh compression onset",
                "trial14_daily_oi_z": float(z),
                "trial14_daily_oi_chg_pct": evidence.get("daily_oi_chg_pct"),
                "trial14_daily_oi_feature_ts": evidence.get("feature_ts"),
            })
            out.append(candidate)
    return out


def register_live_trial14_straddles(kite, rows, *, now=None, max_candidates=None) -> dict:
    """Register executable ATM-straddle shadow signals for fresh Trial-14 precursors."""
    now = now or dt.datetime.now()
    candidates = fresh_trial14_candidates(rows, now=now)
    if not candidates:
        return {"candidates": 0, "registered": 0}
    limit = max(1, int(max_candidates or MAX_LIVE_CANDIDATES))
    candidates = candidates[:limit]
    cmap = di.get_option_contracts_map(kite)
    registered = 0
    for cand in candidates:
        symbol = cand.get("symbol")
        spot = float(cand.get("close")) if _finite(cand.get("close")) else None
        contracts = list(cmap.get(symbol) or [])
        if spot is None or not contracts:
            continue
        today = now.date()
        expiries = sorted({
            c.get("expiry") for c in contracts
            if c.get("expiry") and c.get("expiry") >= today and (c.get("expiry") - today).days >= 3
        })
        if not expiries:
            continue
        expiry = expiries[0]
        expc = [c for c in contracts if c.get("expiry") == expiry]
        strikes = sorted({float(c.get("strike")) for c in expc if _finite(c.get("strike"))})
        if not strikes:
            continue
        atm = min(strikes, key=lambda x: abs(x - spot))
        pair = [c for c in expc if _finite(c.get("strike")) and float(c.get("strike")) == atm and c.get("instrument_type") in ("CE", "PE")]
        if len(pair) < 2:
            continue
        keys = [f"NFO:{c['tradingsymbol']}" for c in pair if c.get("tradingsymbol")]
        try:
            quotes = kite.quote(keys)
        except Exception:
            continue
        chain = di.analyze_option_quotes(symbol, None, spot, pair, quotes, now=now, min_dte=3)
        sid = di.register_long_vol_signal(cand, chain, now=now)
        if not sid:
            continue
        registered += 1
        # Attach only research diagnostics to the real scan row; no alert or
        # production state is changed.
        for row in rows or []:
            if row.get("symbol") == symbol:
                row["v94_trial14_shadow_registered"] = True
                row["v94_trial14_daily_oi_z"] = cand.get("trial14_daily_oi_z")
                row["v94_trial14_straddle_ask"] = chain.get("atm_straddle_ask")
                row["v94_trial14_atm_iv_pct"] = chain.get("atm_iv_pct")
                row["v94_trial14_dte"] = chain.get("dte")
                break
    return {"candidates": len(candidates), "registered": registered}


def reset_live_state_for_tests():
    with _state_lock:
        _compression_active.clear()
