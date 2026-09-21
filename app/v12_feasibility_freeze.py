"""Immutable first-10-day V12 stock-option feasibility freeze.

This module never changes the live recorder state.  It creates one auditable
snapshot of the exact first ten distinct trading days, evaluates the already
locked V12 feasibility function on that snapshot, and then becomes idempotent.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
from pathlib import Path

from . import v12_feasibility

FREEZE_DATE = "2026-09-21"
EXPECTED_TRADING_DAYS = (
    "2026-09-07",
    "2026-09-08",
    "2026-09-09",
    "2026-09-10",
    "2026-09-11",
    "2026-09-15",
    "2026-09-16",
    "2026-09-17",
    "2026-09-18",
    "2026-09-21",
)
EXPECTED_SLOTS = 39
REQUIRED_FINAL_SLOTS = ("OPEN_STABLE", "MIDDAY", "PRE_CAS", "POST_CAS")
EXPECTED_MISSING = {"2026-09-11": {"OPEN_STABLE"}}

STATE_NAME = f"v12_option_state_10d_{FREEZE_DATE}.json"
CODE_NAME = f"v12_feasibility_code_10d_{FREEZE_DATE}.py"
REPORT_NAME = f"v12_feasibility_10d_{FREEZE_DATE}.json"
MANIFEST_NAME = f"v12_feasibility_10d_{FREEZE_DATE}.sha256"


class FreezePreconditionError(RuntimeError):
    pass


class FreezeIntegrityError(RuntimeError):
    pass


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _canonical_json_bytes(value) -> bytes:
    return (json.dumps(value, sort_keys=True, indent=2, ensure_ascii=False) + "\n").encode("utf-8")


def _atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp.{os.getpid()}")
    try:
        with open(tmp, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    finally:
        try:
            if tmp.exists():
                tmp.unlink()
        except OSError:
            pass


def _captured_days(state: dict) -> tuple[str, ...]:
    captured = state.get("captured_slots") or {}
    return tuple(sorted(day for day, slots in captured.items() if slots))


def _validate_source_state(state: dict) -> None:
    captured = state.get("captured_slots") or {}
    days = _captured_days(state)
    if days != EXPECTED_TRADING_DAYS:
        raise FreezePreconditionError(
            f"first-10-day freeze requires exact days {EXPECTED_TRADING_DAYS}; got {days}"
        )
    slots = sum(len(captured.get(day) or []) for day in days)
    if slots != EXPECTED_SLOTS:
        raise FreezePreconditionError(f"expected {EXPECTED_SLOTS} captured slots; got {slots}")
    if set(captured.get(FREEZE_DATE) or []) != set(REQUIRED_FINAL_SLOTS):
        raise FreezePreconditionError(
            f"{FREEZE_DATE} must contain all four fixed slots before freezing"
        )
    for day, slots in captured.items():
        expected = set(REQUIRED_FINAL_SLOTS) - EXPECTED_MISSING.get(day, set())
        if set(slots or []) != expected:
            raise FreezePreconditionError(
                f"unexpected slot pattern on {day}: expected {sorted(expected)}, got {sorted(slots or [])}"
            )
    if int(state.get("quote_error_count") or 0) != 0:
        raise FreezePreconditionError("quote_error_count must be zero at the 10-day freeze")
    if state.get("last_error") not in (None, ""):
        raise FreezePreconditionError(f"recorder last_error is not clean: {state.get('last_error')}")
    if state.get("last_write_error") not in (None, ""):
        raise FreezePreconditionError(
            f"recorder last_write_error is not clean: {state.get('last_write_error')}"
        )
    if state.get("last_capture_status") != "CAPTURED":
        raise FreezePreconditionError(
            f"final capture must be CAPTURED; got {state.get('last_capture_status')}"
        )


def _validate_feasibility(summary: dict) -> int:
    if int(summary.get("trading_days_recorded") or 0) != 10:
        raise FreezeIntegrityError("feasibility summary must contain exactly 10 trading days")
    if int(summary.get("slots_captured") or 0) != EXPECTED_SLOTS:
        raise FreezeIntegrityError("feasibility summary slot count changed")
    if summary.get("trial25_locked") is not True:
        raise FreezeIntegrityError("Trial 25 must remain locked by the feasibility function")

    symbols = list(summary.get("tradeable_symbol_list") or [])
    tradeable = int(summary.get("tradeable_symbols") or 0)
    if tradeable != len(symbols) or len(symbols) != len(set(symbols)):
        raise FreezeIntegrityError("tradeable symbol count/list mismatch")

    metrics = summary.get("symbol_metrics") or {}
    universe_n = len(metrics)
    if universe_n <= 0:
        raise FreezeIntegrityError("empty evaluated symbol universe")

    thresholds = summary.get("symbols_below_spread_pct") or {}
    counts = [int(thresholds.get(str(x)) or 0) for x in (1, 2, 4, 5)]
    if any(value < 0 or value > universe_n for value in counts):
        raise FreezeIntegrityError("spread threshold count exceeds evaluated universe")
    if counts != sorted(counts):
        raise FreezeIntegrityError("spread threshold counts are not monotonic")
    if counts[2] != tradeable:
        raise FreezeIntegrityError("<=4% threshold count must equal tradeable symbol count")

    gate = summary.get("gate") or {}
    expected_gate = {
        "minimum_trading_days": 10,
        "minimum_tradeable_symbols": 20,
        "minimum_two_sided_coverage_pct": 70.0,
        "maximum_median_straddle_spread_pct": 4.0,
    }
    if gate != expected_gate:
        raise FreezeIntegrityError(f"feasibility gate changed: {gate}")

    expected_status = (
        "STOCK OPTIONS PRACTICALLY TESTABLE"
        if tradeable >= 20
        else "STOCK OPTION LIQUIDITY GATE NOT MET"
    )
    if summary.get("status") != expected_status:
        raise FreezeIntegrityError(
            f"status/count mismatch: {summary.get('status')} with {tradeable} tradeable symbols"
        )
    return universe_n


def _paths(storage_root: str | os.PathLike) -> dict[str, Path]:
    root = Path(storage_root) / "research_freezes"
    return {
        "root": root,
        "state": root / STATE_NAME,
        "code": root / CODE_NAME,
        "report": root / REPORT_NAME,
        "manifest": root / MANIFEST_NAME,
    }


def _manifest_map(data: bytes) -> dict[str, str]:
    out = {}
    for raw in data.decode("utf-8").splitlines():
        if not raw.strip():
            continue
        digest, name = raw.split("  ", 1)
        out[name.strip()] = digest.strip()
    return out


def _validate_existing(paths: dict[str, Path]) -> dict:
    required = ("state", "code", "report", "manifest")
    if not all(paths[key].exists() for key in required):
        raise FreezeIntegrityError("partial feasibility freeze exists; refusing to overwrite it")

    state_bytes = paths["state"].read_bytes()
    code_bytes = paths["code"].read_bytes()
    report_bytes = paths["report"].read_bytes()
    manifest = _manifest_map(paths["manifest"].read_bytes())
    actual = {
        paths["state"].name: _sha256(state_bytes),
        paths["code"].name: _sha256(code_bytes),
        paths["report"].name: _sha256(report_bytes),
    }
    if manifest != actual:
        raise FreezeIntegrityError("frozen feasibility manifest/hash mismatch")

    payload = json.loads(report_bytes)
    meta = payload.get("metadata") or {}
    if meta.get("source_state_sha256") != actual[paths["state"].name]:
        raise FreezeIntegrityError("frozen source-state hash mismatch")
    if meta.get("feasibility_code_sha256") != actual[paths["code"].name]:
        raise FreezeIntegrityError("frozen feasibility-code hash mismatch")
    universe_n = _validate_feasibility(payload.get("feasibility") or {})
    return {
        "status": "EXISTING_VALID_FREEZE",
        "paths": {key: str(paths[key]) for key in required},
        "hashes": actual,
        "evaluated_universe": universe_n,
        "feasibility": payload["feasibility"],
    }


def create_10d_feasibility_freeze(
    state_file: str | os.PathLike,
    storage_root: str | os.PathLike,
    *,
    feasibility_source_file: str | os.PathLike | None = None,
    deployed_commit: str | None = None,
    generated_at: dt.datetime | None = None,
) -> dict:
    """Create (once) and verify the exact first-10-day feasibility freeze."""
    paths = _paths(storage_root)
    if any(paths[key].exists() for key in ("state", "code", "report", "manifest")):
        return _validate_existing(paths)

    source_path = Path(state_file)
    state_bytes_before = source_path.read_bytes()
    state = json.loads(state_bytes_before)
    _validate_source_state(state)

    code_path = Path(feasibility_source_file or Path(v12_feasibility.__file__))
    code_bytes = code_path.read_bytes()
    source_hash = _sha256(state_bytes_before)
    code_hash = _sha256(code_bytes)

    _atomic_write(paths["state"], state_bytes_before)
    _atomic_write(paths["code"], code_bytes)

    frozen_state = json.loads(paths["state"].read_bytes())
    summary = v12_feasibility.summarize_feasibility(frozen_state)
    universe_n = _validate_feasibility(summary)

    now = generated_at or dt.datetime.now(dt.timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=dt.timezone.utc)
    metadata = {
        "freeze_date": FREEZE_DATE,
        "generated_at_utc": now.astimezone(dt.timezone.utc).isoformat(timespec="seconds"),
        "deployed_commit": deployed_commit or os.getenv("RAILWAY_GIT_COMMIT_SHA") or "UNKNOWN",
        "source_state_sha256": source_hash,
        "feasibility_code_sha256": code_hash,
        "source_last_capture_at": frozen_state.get("last_capture_at"),
        "source_last_successful_write_at": frozen_state.get("last_successful_write_at"),
        "evaluated_universe": universe_n,
        "contract": "FIRST_10_DISTINCT_TRADING_DAYS_ONLY",
    }
    report_bytes = _canonical_json_bytes({"metadata": metadata, "feasibility": summary})
    _atomic_write(paths["report"], report_bytes)

    hashes = {
        paths["state"].name: _sha256(paths["state"].read_bytes()),
        paths["code"].name: _sha256(paths["code"].read_bytes()),
        paths["report"].name: _sha256(paths["report"].read_bytes()),
    }
    manifest_bytes = "".join(
        f"{digest}  {name}\n" for name, digest in sorted(hashes.items())
    ).encode("utf-8")
    _atomic_write(paths["manifest"], manifest_bytes)

    # Last check: freezing is not allowed to mutate the live recorder state.
    if _sha256(source_path.read_bytes()) != source_hash:
        raise FreezeIntegrityError("live recorder state changed while the freeze was being created")

    verified = _validate_existing(paths)
    verified["status"] = "CREATED_AND_VERIFIED"
    return verified


def maybe_freeze_10d(
    state_file: str | os.PathLike,
    storage_root: str | os.PathLike,
    **kwargs,
) -> dict:
    """Fail closed without ever blocking the live scanner startup."""
    paths = _paths(storage_root)
    if any(paths[key].exists() for key in ("state", "code", "report", "manifest")):
        return _validate_existing(paths)
    try:
        return create_10d_feasibility_freeze(state_file, storage_root, **kwargs)
    except (OSError, ValueError, FreezePreconditionError) as exc:
        return {"status": "NOT_FROZEN", "reason": str(exc)}
