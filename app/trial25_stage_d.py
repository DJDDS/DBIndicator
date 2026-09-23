"""Trial-25 Stage-D no-peeking calibration gate.

Before 40 completed eligible events, this module exposes counts only. At the
first >=40 boundary it transiently reconstructs exactly the deterministic first
40 executable event outcomes, stores only their sample standard deviation and
the preregistered Stage-C sample size, and discards event-level efficacy.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import math
import statistics
from collections import Counter
from pathlib import Path

from . import trial25_execution as execution


STAGE_D_TARGET = 40
Z_ALPHA = 1.644854
Z_BETA = 0.841621
CONFIRMATORY_EFFECT_PCT = 4.0


class StageDIntegrityError(RuntimeError):
    pass


def stage_c_required_n(sigma_d: float) -> int:
    sigma = float(sigma_d)
    if not math.isfinite(sigma) or sigma < 0:
        raise ValueError("sigma_d must be finite and non-negative")
    raw = math.ceil((((Z_ALPHA + Z_BETA) * sigma) / CONFIRMATORY_EFFECT_PCT) ** 2)
    return max(40, int(raw))


def _completed_events(state: dict | None) -> list[dict]:
    rows = [
        dict(event)
        for event in ((state or {}).get("events") or {}).values()
        if event.get("status") == "COMPLETED_RAW"
    ]
    return sorted(rows, key=lambda e: (str(e.get("exit_captured_at") or ""), str(e.get("event_id") or "")))


def safe_stage_d_summary(state: dict | None) -> dict:
    events = list(((state or {}).get("events") or {}).values())
    completed = len(_completed_events(state))
    reasons = Counter(
        str(event.get("status"))
        for event in events
        if str(event.get("status") or "").startswith("UNAVAILABLE_")
    )
    return {
        "status": "STAGE_D_COLLECTING" if completed < STAGE_D_TARGET else "STAGE_D_CALIBRATION_READY",
        "completed": completed,
        "target": STAGE_D_TARGET,
        "unavailable_reasons": dict(sorted(reasons.items())),
    }


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _atomic_write(path, data: bytes) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_bytes(data)
    tmp.replace(p)


def _load_raw(raw_quote_file) -> dict[str, dict[str, dict]]:
    by_event: dict[str, dict[str, dict]] = {}
    try:
        lines = Path(raw_quote_file).read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise StageDIntegrityError(f"raw quote evidence unavailable: {exc}") from exc
    for line in lines:
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except (TypeError, ValueError) as exc:
            raise StageDIntegrityError("raw quote ledger contains invalid JSON") from exc
        event_id = str(row.get("event_id") or "")
        capture = str(row.get("capture") or "")
        if not event_id or capture not in ("ENTRY", "EXIT"):
            continue
        bucket = by_event.setdefault(event_id, {})
        if capture in bucket:
            raise StageDIntegrityError(f"duplicate {capture} raw evidence for {event_id}")
        bucket[capture] = row
    return by_event


def _price(quote: dict, key: str) -> float:
    try:
        value = float(quote[key])
    except (KeyError, TypeError, ValueError) as exc:
        raise StageDIntegrityError(f"missing executable price {key}") from exc
    if not math.isfinite(value) or value <= 0:
        raise StageDIntegrityError(f"invalid executable price {key}")
    return value


def _event_return(event: dict, evidence: dict) -> float:
    entry = (evidence.get("ENTRY") or {}).get("quotes") or {}
    exitq = (evidence.get("EXIT") or {}).get("quotes") or {}
    lot = int(event.get("lot_size") or 0)
    if lot <= 0:
        raise StageDIntegrityError(f"invalid lot size for {event.get('event_id')}")
    for role in ("atm_call", "atm_put", "lower_put", "upper_call"):
        if role not in entry or role not in exitq:
            raise StageDIntegrityError(f"missing raw leg {role} for {event.get('event_id')}")

    entry_atm_received = (
        _price(entry["atm_call"], "best_bid") + _price(entry["atm_put"], "best_bid")
    ) * lot
    entry_wings_paid = (
        _price(entry["lower_put"], "best_ask") + _price(entry["upper_call"], "best_ask")
    ) * lot
    exit_atm_paid = (
        _price(exitq["atm_call"], "best_ask") + _price(exitq["atm_put"], "best_ask")
    ) * lot
    exit_wings_received = (
        _price(exitq["lower_put"], "best_bid") + _price(exitq["upper_call"], "best_bid")
    ) * lot
    gross = entry_atm_received - entry_wings_paid - exit_atm_paid + exit_wings_received

    fills = [
        {"side": "SELL", "price": _price(entry["atm_call"], "best_bid"), "quantity": lot},
        {"side": "SELL", "price": _price(entry["atm_put"], "best_bid"), "quantity": lot},
        {"side": "BUY", "price": _price(entry["lower_put"], "best_ask"), "quantity": lot},
        {"side": "BUY", "price": _price(entry["upper_call"], "best_ask"), "quantity": lot},
        {"side": "BUY", "price": _price(exitq["atm_call"], "best_ask"), "quantity": lot},
        {"side": "BUY", "price": _price(exitq["atm_put"], "best_ask"), "quantity": lot},
        {"side": "SELL", "price": _price(exitq["lower_put"], "best_bid"), "quantity": lot},
        {"side": "SELL", "price": _price(exitq["upper_call"], "best_bid"), "quantity": lot},
    ]
    charges = execution.calculate_option_charges(fills)["total"]
    net = gross - float(charges)
    if entry_atm_received <= 0:
        raise StageDIntegrityError("non-positive ATM premium denominator")
    return 100.0 * net / entry_atm_received


def _verify_existing(calibration_file, hash_file) -> dict:
    cal_path, hash_path = Path(calibration_file), Path(hash_file)
    if not cal_path.exists() and not hash_path.exists():
        return {}
    if not cal_path.exists() or not hash_path.exists():
        raise StageDIntegrityError("incomplete Stage-D freeze artifact")
    data = cal_path.read_bytes()
    expected = hash_path.read_text(encoding="utf-8").strip().split()[0]
    actual = _sha256_bytes(data)
    if expected != actual:
        raise StageDIntegrityError("Stage-D calibration hash mismatch")
    try:
        report = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise StageDIntegrityError("Stage-D calibration JSON invalid") from exc
    report["status"] = "EXISTING_VALID_FREEZE"
    return report


def maybe_freeze_calibration(
    state: dict,
    raw_quote_file,
    calibration_file,
    hash_file,
    *,
    code_files: list[str] | None = None,
    now: dt.datetime | None = None,
) -> dict:
    existing = _verify_existing(calibration_file, hash_file)
    if existing:
        return existing

    completed = _completed_events(state)
    if len(completed) < STAGE_D_TARGET:
        return {"status": "WAITING_FOR_40", "completed": len(completed), "target": STAGE_D_TARGET}

    selected = completed[:STAGE_D_TARGET]
    raw = _load_raw(raw_quote_file)
    values = []
    for event in selected:
        eid = str(event.get("event_id") or "")
        evidence = raw.get(eid)
        if evidence is None or "ENTRY" not in evidence or "EXIT" not in evidence:
            raise StageDIntegrityError(f"incomplete raw evidence for {eid}")
        values.append(_event_return(event, evidence))
    sigma = statistics.stdev(values)
    required_n = stage_c_required_n(sigma)

    now = now or dt.datetime.now(dt.timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=dt.timezone.utc)
    stage_code = Path(__file__).read_bytes()
    execution_code = Path(execution.__file__).read_bytes()
    extra_hashes = {}
    for name in code_files or []:
        p = Path(name)
        extra_hashes[str(p)] = _sha256_bytes(p.read_bytes())
    report = {
        "status": "FROZEN_STAGE_D",
        "event_ids": [str(event.get("event_id")) for event in selected],
        "sigma_d": round(float(sigma), 8),
        "stage_c_required_n": int(required_n),
        "fee_model_version": execution.FEE_MODEL_VERSION,
        "trial25_stage_d_code_sha256": _sha256_bytes(stage_code),
        "trial25_execution_code_sha256": _sha256_bytes(execution_code),
        "extra_code_sha256": extra_hashes,
        "frozen_at_utc": now.astimezone(dt.timezone.utc).isoformat(timespec="seconds"),
    }
    data = json.dumps(report, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    digest = _sha256_bytes(data)
    _atomic_write(calibration_file, data)
    _atomic_write(hash_file, f"{digest}  {Path(calibration_file).name}\n".encode("utf-8"))
    return report
