"""Point-in-time NSE earnings-calendar recorder for V12.0.

Only explicit financial-results board meetings are admitted. The state keeps
what was known when and the JSONL ledger is append-only, so later changes to a
meeting date cannot silently rewrite the research history.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import re
from pathlib import Path
from typing import Iterable

import pandas as pd

NSE_BOARD_MEETINGS_URL = "https://www.nseindia.com/api/corporate-board-meetings"
RESULT_TOKENS = (
    "financial result",
    "financial results",
    "quarterly result",
    "quarterly results",
    "audited result",
    "audited results",
    "unaudited result",
    "unaudited results",
)


def _rows(payload):
    if isinstance(payload, dict):
        return payload.get("data") or payload.get("records") or payload.get("results") or []
    return payload or []


def _parse_timestamp(value):
    if value is None or str(value).strip() in {"", "-", "None", "nan"}:
        return None
    text = str(value).strip()
    # NSE returns both DD-MM-YYYY display dates and ISO timestamps.  Passing
    # dayfirst=True to an ISO date silently swaps month/day for dates such as
    # 2026-09-08, so detect ISO first rather than relying on pandas guessing.
    iso_like = bool(re.match(r"^\d{4}-\d{2}-\d{2}(?:[T\s]|$)", text))
    ts = pd.to_datetime(text, errors="coerce", dayfirst=not iso_like)
    if pd.isna(ts):
        return None
    if getattr(ts, "tzinfo", None) is not None:
        ts = ts.tz_localize(None)
    return pd.Timestamp(ts)


def _parse_date(value):
    ts = _parse_timestamp(value)
    return ts.date() if ts is not None else None


def _parse_dt(value):
    ts = _parse_timestamp(value)
    return ts.to_pydatetime().isoformat(timespec="seconds") if ts is not None else None


def _fingerprint(row: dict) -> str:
    canonical = json.dumps(row, sort_keys=True, default=str, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _financial_purpose(row: dict) -> bool:
    text = " ".join(
        str(row.get(key) or "")
        for key in ("purpose", "bm_purpose", "details", "bm_desc", "description")
    ).lower()
    return any(token in text for token in RESULT_TOKENS)


def parse_bulk_board_meetings(payload, *, fno_symbols: set[str] | None = None) -> list[dict]:
    allowed = {str(symbol).strip().upper() for symbol in (fno_symbols or set()) if str(symbol).strip()} if fno_symbols is not None else None
    by_symbol: dict[str, dict] = {}
    for raw in _rows(payload):
        if not isinstance(raw, dict) or not _financial_purpose(raw):
            continue
        symbol = str(raw.get("symbol") or raw.get("bm_symbol") or raw.get("sm_name") or "").strip().upper()
        if not symbol or (allowed is not None and symbol not in allowed):
            continue
        meeting_date = _parse_date(raw.get("meetingDate") or raw.get("meeting_date") or raw.get("bm_date") or raw.get("date"))
        if meeting_date is None:
            continue
        purpose = str(raw.get("purpose") or raw.get("bm_purpose") or raw.get("details") or "").strip()
        details = str(raw.get("details") or raw.get("bm_desc") or raw.get("description") or "").strip()
        broadcast = _parse_dt(raw.get("broadcastDate") or raw.get("broadCastDate") or raw.get("broadcast_date") or raw.get("timestamp"))
        normalized = {
            "symbol": symbol,
            "meeting_date": meeting_date.isoformat(),
            "purpose": purpose,
            "details": details,
            "broadcast_at": broadcast,
        }
        normalized["source_fingerprint"] = _fingerprint(normalized)
        prior = by_symbol.get(symbol)
        if prior is None or normalized["meeting_date"] < prior["meeting_date"]:
            by_symbol[symbol] = normalized
    return [by_symbol[symbol] for symbol in sorted(by_symbol)]


def _load_state(path: str | os.PathLike) -> dict:
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
        if isinstance(raw, dict) and isinstance(raw.get("events"), dict):
            return raw
    except (OSError, ValueError, TypeError):
        pass
    return {"status": "EMPTY", "events": {}, "last_refresh_at": None}


def _save_state(path: str | os.PathLike, state: dict) -> None:
    path = Path(path)
    tmp = path.with_suffix(path.suffix + ".tmp")
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp.write_text(json.dumps(state, sort_keys=True, default=str, separators=(",", ":")), encoding="utf-8")
    tmp.replace(path)


def _append_ledger(path: str | os.PathLike, record: dict) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, sort_keys=True, default=str, separators=(",", ":")) + "\n")


def record_calendar_observation(
    events: Iterable[dict],
    *,
    now: dt.datetime,
    ledger_file: str | os.PathLike,
    state_file: str | os.PathLike,
) -> dict:
    state = _load_state(state_file)
    current = {str(event.get("symbol")): dict(event) for event in (events or []) if event.get("symbol")}
    old_events = state.get("events") or {}
    now_iso = now.isoformat(timespec="seconds")

    for symbol, event in current.items():
        old = old_events.get(symbol)
        if old is None:
            fresh = dict(event)
            fresh.update({
                "first_seen_at": now_iso,
                "last_seen_at": now_iso,
                "last_changed_at": now_iso,
                "state": "ACTIVE",
            })
            old_events[symbol] = fresh
            _append_ledger(ledger_file, {**fresh, "observed_at": now_iso, "change_type": "FIRST_SEEN"})
            continue

        changed = any(
            old.get(key) != event.get(key)
            for key in ("meeting_date", "purpose", "details", "broadcast_at", "source_fingerprint")
        )
        if changed:
            prior_date = old.get("meeting_date")
            first_seen = old.get("first_seen_at") or now_iso
            revised = dict(event)
            revised.update({
                "first_seen_at": first_seen,
                "last_seen_at": now_iso,
                "last_changed_at": now_iso,
                "state": "REVISED",
            })
            old_events[symbol] = revised
            _append_ledger(ledger_file, {**revised, "observed_at": now_iso, "change_type": "REVISED", "previous_meeting_date": prior_date})
        else:
            old["last_seen_at"] = now_iso
            if old.get("state") == "REMOVED":
                old["state"] = "ACTIVE"
                old["last_changed_at"] = now_iso
                _append_ledger(ledger_file, {**old, "observed_at": now_iso, "change_type": "RESTORED"})

    # Preserve disappeared future rows instead of deleting them. A missing row
    # is evidence that the feed changed, not proof of a cancellation reason.
    for symbol, old in list(old_events.items()):
        if symbol in current or old.get("state") == "REMOVED":
            continue
        meeting_date = _parse_date(old.get("meeting_date"))
        if meeting_date is not None and meeting_date >= now.date():
            old["state"] = "REMOVED"
            old["last_changed_at"] = now_iso
            _append_ledger(ledger_file, {**old, "observed_at": now_iso, "change_type": "REMOVED"})

    state.update({
        "status": "OK",
        "events": old_events,
        "last_refresh_at": now_iso,
        "active_count": sum(event.get("state") in ("ACTIVE", "REVISED") for event in old_events.values()),
    })
    _save_state(state_file, state)
    return state


def fetch_upcoming_earnings(session, symbols: set[str], start: dt.date, end: dt.date, *, timeout: int = 25) -> dict:
    try:
        try:
            session.headers.update({
                "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/122 Safari/537.36",
                "Accept": "application/json,text/plain,*/*",
                "Referer": "https://www.nseindia.com/companies-listing/corporate-filings-board-meetings",
            })
        except Exception:
            pass
        # NSE commonly requires a cookie-bearing landing-page request before
        # its corporate-filings APIs will answer reliably. Warming is best
        # effort; the API request below remains the authoritative operation.
        try:
            session.get("https://www.nseindia.com/", timeout=int(timeout))
        except Exception:
            pass
        response = session.get(
            NSE_BOARD_MEETINGS_URL,
            params={
                "index": "equities",
                "fno": "true",
                "from_date": start.strftime("%d-%m-%Y"),
                "to_date": end.strftime("%d-%m-%Y"),
            },
            timeout=int(timeout),
        )
        if hasattr(response, "raise_for_status"):
            response.raise_for_status()
        payload = response.json()
        return {"status": "OK", "events": parse_bulk_board_meetings(payload, fno_symbols=symbols), "error": None}
    except Exception as exc:  # noqa: BLE001 - a missing calendar must fail closed, not kill scanning
        return {"status": "UNAVAILABLE", "events": [], "error": str(exc)}


def upcoming_earnings_symbols(state: dict | None, today: dt.date, *, days: int = 7) -> set[str]:
    state = state or {}
    end = today + dt.timedelta(days=max(0, int(days)))
    out = set()
    for symbol, event in (state.get("events") or {}).items():
        if event.get("state") not in ("ACTIVE", "REVISED"):
            continue
        meeting = _parse_date(event.get("meeting_date"))
        if meeting is not None and today <= meeting <= end:
            out.add(str(symbol))
    return out
