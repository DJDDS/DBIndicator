"""Crash/redeploy-safe persistence for V12.3 live trading state.

This module is deliberately separate from frozen V12 / Trial-25 artifacts.
It persists only live decision-support state so a Railway restart does not
erase the Focus Desk or force the full-universe observer to warm up from zero.

Files are written atomically (tmp + fsync + os.replace). Corrupt or stale files
fail closed to an empty state and never stop the scanner.
"""
from __future__ import annotations

import copy
import datetime as dt
import json
import os
import tempfile
import threading
from typing import Any

FOCUS_SCHEMA_VERSION = 1
OBSERVER_SCHEMA_VERSION = 1
OBSERVER_KEEP_MINUTES = 15
OBSERVER_CHECKPOINT_SECONDS = 30
OBSERVER_DOWNSAMPLE_SECONDS = 15

_lock = threading.RLock()


def _now_iso(now=None):
    now = now or dt.datetime.now()
    return now.isoformat(timespec="seconds")


def _parse_dt(value):
    if isinstance(value, dt.datetime):
        return value
    if not value:
        return None
    try:
        return dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None


def _same_day(value, now):
    ts = _parse_dt(value)
    if ts is None:
        return False
    if ts.tzinfo is not None and now.tzinfo is None:
        ts = ts.replace(tzinfo=None)
    elif ts.tzinfo is None and now.tzinfo is not None:
        ts = ts.replace(tzinfo=now.tzinfo)
    return ts.date() == now.date()


def _atomic_json_write(path: str, payload: dict) -> None:
    directory = os.path.dirname(path) or "."
    os.makedirs(directory, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(prefix=".v123-", suffix=".tmp", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, separators=(",", ":"), default=str)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_path, path)
    finally:
        try:
            if os.path.exists(tmp_path):
                os.unlink(tmp_path)
        except OSError:
            pass


def _read_json(path: str):
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        return None


def _focus_signature(state: dict) -> tuple:
    """Meaningful lifecycle signature; ignores constantly changing prices/ages."""
    focus = state.get("focus") or {}
    recent = state.get("recent") or []
    focus_sig = []
    for symbol, row in sorted(focus.items()):
        focus_sig.append((
            symbol,
            row.get("direction"),
            row.get("lifecycle"),
            row.get("selected_at"),
            row.get("last_state_change_at"),
            row.get("event_family"),
            row.get("trigger"),
            row.get("invalidation"),
            (row.get("vehicles") or {}).get("cash"),
            (row.get("vehicles") or {}).get("future"),
            (row.get("vehicles") or {}).get("option"),
            row.get("entry_episode_no"),
            row.get("entry_episode_open"),
            row.get("entry_episode_result"),
            row.get("locked_option_contract"),
            row.get("locked_option_strike"),
            row.get("locked_option_delta"),
        ))
    recent_sig = [
        (
            row.get("symbol"), row.get("direction"), row.get("lifecycle"),
            row.get("completed_at"), row.get("trigger"), row.get("invalidation")
        )
        for row in recent[-30:]
    ]
    swing = state.get("swing_1d") or {}
    swing_sig = (
        swing.get("trade_date"),
        swing.get("phase"),
        swing.get("last_refresh_slot"),
        tuple(sorted(
            (
                symbol,
                row.get("direction"),
                row.get("status"),
                row.get("trigger"),
                row.get("invalidation"),
                row.get("selected_slot"),
            )
            for symbol, row in (swing.get("selected") or {}).items()
        )),
    )
    continuation_sig = tuple(sorted(
        (
            symbol,
            row.get("direction"),
            row.get("lifecycle"),
            row.get("watch_started_at"),
            row.get("watch_until"),
            row.get("thesis_invalidation"),
            row.get("entry_episode_no"),
            row.get("locked_option_contract"),
        )
        for symbol, row in (state.get("continuation_watch") or {}).items()
        if isinstance(row, dict)
    ))
    forensics_sig = tuple(sorted(
        (
            symbol,
            row.get("stage"),
            row.get("reason"),
            row.get("event_family"),
            row.get("tactical_state"),
        )
        for symbol, row in (state.get("forensics") or {}).items()
        if isinstance(row, dict)
    ))
    return (
        state.get("trade_date"), tuple(focus_sig), tuple(recent_sig),
        continuation_sig, forensics_sig, swing_sig
    )


class FocusStateStore:
    """Write the Focus Desk immediately when its lifecycle meaning changes."""

    def __init__(self, path: str):
        self.path = path
        self._last_signature = None

    def load(self, *, now=None):
        now = now or dt.datetime.now()
        payload = _read_json(self.path)
        if not isinstance(payload, dict):
            return None
        if int(payload.get("schema_version") or 0) != FOCUS_SCHEMA_VERSION:
            return None
        state = payload.get("state")
        if not isinstance(state, dict):
            return None
        trade_date = state.get("trade_date")
        if trade_date and str(trade_date) != now.date().isoformat():
            return None
        with _lock:
            self._last_signature = _focus_signature(state)
        return state

    def save_if_changed(self, state: dict, *, now=None, force=False) -> bool:
        now = now or dt.datetime.now()
        state = copy.deepcopy(state or {})
        sig = _focus_signature(state)
        with _lock:
            if not force and self._last_signature == sig:
                return False
            payload = {
                "schema_version": FOCUS_SCHEMA_VERSION,
                "saved_at": _now_iso(now),
                "state": state,
            }
            _atomic_json_write(self.path, payload)
            self._last_signature = sig
        return True


def _thin_samples(samples, now):
    cutoff = now - dt.timedelta(minutes=OBSERVER_KEEP_MINUTES)
    out = []
    last_kept = None
    for row in samples or []:
        ts = _parse_dt(row.get("ts"))
        if ts is None:
            continue
        if ts.tzinfo is not None and cutoff.tzinfo is None:
            ts = ts.replace(tzinfo=None)
        elif ts.tzinfo is None and cutoff.tzinfo is not None:
            ts = ts.replace(tzinfo=cutoff.tzinfo)
        if ts < cutoff:
            continue
        if last_kept is not None and (ts - last_kept).total_seconds() < OBSERVER_DOWNSAMPLE_SECONDS:
            continue
        last_kept = ts
        out.append({
            "ts": _now_iso(ts),
            "price": row.get("price"),
            "volume": row.get("volume"),
            "high": row.get("high"),
            "low": row.get("low"),
            "prev_close": row.get("prev_close"),
        })
    return out


def save_observer_checkpoint(path: str, samples: dict, latest: dict, *, now=None) -> bool:
    now = now or dt.datetime.now()
    symbols = {}
    for symbol, rows in (samples or {}).items():
        thin = _thin_samples(rows, now)
        if thin:
            symbols[str(symbol)] = thin

    latest_compact = {}
    for symbol, tick in (latest or {}).items():
        if not isinstance(tick, dict):
            continue
        ohlc = tick.get("ohlc") or {}
        latest_compact[str(symbol)] = {
            "last_price": tick.get("last_price"),
            "volume_traded": tick.get("volume_traded"),
            "volume": tick.get("volume"),
            "ohlc": {
                "open": ohlc.get("open"),
                "high": ohlc.get("high"),
                "low": ohlc.get("low"),
                "close": ohlc.get("close"),
            },
        }

    payload = {
        "schema_version": OBSERVER_SCHEMA_VERSION,
        "saved_at": _now_iso(now),
        "trade_date": now.date().isoformat(),
        "symbols": symbols,
        "latest": latest_compact,
    }
    with _lock:
        _atomic_json_write(path, payload)
    return True


def load_observer_checkpoint(path: str, *, now=None):
    now = now or dt.datetime.now()
    payload = _read_json(path)
    if not isinstance(payload, dict):
        return None
    if int(payload.get("schema_version") or 0) != OBSERVER_SCHEMA_VERSION:
        return None
    if str(payload.get("trade_date") or "") != now.date().isoformat():
        return None
    if not _same_day(payload.get("saved_at"), now):
        return None
    return payload
