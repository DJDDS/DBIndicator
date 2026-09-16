"""Read-only operational observability for the V12/V12.1 recorders."""
from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

SLOTS = (("OPEN_STABLE", dt.time(9, 30)), ("MIDDAY", dt.time(13, 0)),
         ("PRE_CAS", dt.time(15, 10)), ("POST_CAS", dt.time(15, 37)))
GRACE_MINUTES = 7


def _read(path):
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError, TypeError):
        return {}


def _size(path):
    try:
        return Path(path).stat().st_size
    except OSError:
        return 0


def _age(now, value):
    if not value:
        return None
    try:
        prior = dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if prior.tzinfo and not now.tzinfo:
            prior = prior.replace(tzinfo=None)
        elif now.tzinfo and not prior.tzinfo:
            prior = prior.replace(tzinfo=now.tzinfo)
        return round(max(0.0, (now - prior).total_seconds()), 3)
    except (TypeError, ValueError):
        return None


def _slot_health(state, now):
    captured = state.get("captured_slots") or {}
    # Older state files may persist a list rather than a mapping.
    captured_names = set(captured if isinstance(captured, (list, tuple, set)) else captured.keys())
    out = {}
    for name, clock in SLOTS:
        due = dt.datetime.combine(now.date(), clock)
        if now.tzinfo is not None:
            due = due.replace(tzinfo=now.tzinfo)
        deadline = due + dt.timedelta(minutes=GRACE_MINUTES)
        if name in captured_names:
            status = "CAPTURED"
        elif now > deadline:
            status = "MISSED"
        else:
            status = "PENDING"
        out[name] = {"scheduled_at": due.isoformat(timespec="minutes"),
                     "deadline": deadline.isoformat(timespec="minutes"), "status": status}
    return out


def operational_health(*, v12_snapshot_file, v12_state_file, v121_root, v121_state_file,
                       backup_state_file, now, storage_mode, connection_state=None):
    """Return recorder evidence only; never mutates recorder state or data."""
    v12 = _read(v12_state_file)
    v121 = _read(v121_state_file)
    backup = _read(backup_state_file)
    day = str(v121.get("current_day") or now.date().isoformat())
    root = Path(v121_root)
    micro = root / f"{day}_micro.jsonl"
    depth = root / f"{day}_depth.jsonl"
    return {
        "observed_at": now.isoformat(timespec="seconds"),
        "storage_mode": storage_mode,
        "v12": {
            "slots": _slot_health(v12, now),
            "last_capture_at": v12.get("last_capture_at"),
            "last_successful_write_at": v12.get("last_successful_write_at"),
            "quote_error_count": int(v12.get("quote_error_count") or 0),
            "last_error": v12.get("last_error"), "last_write_error": v12.get("last_write_error"),
            "snapshot_file_bytes": _size(v12_snapshot_file),
        },
        "v121": {
            "status": v121.get("status"), "active_expiry": v121.get("active_expiry"),
            "atm_strike": v121.get("atm_strike"), "token_count": v121.get("token_count"),
            "session_last_tick_at": v121.get("session_last_tick_at"),
            "last_tick_age_seconds": _age(now, v121.get("session_last_tick_at")),
            "last_micro_write_at": v121.get("last_micro_write_at"),
            "last_depth_write_at": v121.get("last_depth_write_at"),
            "micro_snapshot_count": int(v121.get("micro_snapshot_count") or 0),
            "depth_snapshot_count": int(v121.get("depth_snapshot_count") or 0),
            "last_write_error": v121.get("last_write_error"),
            "micro_file_bytes": _size(micro), "depth_file_bytes": _size(depth),
            "micro_gzip_bytes": _size(str(micro) + ".gz"), "depth_gzip_bytes": _size(str(depth) + ".gz"),
        },
        "backup": backup or {"status": "UNKNOWN"},
        "connection": dict(connection_state or {}),
    }
