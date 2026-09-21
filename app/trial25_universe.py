"""Trial-25 frozen-cohort versus live F&O-universe reconciliation."""
from __future__ import annotations

import datetime as dt
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
    return {
        "asof": now.isoformat(timespec="seconds"),
        "cohort_a_live": sorted(frozen & live),
        "cohort_a_missing_contracts": sorted(frozen - live),
        "new_fno_onboarding": sorted(live - frozen),
        "first_seen": _merge_seen(prior.get("first_seen") or {}, live, now, first=True),
        "last_seen": _merge_seen(prior.get("last_seen") or {}, live, now, first=False),
    }


def load_universe_state(path) -> dict:
    """Load the persisted post-freeze F&O reconciliation state fail-soft."""
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
        if isinstance(raw, dict):
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
        "cohort_a_live": [],
        "cohort_a_missing_contracts": [],
        "new_fno_onboarding": [],
        "first_seen": {},
        "last_seen": {},
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


def update_universe_state(path, frozen_symbols: set[str], contracts_map: dict, now: dt.datetime) -> dict:
    """Reconcile and atomically persist the frozen cohort versus the live OPTSTK master."""
    prior = load_universe_state(path)
    current = reconcile_universe(frozen_symbols, contracts_map, prior, now)
    _save_universe_state(path, current)
    return current
