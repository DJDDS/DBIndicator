"""Forward recorder for CALL_EXECUTION_CANDIDATE_V1.

Observational only. It records the exact live bid/ask state when CALL V1 first
enters PASS, then captures executable outcomes at +1m/+3m/+5m/+10m using the
same physical contract. It never places orders and never controls Actionable.
"""
from __future__ import annotations

import datetime as dt
import json
import math
import threading
from pathlib import Path

HORIZONS = {"1m": 60, "3m": 180, "5m": 300, "10m": 600}
MAX_OUTCOME_DELAY_SECONDS = 45
MAX_EVENT_AGE_SECONDS = 15 * 60


def _finite(value):
    try:
        x = float(value)
    except (TypeError, ValueError):
        return None
    return x if math.isfinite(x) else None


def _iso(value):
    return value.isoformat(timespec="seconds") if isinstance(value, dt.datetime) else value


def _dt(value):
    if isinstance(value, dt.datetime):
        return value
    if not value:
        return None
    try:
        return dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None


def _atomic_write(path, payload):
    if not path:
        return
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, default=str, sort_keys=True, separators=(",", ":")), encoding="utf-8")
    tmp.replace(path)


def _append_jsonl(path, payload):
    if not path:
        return
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, default=str, sort_keys=True, separators=(",", ":")) + "\n")


def _empty_state():
    return {
        "version": 1,
        "open_events": {},
        "latches": {},
        "entries": 0,
        "outcomes": 0,
        "completed": 0,
        "last_entry_at": None,
        "last_outcome_at": None,
        "last_error": None,
    }


def _load_state(path):
    if not path:
        return _empty_state()
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
        if isinstance(raw, dict):
            state = _empty_state()
            state.update(raw)
            state["open_events"] = dict(state.get("open_events") or {})
            state["latches"] = dict(state.get("latches") or {})
            return state
    except (OSError, ValueError, TypeError):
        pass
    return _empty_state()


class CallV1ForwardRecorder:
    def __init__(self, ledger_file, state_file):
        self.ledger_file = str(ledger_file) if ledger_file else None
        self.state_file = str(state_file) if state_file else None
        self._lock = threading.RLock()
        self._state = _load_state(self.state_file)

    def _save(self):
        _atomic_write(self.state_file, self._state)

    def status(self):
        with self._lock:
            return {
                "status": "RECORDING_FORWARD",
                "entries": int(self._state.get("entries") or 0),
                "outcomes": int(self._state.get("outcomes") or 0),
                "completed": int(self._state.get("completed") or 0),
                "open_events": len(self._state.get("open_events") or {}),
                "last_entry_at": self._state.get("last_entry_at"),
                "last_outcome_at": self._state.get("last_outcome_at"),
                "ledger_file": Path(self.ledger_file).name if self.ledger_file else None,
                "state_file": Path(self.state_file).name if self.state_file else None,
                "controls_trading": False,
            }

    def active_subscriptions(self):
        with self._lock:
            out = []
            for event in (self._state.get("open_events") or {}).values():
                if not isinstance(event, dict):
                    continue
                symbol = str(event.get("symbol") or "")
                contract = str(event.get("contract") or "")
                if symbol and contract:
                    out.append({"symbol": symbol, "contract": contract})
            return out

    def _release_latch(self, symbol):
        latch = (self._state.get("latches") or {}).get(symbol)
        if isinstance(latch, dict) and latch.get("active"):
            latch["active"] = False
            latch["released_at"] = dt.datetime.now().isoformat(timespec="seconds")
            return True
        return False

    def observe_signal(self, *, now, symbol, spot, candidate, contract_snapshot):
        """Record one entry on a PASS transition; repeated PASS ticks are latched."""
        symbol = str(symbol or "")
        candidate = dict(candidate or {})
        passed = bool(candidate.get("pass")) and str(candidate.get("state") or "") == "PASS"
        with self._lock:
            if not passed:
                if self._release_latch(symbol):
                    self._save()
                return None

            contract = str(candidate.get("selected_contract") or "")
            if not symbol or not contract:
                return None

            # One unresolved forward episode per underlying avoids correlated
            # duplicate entries while the same PASS remains active.
            for event in (self._state.get("open_events") or {}).values():
                if isinstance(event, dict) and str(event.get("symbol") or "") == symbol:
                    latch = self._state["latches"].setdefault(symbol, {})
                    latch.update({"active": True, "contract": contract})
                    self._save()
                    return event.get("event_id")

            latch = self._state["latches"].setdefault(symbol, {})
            if latch.get("active") and str(latch.get("contract") or "") == contract:
                return latch.get("event_id")

            snap = dict(contract_snapshot or {})
            bid = _finite(snap.get("bid"))
            ask = _finite(snap.get("ask"))
            mid = _finite(snap.get("mid"))
            spread = _finite(snap.get("spread_pct"))
            entry_spot = _finite(spot)
            if bid is None or ask is None or bid <= 0 or ask < bid or entry_spot is None or entry_spot <= 0:
                return None
            if mid is None:
                mid = (bid + ask) / 2.0

            stamp = now.strftime("%Y%m%dT%H%M%S")
            event_id = f"CALLV1|{stamp}|{symbol}|{contract}"
            event = {
                "event_id": event_id,
                "lane": "CALL_EXECUTION_CANDIDATE_V1",
                "symbol": symbol,
                "direction": "Bullish",
                "contract": contract,
                "strike": _finite(snap.get("strike")),
                "expiry": snap.get("expiry"),
                "dte": snap.get("dte"),
                "probability": _finite(candidate.get("probability")),
                "threshold": _finite(candidate.get("threshold")),
                "selected_offset": candidate.get("selected_offset"),
                "entry_at": _iso(now),
                "entry_spot": entry_spot,
                "entry_bid": bid,
                "entry_ask": ask,
                "entry_mid": mid,
                "entry_spread_pct": spread,
                "entry_iv_pct": _finite(snap.get("iv_pct")),
                "entry_delta": _finite(snap.get("delta")),
                "entry_gamma": _finite(snap.get("gamma")),
                "entry_theta_per_day": _finite(snap.get("theta_per_day")),
                "entry_vega": _finite(snap.get("vega")),
                "entry_oi": _finite(snap.get("oi")),
                "entry_volume": _finite(snap.get("volume")),
                "outcomes": {},
            }
            self._state["open_events"][event_id] = event
            latch.update({"active": True, "contract": contract, "event_id": event_id, "started_at": _iso(now)})
            self._state["entries"] = int(self._state.get("entries") or 0) + 1
            self._state["last_entry_at"] = _iso(now)
            self._save()
            _append_jsonl(self.ledger_file, {"record_type": "ENTRY", **event})
            return event_id

    def _finish_event(self, event_id, now, reason):
        event = self._state.get("open_events", {}).pop(event_id, None)
        if not isinstance(event, dict):
            return
        self._state["completed"] = int(self._state.get("completed") or 0) + 1
        _append_jsonl(self.ledger_file, {
            "record_type": "COMPLETE",
            "event_id": event_id,
            "symbol": event.get("symbol"),
            "contract": event.get("contract"),
            "completed_at": _iso(now),
            "reason": reason,
            "outcomes": event.get("outcomes") or {},
        })

    def observe_market(self, *, now, symbol, contract, spot, snapshot):
        """Capture any due horizon for an open event using exit bid vs entry ask."""
        symbol = str(symbol or "")
        contract = str(contract or "")
        snap = dict(snapshot or {})
        changed = False
        with self._lock:
            open_events = list((self._state.get("open_events") or {}).items())
            for event_id, event in open_events:
                if not isinstance(event, dict):
                    continue
                if str(event.get("symbol") or "") != symbol or str(event.get("contract") or "") != contract:
                    continue
                start = _dt(event.get("entry_at"))
                if start is None:
                    self._finish_event(event_id, now, "INVALID_ENTRY_TIME")
                    changed = True
                    continue
                age = max(0.0, (now - start).total_seconds())
                outcomes = event.setdefault("outcomes", {})
                for label, seconds in HORIZONS.items():
                    if label in outcomes or age < seconds:
                        continue
                    delay = age - seconds
                    if delay > MAX_OUTCOME_DELAY_SECONDS:
                        outcomes[label] = {
                            "status": "MISSING_QUOTE",
                            "due_seconds": seconds,
                            "observed_at": _iso(now),
                            "delay_seconds": round(delay, 2),
                        }
                        self._state["outcomes"] = int(self._state.get("outcomes") or 0) + 1
                        _append_jsonl(self.ledger_file, {
                            "record_type": "OUTCOME",
                            "event_id": event_id,
                            "symbol": symbol,
                            "contract": contract,
                            "horizon": label,
                            **outcomes[label],
                        })
                        changed = True
                        continue

                    exit_bid = _finite(snap.get("bid"))
                    exit_ask = _finite(snap.get("ask"))
                    exit_mid = _finite(snap.get("mid"))
                    exit_spot = _finite(spot)
                    if exit_bid is None or exit_bid <= 0:
                        continue
                    if exit_mid is None and exit_ask is not None and exit_ask >= exit_bid:
                        exit_mid = (exit_bid + exit_ask) / 2.0
                    entry_ask = _finite(event.get("entry_ask"))
                    entry_mid = _finite(event.get("entry_mid"))
                    entry_spot = _finite(event.get("entry_spot"))
                    payload = {
                        "status": "RECORDED",
                        "due_seconds": seconds,
                        "observed_at": _iso(now),
                        "delay_seconds": round(delay, 2),
                        "exit_bid": exit_bid,
                        "exit_ask": exit_ask,
                        "exit_mid": exit_mid,
                        "exit_spread_pct": _finite(snap.get("spread_pct")),
                        "exit_iv_pct": _finite(snap.get("iv_pct")),
                        "exit_delta": _finite(snap.get("delta")),
                        "exit_oi": _finite(snap.get("oi")),
                        "exit_volume": _finite(snap.get("volume")),
                        "exit_spot": exit_spot,
                        "executable_return_pct": (
                            (exit_bid / entry_ask - 1.0) * 100.0
                            if entry_ask is not None and entry_ask > 0 else None
                        ),
                        "mid_return_pct": (
                            (exit_mid / entry_mid - 1.0) * 100.0
                            if exit_mid is not None and entry_mid is not None and entry_mid > 0 else None
                        ),
                        "underlying_return_bps": (
                            (exit_spot / entry_spot - 1.0) * 10000.0
                            if exit_spot is not None and entry_spot is not None and entry_spot > 0 else None
                        ),
                    }
                    outcomes[label] = payload
                    self._state["outcomes"] = int(self._state.get("outcomes") or 0) + 1
                    self._state["last_outcome_at"] = _iso(now)
                    _append_jsonl(self.ledger_file, {
                        "record_type": "OUTCOME",
                        "event_id": event_id,
                        "symbol": symbol,
                        "contract": contract,
                        "horizon": label,
                        **payload,
                    })
                    changed = True

                if all(label in outcomes for label in HORIZONS):
                    self._finish_event(event_id, now, "ALL_HORIZONS_CAPTURED")
                    changed = True
                elif age > MAX_EVENT_AGE_SECONDS:
                    for label, seconds in HORIZONS.items():
                        if label in outcomes:
                            continue
                        outcomes[label] = {
                            "status": "MISSING_QUOTE",
                            "due_seconds": seconds,
                            "observed_at": _iso(now),
                            "delay_seconds": round(max(0.0, age - seconds), 2),
                        }
                    self._finish_event(event_id, now, "EVENT_EXPIRED")
                    changed = True

            if changed:
                self._save()
        return changed
