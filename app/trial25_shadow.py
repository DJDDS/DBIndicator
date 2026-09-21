"""Persistent, no-P&L Trial-25 event evidence state machine."""
from __future__ import annotations

import hashlib
import json
from collections import Counter
from pathlib import Path


TERMINAL_UNAVAILABLE_PREFIX = "UNAVAILABLE_"
REQUIRED_ROLES = ("atm_call", "atm_put", "lower_put", "upper_call")


def event_id(symbol, meeting_date, entry_date) -> str:
    raw = f"{symbol}|{meeting_date}|{entry_date}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]


def empty_state() -> dict:
    return {
        "version": 1,
        "events": {},
        "entry_capture_count": 0,
        "exit_capture_count": 0,
        "last_error": None,
        "last_updated_at": None,
    }


def load_state(path) -> dict:
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
        if isinstance(raw, dict) and isinstance(raw.get("events"), dict):
            raw.setdefault("entry_capture_count", 0)
            raw.setdefault("exit_capture_count", 0)
            raw.setdefault("last_error", None)
            raw.setdefault("last_updated_at", None)
            return raw
    except (OSError, ValueError, TypeError):
        pass
    return empty_state()


def _atomic_save(path, payload: dict) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str), encoding="utf-8")
    tmp.replace(p)


def _append(path, payload: dict) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str) + "\n")


def _event_key(event: dict) -> str:
    return event_id(event.get("symbol"), event.get("meeting_date"), event.get("entry_date"))


def _transition(ledger_file, event_id_value: str, old: str | None, new: str, when, reason=None) -> None:
    _append(ledger_file, {
        "event_id": event_id_value,
        "from": old,
        "to": new,
        "at": when.isoformat(timespec="seconds"),
        "reason": reason,
    })


def record_entry(*, state_file, ledger_file, raw_quote_file, event: dict, structure: dict, quotes: dict, captured_at) -> dict:
    state = load_state(state_file)
    key = _event_key(event)
    existing = (state.get("events") or {}).get(key)
    if existing and existing.get("status") in ("ENTRY_CAPTURED", "EXIT_DUE", "COMPLETED_RAW"):
        return dict(existing)
    if existing and str(existing.get("status") or "").startswith(TERMINAL_UNAVAILABLE_PREFIX):
        return dict(existing)

    identities = dict((structure or {}).get("contract_identities") or {})
    if set(identities) != set(REQUIRED_ROLES):
        unavailable = {
            **dict(event),
            "event_id": key,
            "status": "UNAVAILABLE_WING_BOOK",
            "reason": "FOUR_CONTRACT_IDENTITIES_REQUIRED",
        }
        state.setdefault("events", {})[key] = unavailable
        state["last_updated_at"] = captured_at.isoformat(timespec="seconds")
        _transition(ledger_file, key, (existing or {}).get("status"), unavailable["status"], captured_at, unavailable["reason"])
        _atomic_save(state_file, state)
        return dict(unavailable)

    missing = [role for role in REQUIRED_ROLES if role not in (quotes or {})]
    if missing:
        unavailable = {
            **dict(event), "event_id": key, "status": "UNAVAILABLE_ENTRY_BOOK",
            "reason": "MISSING_ENTRY_QUOTES:" + ",".join(missing),
        }
        state.setdefault("events", {})[key] = unavailable
        state["last_updated_at"] = captured_at.isoformat(timespec="seconds")
        _transition(ledger_file, key, (existing or {}).get("status"), unavailable["status"], captured_at, unavailable["reason"])
        _atomic_save(state_file, state)
        return dict(unavailable)

    old_status = (existing or {}).get("status") or event.get("status") or "ENTRY_DUE"
    row = {
        **dict(event),
        "event_id": key,
        "status": "ENTRY_CAPTURED",
        "expiry": structure.get("expiry"),
        "atm_strike": structure.get("atm_strike"),
        "lot_size": int(structure.get("lot_size") or 0),
        "contracts": identities,
        "entry_captured_at": captured_at.isoformat(timespec="seconds"),
    }
    _append(raw_quote_file, {
        "event_id": key,
        "capture": "ENTRY",
        "captured_at": row["entry_captured_at"],
        "quotes": quotes,
    })
    _transition(ledger_file, key, old_status, "ENTRY_CAPTURED", captured_at)
    state.setdefault("events", {})[key] = row
    state["entry_capture_count"] = int(state.get("entry_capture_count") or 0) + 1
    state["last_updated_at"] = row["entry_captured_at"]
    _atomic_save(state_file, state)
    return dict(row)


def _quote_identity_matches(expected: dict, observed: dict) -> bool:
    return (
        str(expected.get("tradingsymbol") or "") == str(observed.get("tradingsymbol") or "")
        and str(expected.get("instrument_token")) == str(observed.get("instrument_token"))
    )


def record_exit(*, state_file, ledger_file, raw_quote_file, event_id: str, quotes: dict, captured_at) -> dict:
    state = load_state(state_file)
    event = (state.get("events") or {}).get(event_id)
    if event is None:
        return {"event_id": event_id, "status": "UNAVAILABLE_UNKNOWN_EVENT"}
    if event.get("status") == "COMPLETED_RAW":
        return dict(event)
    if str(event.get("status") or "").startswith(TERMINAL_UNAVAILABLE_PREFIX):
        return dict(event)

    contracts = event.get("contracts") or {}
    for role in REQUIRED_ROLES:
        expected = contracts.get(role)
        observed = (quotes or {}).get(role)
        if expected is None or observed is None:
            event["status"] = "UNAVAILABLE_EXIT_BOOK"
            event["reason"] = f"MISSING_EXIT_QUOTE:{role}"
            event["unavailable_at"] = captured_at.isoformat(timespec="seconds")
            _transition(ledger_file, event_id, "ENTRY_CAPTURED", event["status"], captured_at, event["reason"])
            state["last_updated_at"] = event["unavailable_at"]
            _atomic_save(state_file, state)
            return dict(event)
        if not _quote_identity_matches(expected, observed):
            event["status"] = "UNAVAILABLE_CONTRACT_CHANGED"
            event["reason"] = f"CONTRACT_IDENTITY_MISMATCH:{role}"
            event["unavailable_at"] = captured_at.isoformat(timespec="seconds")
            _transition(ledger_file, event_id, "ENTRY_CAPTURED", event["status"], captured_at, event["reason"])
            state["last_updated_at"] = event["unavailable_at"]
            _atomic_save(state_file, state)
            return dict(event)

    exit_at = captured_at.isoformat(timespec="seconds")
    _append(raw_quote_file, {
        "event_id": event_id,
        "capture": "EXIT",
        "captured_at": exit_at,
        "quotes": quotes,
    })
    _transition(ledger_file, event_id, event.get("status"), "COMPLETED_RAW", captured_at)
    event["status"] = "COMPLETED_RAW"
    event["exit_captured_at"] = exit_at
    state["exit_capture_count"] = int(state.get("exit_capture_count") or 0) + 1
    state["last_updated_at"] = exit_at
    _atomic_save(state_file, state)
    return dict(event)


def public_summary(state: dict | None) -> dict:
    events = list(((state or {}).get("events") or {}).values())
    counts = Counter(str(event.get("status") or "UNKNOWN") for event in events)
    unavailable = {k: v for k, v in sorted(counts.items()) if k.startswith(TERMINAL_UNAVAILABLE_PREFIX)}
    return {
        "status": "PREREGISTERED_WAITING_EVENTS" if not events else "STAGE_D_COLLECTING",
        "events_total": len(events),
        "entry_captured": counts.get("ENTRY_CAPTURED", 0) + counts.get("EXIT_DUE", 0) + counts.get("COMPLETED_RAW", 0),
        "completed": counts.get("COMPLETED_RAW", 0),
        "target": 40,
        "unavailable_reasons": unavailable,
    }
