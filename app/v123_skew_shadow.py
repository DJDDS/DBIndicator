"""Cross-sectional ATM call-vs-put IV skew shadow research.

This module implements the 30-Sep-2026 spotting-book hypothesis as an
observation-only scorer. It is deliberately separate from Math Recorder,
CALL_EXECUTION_CANDIDATE_V1, Actionable, and broker execution.

Primary shadow setup:
- 09:30 and 13:00 fixed V12 option-recorder slots.
- Skew = ATM CE mid IV - ATM PE mid IV.
- If nearest expiry has <=3 DTE, prefer the second live expiry when available.
- Require two-sided CE/PE quotes, each spread <=10%, and no explicit stale quote.
- Rank all eligible F&O names cross-sectionally.
- Top 5% = bullish extreme; bottom 5% = bearish extreme.
- Market filter: equal-weight average F&O spot move since prior PRE_CAS (15:10)
  must agree with the side.
- PRE_CAS closes the same-day shadow outcomes.
- No orders, alerts, Actionable changes, or option-selection changes.
"""
from __future__ import annotations

import datetime as dt
import json
import logging
import math
from pathlib import Path

ENTRY_SLOTS = {"OPEN_STABLE": "09:30", "MIDDAY": "13:00"}
EXIT_SLOT = "PRE_CAS"
EXTREME_FRACTION = 0.05
MAX_LEG_SPREAD_PCT = 10.0
MIN_BASELINE_COVERAGE = 0.50

log = logging.getLogger(__name__)


def _finite(value):
    try:
        x = float(value)
    except (TypeError, ValueError):
        return None
    return x if math.isfinite(x) else None


def _atomic_write(path, payload):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(
        json.dumps(payload, default=str, sort_keys=True, separators=(",", ":")),
        encoding="utf-8",
    )
    tmp.replace(path)


def _append_jsonl(path, payload):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(
            json.dumps(payload, default=str, sort_keys=True, separators=(",", ":")) + "\n"
        )


def _empty_state():
    return {
        "version": 1,
        "status": "WAITING_FOR_SLOT",
        "controls_trading": False,
        "current": None,
        "open_batches": {},
        "processed_batch_ids": [],
        "completed_batches": 0,
        "completed_signals": 0,
        "last_signal_at": None,
        "last_outcome_at": None,
        "last_error": None,
    }


def load_state(path):
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
        if isinstance(raw, dict):
            out = _empty_state()
            out.update(raw)
            out["open_batches"] = dict(out.get("open_batches") or {})
            out["processed_batch_ids"] = list(out.get("processed_batch_ids") or [])
            return out
    except (OSError, ValueError, TypeError):
        pass
    return _empty_state()


def shadow_status(path):
    state = load_state(path)
    current = dict(state.get("current") or {})
    return {
        "status": state.get("status") or "WAITING_FOR_SLOT",
        "controls_trading": False,
        "research_warning": "RESEARCH IN PROGRESS — 17-session hypothesis — NOT VALIDATED",
        "current": current,
        "open_batches": len(state.get("open_batches") or {}),
        "completed_batches": int(state.get("completed_batches") or 0),
        "completed_signals": int(state.get("completed_signals") or 0),
        "last_signal_at": state.get("last_signal_at"),
        "last_outcome_at": state.get("last_outcome_at"),
        "last_error": state.get("last_error"),
    }


def _iter_jsonl_reverse(path, chunk_size=1 << 16):
    """Yield parsed JSON objects from a JSONL file, newest first."""
    path = Path(path)
    if not path.exists() or not path.is_file() or path.stat().st_size <= 0:
        return
    with path.open("rb") as handle:
        handle.seek(0, 2)
        pos = handle.tell()
        carry = b""
        while pos > 0:
            take = min(chunk_size, pos)
            pos -= take
            handle.seek(pos)
            block = handle.read(take) + carry
            lines = block.split(b"\n")
            carry = lines[0]
            for raw in reversed(lines[1:]):
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    obj = json.loads(raw.decode("utf-8"))
                except (UnicodeDecodeError, ValueError):
                    continue
                if isinstance(obj, dict):
                    yield obj
        raw = carry.strip()
        if raw:
            try:
                obj = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, ValueError):
                obj = None
            if isinstance(obj, dict):
                yield obj


def _latest_slot_record(snapshot_file, day, slot):
    day_s = day.isoformat() if isinstance(day, dt.date) else str(day)
    for record in _iter_jsonl_reverse(snapshot_file):
        if record.get("record_type") != "V12_OPTION_SLOT":
            continue
        if str(record.get("date") or "") == day_s and str(record.get("slot") or "") == slot:
            return record
        # Once reverse scan has crossed into an older date, the desired current
        # slot cannot appear later.
        rec_date = str(record.get("date") or "")
        if rec_date and rec_date < day_s:
            break
    return None


def _previous_pre_cas(snapshot_file, day):
    day_s = day.isoformat() if isinstance(day, dt.date) else str(day)
    for record in _iter_jsonl_reverse(snapshot_file):
        if record.get("record_type") != "V12_OPTION_SLOT":
            continue
        rec_date = str(record.get("date") or "")
        if rec_date >= day_s:
            continue
        if str(record.get("slot") or "") == EXIT_SLOT:
            return record
    return None


def _spot_map(record):
    out = {}
    for snap in (record or {}).get("broad_contracts") or []:
        symbol = str((snap or {}).get("underlying") or "")
        spot = _finite((snap or {}).get("spot"))
        if symbol and spot is not None and spot > 0:
            out[symbol] = spot
    return out


def _eligible_rows(record):
    grouped = {}
    for snap in (record or {}).get("broad_contracts") or []:
        symbol = str((snap or {}).get("underlying") or "")
        expiry = str((snap or {}).get("expiry") or "")
        typ = str((snap or {}).get("type") or "")
        if not symbol or not expiry or typ not in ("CE", "PE"):
            continue
        grouped.setdefault(symbol, {}).setdefault(expiry, {})[typ] = dict(snap)

    rows = []
    for symbol, expiries in grouped.items():
        expiry_keys = sorted(expiries)
        if not expiry_keys:
            continue

        chosen = expiry_keys[0]
        expiry_selection = "NEAREST_EXPIRY"
        near_pair = expiries[chosen]
        near_dte = _finite((near_pair.get("CE") or near_pair.get("PE") or {}).get("dte"))
        if near_dte is not None and near_dte <= 3 and len(expiry_keys) >= 2:
            chosen = expiry_keys[1]
            expiry_selection = "NEXT_EXPIRY_DTE_LE_3"

        pair = expiries.get(chosen) or {}
        ce, pe = pair.get("CE") or {}, pair.get("PE") or {}
        if not ce or not pe:
            continue

        ce_bid, ce_ask = _finite(ce.get("best_bid")), _finite(ce.get("best_ask"))
        pe_bid, pe_ask = _finite(pe.get("best_bid")), _finite(pe.get("best_ask"))
        ce_spread, pe_spread = _finite(ce.get("spread_pct")), _finite(pe.get("spread_pct"))
        ce_iv, pe_iv = _finite(ce.get("mid_iv_pct")), _finite(pe.get("mid_iv_pct"))
        spot = _finite(ce.get("spot")) or _finite(pe.get("spot"))
        if None in (ce_bid, ce_ask, pe_bid, pe_ask, ce_spread, pe_spread, ce_iv, pe_iv, spot):
            continue
        if ce_bid <= 0 or pe_bid <= 0 or ce_ask < ce_bid or pe_ask < pe_bid:
            continue
        if ce_spread > MAX_LEG_SPREAD_PCT or pe_spread > MAX_LEG_SPREAD_PCT:
            continue
        if ce.get("quote_stale") is True or pe.get("quote_stale") is True:
            continue
        if ce.get("stale") is True or pe.get("stale") is True:
            continue

        ce_buy = sum(int(x.get("quantity") or 0) for x in ((ce.get("depth") or {}).get("buy") or []))
        ce_sell = sum(int(x.get("quantity") or 0) for x in ((ce.get("depth") or {}).get("sell") or []))
        pe_buy = sum(int(x.get("quantity") or 0) for x in ((pe.get("depth") or {}).get("buy") or []))
        pe_sell = sum(int(x.get("quantity") or 0) for x in ((pe.get("depth") or {}).get("sell") or []))

        rows.append({
            "symbol": symbol,
            "expiry": chosen,
            "expiry_selection": expiry_selection,
            "dte": int(_finite(ce.get("dte")) or _finite(pe.get("dte")) or 0),
            "strike": _finite(ce.get("strike")) or _finite(pe.get("strike")),
            "spot": spot,
            "call_iv_pct": ce_iv,
            "put_iv_pct": pe_iv,
            "skew_pct_points": ce_iv - pe_iv,
            "call_spread_pct": ce_spread,
            "put_spread_pct": pe_spread,
            "call_contract": ce.get("tradingsymbol"),
            "put_contract": pe.get("tradingsymbol"),
            "call_bid": ce_bid,
            "call_ask": ce_ask,
            "put_bid": pe_bid,
            "put_ask": pe_ask,
            "call_depth_buy_qty": ce_buy,
            "call_depth_sell_qty": ce_sell,
            "put_depth_buy_qty": pe_buy,
            "put_depth_sell_qty": pe_sell,
        })
    return rows


def _market_context(current_rows, previous_record):
    prev_spots = _spot_map(previous_record)
    current_spots = {r["symbol"]: r["spot"] for r in current_rows if _finite(r.get("spot"))}
    returns = []
    for symbol, spot in current_spots.items():
        prev = _finite(prev_spots.get(symbol))
        if prev is None or prev <= 0:
            continue
        returns.append((spot / prev - 1.0) * 100.0)

    required = max(20, int(math.ceil(len(current_spots) * MIN_BASELINE_COVERAGE)))
    if len(returns) < required:
        return {
            "status": "INSUFFICIENT_PRE_CAS_BASELINE",
            "market_move_pct": None,
            "market_direction": "Neutral",
            "coverage": len(returns),
            "required": required,
        }

    market_move = sum(returns) / len(returns)
    direction = "Bullish" if market_move > 0 else ("Bearish" if market_move < 0 else "Neutral")
    return {
        "status": "OK",
        "market_move_pct": market_move,
        "market_direction": direction,
        "coverage": len(returns),
        "required": required,
    }


def _rank_and_filter(rows, market):
    ranked = sorted(rows, key=lambda r: (r["skew_pct_points"], r["symbol"]))
    n = len(ranked)
    if not n:
        return [], [], []
    pick_n = max(1, int(math.ceil(n * EXTREME_FRACTION)))

    for idx, row in enumerate(ranked):
        row["rank_ascending"] = idx + 1
        row["rank_percentile"] = (idx + 1) / n

    bottom = [dict(x, side="Bearish") for x in ranked[:pick_n]]
    top = [dict(x, side="Bullish") for x in ranked[-pick_n:]]
    direction = market.get("market_direction")
    selected = top if direction == "Bullish" else (bottom if direction == "Bearish" else [])
    return top, bottom, selected


def _parse_dt(value):
    if isinstance(value, dt.datetime):
        return value
    if not value:
        return None
    try:
        return dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None


def _live_spot_map(rows):
    out = {}
    for row in rows or []:
        symbol = str((row or {}).get("symbol") or "")
        price = (
            _finite((row or {}).get("close"))
            or _finite((row or {}).get("last_price"))
            or _finite((row or {}).get("ltp"))
            or _finite((row or {}).get("price"))
        )
        if symbol and price is not None and price > 0:
            out[symbol] = price
    return out


def _observer_context_map(rows):
    out = {}
    for row in rows or []:
        symbol = str((row or {}).get("symbol") or "")
        if not symbol:
            continue
        live_price = (
            _finite((row or {}).get("live_price"))
            or _finite((row or {}).get("price"))
            or _finite((row or {}).get("close"))
            or _finite((row or {}).get("last_price"))
        )
        p_value = _finite((row or {}).get("movement_p_value"))
        alpha = _finite((row or {}).get("movement_familywise_alpha"))
        movement_direction = str((row or {}).get("movement_direction") or "")
        lock = str((row or {}).get("direction_lock_state") or "")
        significant = bool((row or {}).get("movement_significant"))
        if p_value is not None and alpha is not None and p_value <= alpha:
            significant = True
        live_direction = lock if lock in ("BULLISH", "BEARISH") else ""
        if not live_direction and significant and movement_direction in ("Bullish", "Bearish"):
            live_direction = movement_direction.upper()
        out[symbol] = {
            "live_price": live_price,
            "live_direction": live_direction.title() if live_direction else None,
            "direction_lock_state": lock or None,
            "direction_lock_phase": (row or {}).get("direction_lock_phase"),
            "movement_direction": movement_direction or None,
            "movement_significant": bool(significant),
            "movement_p_value": p_value,
            "movement_familywise_alpha": alpha,
            "movement_z": _finite((row or {}).get("movement_z")),
            "movement_horizon_seconds": (row or {}).get("movement_horizon_seconds"),
            "day_change_pct": _finite((row or {}).get("day_change_pct")),
            "ret_3m_pct": _finite((row or {}).get("ret_3m_pct")),
            "ret_5m_pct": _finite((row or {}).get("ret_5m_pct")),
            "ret_10m_pct": _finite((row or {}).get("ret_10m_pct")),
            "relative_5m_vs_nifty_pct": _finite((row or {}).get("relative_5m_vs_nifty_pct")),
            "volume_rate_accel": _finite((row or {}).get("volume_rate_accel")),
        }
    return out


def _enrich_signal(signal, ctx, market_direction, *, signal_ts=None, now=None):
    row = dict(signal or {})
    symbol = str(row.get("symbol") or "")
    side = str(row.get("side") or "")
    context = dict((ctx or {}).get(symbol) or {})
    live_price = _finite(context.get("live_price"))
    anchor = _finite(row.get("spot"))
    signed = 1.0 if side == "Bullish" else -1.0
    directional_bps = None
    if live_price is not None and anchor is not None and anchor > 0:
        directional_bps = signed * (live_price / anchor - 1.0) * 10000.0

    live_direction = str(context.get("live_direction") or "")
    significant_same_side = (
        bool(context.get("movement_significant"))
        and live_direction == side
    )
    market_aligned = side == str(market_direction or "")
    if market_aligned:
        lane = "PRIMARY_CONFIRMED" if significant_same_side else (
            "PRIMARY_MOVING" if directional_bps is not None and directional_bps > 0 else "PRIMARY_WATCH"
        )
    elif significant_same_side:
        lane = "REVERSAL_WATCH"
    else:
        lane = "EXTREME_WATCH"

    row.update(context)
    option_type = "CE" if side == "Bullish" else "PE"
    selected_contract = row.get("call_contract") if side == "Bullish" else row.get("put_contract")
    relevant_spread = _finite(row.get("call_spread_pct")) if side == "Bullish" else _finite(row.get("put_spread_pct"))
    option_ok = relevant_spread is not None and relevant_spread <= 3.0

    anchor_age_seconds = None
    signal_dt = _parse_dt(signal_ts)
    if signal_dt is not None and now is not None:
        if signal_dt.tzinfo is not None and now.tzinfo is None:
            signal_dt = signal_dt.replace(tzinfo=None)
        elif signal_dt.tzinfo is None and now.tzinfo is not None:
            signal_dt = signal_dt.replace(tzinfo=now.tzinfo)
        anchor_age_seconds = max(0.0, (now - signal_dt).total_seconds())

    if anchor_age_seconds is None:
        entry_window_state = "UNKNOWN"
    elif anchor_age_seconds <= 15 * 60:
        entry_window_state = "ANCHOR_WINDOW"
    else:
        entry_window_state = "LATE_TRACK_ONLY"

    row["live_directional_bps"] = directional_bps
    row["market_aligned"] = bool(market_aligned)
    row["significant_same_side"] = bool(significant_same_side)
    row["opportunity_lane"] = lane
    row["relevant_option_spread_pct"] = relevant_spread
    row["option_spread_le_3pct"] = bool(option_ok)
    row["machine_option_type"] = option_type
    row["machine_option_contract"] = selected_contract
    row["machine_option_strike"] = _finite(row.get("strike"))
    row["machine_option_expiry"] = row.get("expiry")
    row["machine_option_spread_pct"] = relevant_spread
    row["machine_option_selection_reason"] = (
        f"ATM {option_type} from fixed skew-anchor snapshot; "
        + (
            "next expiry because nearest expiry DTE <=3"
            if row.get("expiry_selection") == "NEXT_EXPIRY_DTE_LE_3"
            else "nearest eligible expiry"
        )
    )
    row["research_primary_instrument"] = "STOCK_FUTURES"
    row["research_option_status"] = "OPTION_ELIGIBLE" if option_ok else "OPTION_SPREAD_TOO_WIDE"
    row["anchor_age_seconds"] = anchor_age_seconds
    row["entry_window_state"] = entry_window_state
    row["machine_option_entry_eligible"] = bool(
        market_aligned and entry_window_state == "ANCHOR_WINDOW" and option_ok and selected_contract
    )
    row["machine_option_action"] = (
        "OPTION_ENTRY_ELIGIBLE"
        if row["machine_option_entry_eligible"]
        else ("TRACK_ONLY" if selected_contract else "NO_OPTION_CONTRACT")
    )
    row["research_exit_clock"] = "15:10"
    row["validation_status"] = "VALIDATION_IN_PROCESS"
    row["controls_trading"] = False
    return row


def live_status(*, state_file, live_rows, now):
    """Return a dashboard-ready live view without changing trading controls.

    Evidence-backed market-aligned extremes remain the primary lane. The opposite
    skew extreme is not discarded: if the existing full-universe observer later
    establishes a statistically significant move in that skew direction, it is
    surfaced as REVERSAL_WATCH. This is research-only and does not mutate Focus.
    """
    state = load_state(state_file)
    status = shadow_status(state_file)
    current = dict(state.get("current") or {})
    if not current:
        status["live_updated_at"] = now.isoformat(timespec="seconds")
        status["validation_status"] = "VALIDATION_IN_PROCESS"
        return status

    ctx = _observer_context_map(live_rows)
    market_direction = current.get("market_direction")
    primary = [
        _enrich_signal(row, ctx, market_direction, signal_ts=current.get("signal_ts"), now=now)
        for row in (current.get("selected") or [])
    ]
    top = [
        _enrich_signal(row, ctx, market_direction, signal_ts=current.get("signal_ts"), now=now)
        for row in (current.get("top_5pct") or [])
    ]
    bottom = [
        _enrich_signal(row, ctx, market_direction, signal_ts=current.get("signal_ts"), now=now)
        for row in (current.get("bottom_5pct") or [])
    ]
    all_extremes = top + bottom
    reversal = [row for row in all_extremes if row.get("opportunity_lane") == "REVERSAL_WATCH"]
    extreme_watch = [row for row in all_extremes if row.get("opportunity_lane") == "EXTREME_WATCH"]

    current["live_primary"] = primary
    current["live_reversal_watch"] = reversal
    current["live_extreme_watch"] = extreme_watch
    current["live_primary_positive"] = sum(
        1 for row in primary if (_finite(row.get("live_directional_bps")) or 0.0) > 0
    )
    current["live_primary_confirmed"] = sum(
        1 for row in primary if row.get("opportunity_lane") == "PRIMARY_CONFIRMED"
    )
    current["live_reversal_count"] = len(reversal)
    current["live_updated_at"] = now.isoformat(timespec="seconds")
    status["current"] = current
    status["live_updated_at"] = current["live_updated_at"]
    status["validation_status"] = "VALIDATION_IN_PROCESS"
    status["controls_trading"] = False
    return status


def update_one_hour_outcomes(*, live_rows, state_file, ledger_file, now):
    """Record first live scanner observation at/after +60m for each open batch."""
    state = load_state(state_file)
    spots = _live_spot_map(live_rows)
    changed = False
    for batch_id, batch in list((state.get("open_batches") or {}).items()):
        if not isinstance(batch, dict) or batch.get("one_hour_outcome"):
            continue
        signal_ts = _parse_dt(batch.get("signal_ts"))
        if signal_ts is None:
            continue
        if signal_ts.tzinfo is not None and now.tzinfo is None:
            signal_ts = signal_ts.replace(tzinfo=None)
        elif signal_ts.tzinfo is None and now.tzinfo is not None:
            signal_ts = signal_ts.replace(tzinfo=now.tzinfo)
        age = (now - signal_ts).total_seconds()
        if age < 3600:
            continue

        outcomes = []
        for signal in batch.get("selected") or []:
            symbol = str(signal.get("symbol") or "")
            entry = _finite(signal.get("spot"))
            exit_spot = _finite(spots.get(symbol))
            if not symbol or entry is None or entry <= 0 or exit_spot is None or exit_spot <= 0:
                continue
            side = str(signal.get("side") or "")
            signed = 1.0 if side == "Bullish" else -1.0
            directional_bps = signed * (exit_spot / entry - 1.0) * 10000.0
            outcomes.append({
                "symbol": symbol,
                "side": side,
                "entry_spot": entry,
                "exit_spot": exit_spot,
                "directional_spot_bps": directional_bps,
                "net_6bps_proxy": directional_bps - 6.0,
                "skew_pct_points": signal.get("skew_pct_points"),
                "rank_percentile": signal.get("rank_percentile"),
            })

        result = {
            "record_type": "SKEW_SHADOW_60M",
            "batch_id": batch_id,
            "date": batch.get("date"),
            "slot": batch.get("slot"),
            "signal_clock": batch.get("signal_clock"),
            "observed_at": now.isoformat(timespec="seconds"),
            "delay_seconds": round(max(0.0, age - 3600.0), 1),
            "selected_count": len(batch.get("selected") or []),
            "outcome_count": len(outcomes),
            "mean_directional_spot_bps": (
                sum(x["directional_spot_bps"] for x in outcomes) / len(outcomes)
                if outcomes else None
            ),
            "median_directional_spot_bps": (
                sorted(x["directional_spot_bps"] for x in outcomes)[len(outcomes)//2]
                if outcomes else None
            ),
            "mean_net_6bps_proxy": (
                sum(x["net_6bps_proxy"] for x in outcomes) / len(outcomes)
                if outcomes else None
            ),
            "positive_count": sum(1 for x in outcomes if x["net_6bps_proxy"] > 0),
            "win_rate_pct": (
                100.0 * sum(1 for x in outcomes if x["net_6bps_proxy"] > 0) / len(outcomes)
                if outcomes else None
            ),
            "outcomes": outcomes,
            "controls_trading": False,
        }
        batch["one_hour_outcome"] = result
        state["current"] = batch
        state["last_outcome_at"] = result["observed_at"]
        _append_jsonl(ledger_file, result)
        changed = True

    if changed:
        state["status"] = "ONE_HOUR_RECORDED"
        _atomic_write(state_file, state)
    return shadow_status(state_file)


def _process_entry(record, previous, state, ledger_file, now):
    slot = str(record.get("slot") or "")
    rows = _eligible_rows(record)
    market = _market_context(rows, previous)
    top, bottom, selected = _rank_and_filter(rows, market)

    batch_id = f"{now.date().isoformat()}|{slot}"
    batch = {
        "batch_id": batch_id,
        "date": now.date().isoformat(),
        "slot": slot,
        "signal_clock": ENTRY_SLOTS.get(slot),
        "signal_ts": str(record.get("ts") or now.isoformat(timespec="seconds")),
        "status": "SHADOW_SIGNAL" if selected else market.get("status") or "NO_SELECTION",
        "market_move_pct": market.get("market_move_pct"),
        "market_direction": market.get("market_direction"),
        "market_coverage": market.get("coverage"),
        "eligible_symbols": len(rows),
        "extreme_count_per_side": len(top),
        "selected_count": len(selected),
        "selected": selected,
        "top_5pct": top,
        "bottom_5pct": bottom,
        "controls_trading": False,
    }
    state["open_batches"][batch_id] = batch
    processed = list(state.get("processed_batch_ids") or [])
    if batch_id not in processed:
        processed.append(batch_id)
    state["processed_batch_ids"] = processed[-200:]
    state["current"] = batch
    state["status"] = batch["status"]
    state["last_signal_at"] = batch["signal_ts"]
    _append_jsonl(ledger_file, {"record_type": "SKEW_SHADOW_SIGNAL", **batch})
    log.info(
        "IV_SKEW_SHADOW_SIGNAL batch=%s slot=%s market=%s eligible=%s selected=%s primary=%s top=%s bottom=%s",
        batch_id,
        batch.get("signal_clock"),
        batch.get("market_direction"),
        batch.get("eligible_symbols"),
        batch.get("selected_count"),
        [x.get("symbol") for x in selected],
        [x.get("symbol") for x in top],
        [x.get("symbol") for x in bottom],
    )
    return batch


def _close_batches(record, state, ledger_file, now):
    exit_spots = _spot_map(record)
    changed = []
    for batch_id, batch in list((state.get("open_batches") or {}).items()):
        if str(batch.get("date") or "") != now.date().isoformat():
            continue
        outcomes = []
        for signal in batch.get("selected") or []:
            symbol = str(signal.get("symbol") or "")
            entry = _finite(signal.get("spot"))
            exit_spot = _finite(exit_spots.get(symbol))
            if not symbol or entry is None or entry <= 0 or exit_spot is None or exit_spot <= 0:
                continue
            side = str(signal.get("side") or "")
            signed = 1.0 if side == "Bullish" else -1.0
            directional_bps = signed * (exit_spot / entry - 1.0) * 10000.0
            outcomes.append({
                "symbol": symbol,
                "side": side,
                "entry_spot": entry,
                "exit_spot": exit_spot,
                "directional_spot_bps": directional_bps,
                "net_6bps_proxy": directional_bps - 6.0,
                "skew_pct_points": signal.get("skew_pct_points"),
                "rank_percentile": signal.get("rank_percentile"),
            })
        mean_bps = (
            sum(x["directional_spot_bps"] for x in outcomes) / len(outcomes)
            if outcomes else None
        )
        mean_net = (
            sum(x["net_6bps_proxy"] for x in outcomes) / len(outcomes)
            if outcomes else None
        )
        close = {
            "record_type": "SKEW_SHADOW_OUTCOME",
            "batch_id": batch_id,
            "date": batch.get("date"),
            "slot": batch.get("slot"),
            "signal_clock": batch.get("signal_clock"),
            "exit_ts": str(record.get("ts") or now.isoformat(timespec="seconds")),
            "selected_count": len(batch.get("selected") or []),
            "outcome_count": len(outcomes),
            "mean_directional_spot_bps": mean_bps,
            "mean_net_6bps_proxy": mean_net,
            "positive_count": sum(1 for x in outcomes if x["net_6bps_proxy"] > 0),
            "outcomes": outcomes,
            "controls_trading": False,
        }
        _append_jsonl(ledger_file, close)
        state["completed_batches"] = int(state.get("completed_batches") or 0) + 1
        state["completed_signals"] = int(state.get("completed_signals") or 0) + len(outcomes)
        state["last_outcome_at"] = close["exit_ts"]
        state["open_batches"].pop(batch_id, None)
        changed.append(close)

    if changed:
        state["status"] = "OUTCOMES_RECORDED"
        current = dict(state.get("current") or {})
        if current:
            current["latest_outcome"] = changed[-1]
            state["current"] = current
    return changed


def process_slot(*, snapshot_file, state_file, ledger_file, now, slot):
    """Process a newly captured V12 fixed slot. Fail-soft and idempotent by batch id."""
    state = load_state(state_file)
    try:
        record = _latest_slot_record(snapshot_file, now.date(), slot)
        if not record:
            state["last_error"] = f"captured slot {slot} not found in snapshot ledger"
            _atomic_write(state_file, state)
            return shadow_status(state_file)

        if slot in ENTRY_SLOTS:
            batch_id = f"{now.date().isoformat()}|{slot}"
            already = batch_id in set(state.get("processed_batch_ids") or [])
            if not already and batch_id not in state.get("open_batches", {}):
                previous = _previous_pre_cas(snapshot_file, now.date())
                if previous is None:
                    state["status"] = "WAITING_FOR_PREVIOUS_PRE_CAS"
                    state["last_error"] = None
                else:
                    _process_entry(record, previous, state, ledger_file, now)
        elif slot == EXIT_SLOT:
            _close_batches(record, state, ledger_file, now)

        state["last_error"] = None
    except Exception as exc:  # research shadow must never stop live scanner
        state["status"] = "ERROR"
        state["last_error"] = str(exc)
    _atomic_write(state_file, state)
    return shadow_status(state_file)


def backfill_today_entries(*, snapshot_file, state_file, ledger_file, now):
    """Replay already-recorded 09:30/13:00 slots after a mid-session deploy.

    This is intentionally same-day only and idempotent. It never creates synthetic
    data; it only processes fixed V12 option-recorder slots already present in the
    persistent snapshot ledger.
    """
    state = load_state(state_file)
    processed = set(state.get("processed_batch_ids") or [])
    changed = False

    for slot, clock in ENTRY_SLOTS.items():
        hh, mm = [int(x) for x in clock.split(":")]
        due = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
        if now < due:
            continue
        batch_id = f"{now.date().isoformat()}|{slot}"
        if batch_id in processed or batch_id in state.get("open_batches", {}):
            continue
        record = _latest_slot_record(snapshot_file, now.date(), slot)
        if not record:
            continue
        previous = _previous_pre_cas(snapshot_file, now.date())
        if previous is None:
            state["status"] = "WAITING_FOR_PREVIOUS_PRE_CAS"
            continue
        _process_entry(record, previous, state, ledger_file, now)
        processed.add(batch_id)
        changed = True

    if changed:
        state["status"] = "SHADOW_SIGNAL"
        state["last_error"] = None
        _atomic_write(state_file, state)
    return shadow_status(state_file)
