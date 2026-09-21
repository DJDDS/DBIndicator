"""Trial-25 frozen-cohort versus live F&O-universe reconciliation."""
from __future__ import annotations

import datetime as dt
import hashlib
import json
from pathlib import Path


def _as_date(value) -> dt.date | None:
    if isinstance(value, dt.datetime):
        return value.date()
    if isinstance(value, dt.date):
        return value
    try:
        return dt.date.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None


def current_stock_option_underlyings(contracts_map: dict, today: dt.date) -> set[str]:
    """Return underlyings with both live CE and PE stock-option contracts."""
    out: set[str] = set()
    for symbol, rows in (contracts_map or {}).items():
        types = set()
        for row in rows or []:
            typ = row.get("instrument_type")
            expiry = _as_date(row.get("expiry"))
            if typ in ("CE", "PE") and expiry is not None and expiry >= today:
                types.add(typ)
        if {"CE", "PE"}.issubset(types):
            out.add(str(symbol))
    return out


def _merge_seen(prior: dict, live: set[str], now: dt.datetime, *, first: bool) -> dict:
    out = dict(prior or {})
    ts = now.isoformat(timespec="seconds")
    for symbol in sorted(live):
        if first:
            out.setdefault(symbol, ts)
        else:
            out[symbol] = ts
    return out


def reconcile_universe(
    frozen_symbols: set[str],
    contracts_map: dict,
    prior: dict | None,
    now: dt.datetime,
) -> dict:
    """Keep the frozen research cohort separate from post-freeze F&O churn."""
    frozen = {str(x) for x in (frozen_symbols or set())}
    live = current_stock_option_underlyings(contracts_map or {}, now.date())
    prior = prior or {}
    prior_live = {str(x) for x in (prior.get("live_underlyings") or [])}
    symbols = sorted(live)
    symbol_hash = hashlib.sha256("\n".join(symbols).encode("utf-8")).hexdigest()
    return {
        "asof": now.isoformat(timespec="seconds"),
        "source": "LIVE_KITE",
        "live_underlyings": symbols,
        "symbol_set_sha256": symbol_hash,
        "added": sorted(live - prior_live),
        "removed": sorted(prior_live - live),
        "cohort_a_live": sorted(frozen & live),
        "cohort_a_missing_contracts": sorted(frozen - live),
        "new_fno_onboarding": sorted(live - frozen),
        "first_seen": _merge_seen(prior.get("first_seen") or {}, live, now, first=True),
        "last_seen": _merge_seen(prior.get("last_seen") or {}, live, now, first=False),
        "last_ledger_date": prior.get("last_ledger_date"),
        "last_ledger_hash": prior.get("last_ledger_hash"),
    }


def load_universe_state(path) -> dict:
    """Load the persisted post-freeze F&O reconciliation state fail-soft."""
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
        if isinstance(raw, dict):
            raw.setdefault("source", None)
            raw.setdefault("live_underlyings", [])
            raw.setdefault("symbol_set_sha256", None)
            raw.setdefault("added", [])
            raw.setdefault("removed", [])
            raw.setdefault("cohort_a_live", [])
            raw.setdefault("cohort_a_missing_contracts", [])
            raw.setdefault("new_fno_onboarding", [])
            raw.setdefault("first_seen", {})
            raw.setdefault("last_seen", {})
            return raw
    except (OSError, ValueError, TypeError):
        pass
    return {
        "asof": None,
        "source": None,
        "live_underlyings": [],
        "symbol_set_sha256": None,
        "added": [],
        "removed": [],
        "cohort_a_live": [],
        "cohort_a_missing_contracts": [],
        "new_fno_onboarding": [],
        "first_seen": {},
        "last_seen": {},
        "last_ledger_date": None,
        "last_ledger_hash": None,
    }


def _save_universe_state(path, payload: dict) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(
        json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str),
        encoding="utf-8",
    )
    tmp.replace(p)


def _append_universe_ledger(path, payload: dict) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str) + "\n")


def update_universe_state(
    path,
    frozen_symbols: set[str],
    contracts_map: dict,
    now: dt.datetime,
    *,
    ledger_path=None,
) -> dict:
    """Persist latest membership plus append-only point-in-time observations.

    A ledger row is appended when the trading date changes or the live
    stock-option symbol-set hash changes. Post-freeze additions remain
    observation-only and never enter the frozen Trial-25 cohort.
    """
    prior = load_universe_state(path)
    current = reconcile_universe(frozen_symbols, contracts_map, prior, now)
    trading_date = now.date().isoformat()
    membership_changed = current.get("symbol_set_sha256") != prior.get("last_ledger_hash")
    date_changed = trading_date != prior.get("last_ledger_date")
    if ledger_path is not None and (membership_changed or date_changed):
        _append_universe_ledger(ledger_path, {
            "asof": current["asof"],
            "trading_date": trading_date,
            "source": "LIVE_KITE",
            "symbol_set_sha256": current["symbol_set_sha256"],
            "live_underlyings": current["live_underlyings"],
            "added": current["added"],
            "removed": current["removed"],
            "cohort_a_live": current["cohort_a_live"],
            "cohort_a_missing_contracts": current["cohort_a_missing_contracts"],
            "new_fno_onboarding": current["new_fno_onboarding"],
        })
        current["last_ledger_date"] = trading_date
        current["last_ledger_hash"] = current["symbol_set_sha256"]
    _save_universe_state(path, current)
    return current
