"""Stage 3 for the audited NIFTY opening-range option-buying study.

This module is deliberately research-only.  It freezes the single Stage-2
candidate selected after the 21-Sep-2026 audit and adds the evidence that Stage
2 did not have:

* returns in NIFTY points and percent of spot (not only R);
* executable long-option P&L using ask on entry and bid on exit;
* the repository's versioned NSE equity-option charge model;
* a NIFTY-futures directional control from the same recorded timestamps;
* a session-level bootstrap confidence interval;
* unchanged cross-index replication on externally supplied 1-minute bars; and
* a forward-paper ledger that can be built directly from the V12.1 recorder.

No function in this file places orders, changes the V12/V12.1 recorder, changes
Trial 25, or promotes a production playbook.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import math
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Iterable, Sequence

import numpy as np
import pandas as pd

from .index_option_research import _validate_1m_ohlc
from .index_option_stage2 import Stage2Spec, analyze_session_confirmation
from .trial25_execution import FEE_MODEL_VERSION, calculate_option_charges


AUDIT_DATE = "2026-09-21"
STAGE3_FORWARD_START = "2026-09-22"


@dataclass(frozen=True)
class FrozenStage3Spec:
    opening_range_minutes: int = 90
    trigger_minutes: int = 3
    confirmation: str = "double_close"
    buffer_pct: float = 0.0004
    range_pct_low: float = 0.0015
    range_pct_high: float = 0.0060
    entry_deadline: str = "13:00"
    hold_minutes: int = 120
    stop_rule: str = "NONE_TIME_EXIT_ONLY"
    one_trade_per_session: bool = True


FROZEN_SPEC = FrozenStage3Spec()
FROZEN_SPEC_JSON = json.dumps(asdict(FROZEN_SPEC), sort_keys=True, separators=(",", ":"))
FROZEN_SPEC_SHA256 = hashlib.sha256(FROZEN_SPEC_JSON.encode("utf-8")).hexdigest()


def _finite(value) -> bool:
    try:
        return value is not None and math.isfinite(float(value))
    except (TypeError, ValueError):
        return False


def _clock(value: str) -> dt.time:
    hour, minute = str(value).split(":", 1)
    return dt.time(int(hour), int(minute))


def frozen_stage2_spec() -> Stage2Spec:
    """Return the exact immutable Stage-3 signal spec."""
    return Stage2Spec(
        opening_range_minutes=FROZEN_SPEC.opening_range_minutes,
        trigger_minutes=FROZEN_SPEC.trigger_minutes,
        confirmation=FROZEN_SPEC.confirmation,
        buffer_pct=FROZEN_SPEC.buffer_pct,
        entry_deadline=_clock(FROZEN_SPEC.entry_deadline),
    )


def _add_point_metrics(row: dict) -> dict:
    out = dict(row)
    width = float(out["range_width"])
    spot = float(out["signal_close"])
    for horizon in (60, 120):
        r_key = f"return_{horizon}m_r"
        r = out.get(r_key)
        points = float(r) * width if _finite(r) else None
        out[f"return_{horizon}m_points"] = points
        out[f"return_{horizon}m_pct_spot"] = (
            points / spot * 100.0 if points is not None and spot > 0 else None
        )
    out["r_definition"] = "directional_index_move_divided_by_opening_range_width"
    return out


def evaluate_frozen_spec(bars_1m: pd.DataFrame, *, instrument: str = "NIFTY 50") -> pd.DataFrame:
    """Run only the frozen audited spec; there is no grid or parameter search."""
    bars = _validate_1m_ohlc(bars_1m)
    if bars.empty:
        return pd.DataFrame()
    rows = []
    spec = frozen_stage2_spec()
    bounds = (FROZEN_SPEC.range_pct_low, FROZEN_SPEC.range_pct_high)
    for _, session in bars.groupby(bars.index.normalize(), sort=True):
        row = analyze_session_confirmation(session, spec, range_pct_bounds=bounds)
        if row is None:
            continue
        row = _add_point_metrics(row)
        row["instrument"] = instrument
        row["frozen_spec_sha256"] = FROZEN_SPEC_SHA256
        rows.append(row)
    return pd.DataFrame.from_records(rows)


def _candidate_rows(trades: pd.DataFrame) -> pd.DataFrame:
    if trades is None or trades.empty:
        return pd.DataFrame()
    required = {
        "opening_range_minutes",
        "trigger_minutes",
        "confirmation",
        "range_pct",
        "range_width",
        "signal_close",
        "return_120m_r",
    }
    missing = required.difference(trades.columns)
    if missing:
        raise ValueError(f"Stage-2 ledger missing columns: {sorted(missing)}")
    mask = (
        (trades["opening_range_minutes"].astype(int) == FROZEN_SPEC.opening_range_minutes)
        & (trades["trigger_minutes"].astype(int) == FROZEN_SPEC.trigger_minutes)
        & (trades["confirmation"].astype(str) == FROZEN_SPEC.confirmation)
    )
    out = trades.loc[mask].copy()
    if out.empty:
        return out
    out["return_120m_points"] = (
        pd.to_numeric(out["return_120m_r"], errors="coerce")
        * pd.to_numeric(out["range_width"], errors="coerce")
    )
    out["return_120m_pct_spot"] = (
        out["return_120m_points"]
        / pd.to_numeric(out["signal_close"], errors="coerce")
        * 100.0
    )
    return out


def gate_points_check(primary_trades: pd.DataFrame) -> dict:
    """Test the 0.15%-0.60% gate in index points, not R."""
    rows = _candidate_rows(primary_trades)
    if rows.empty:
        return {"status": "NO_DATA", "inside_n": 0, "removed_n": 0}
    rp = pd.to_numeric(rows["range_pct"], errors="coerce")
    pts = pd.to_numeric(rows["return_120m_points"], errors="coerce")
    inside = rows[(rp >= FROZEN_SPEC.range_pct_low) & (rp <= FROZEN_SPEC.range_pct_high)].copy()
    removed = rows[(rp < FROZEN_SPEC.range_pct_low) | (rp > FROZEN_SPEC.range_pct_high)].copy()
    inside_pts = pd.to_numeric(inside["return_120m_points"], errors="coerce").dropna()
    removed_pts = pd.to_numeric(removed["return_120m_points"], errors="coerce").dropna()
    if inside_pts.empty or removed_pts.empty:
        status = "INSUFFICIENT_DATA"
    else:
        status = "PASS" if float(inside_pts.mean()) > float(removed_pts.mean()) else "FAIL"
    return {
        "status": status,
        "inside_n": int(len(inside_pts)),
        "removed_n": int(len(removed_pts)),
        "inside_mean_points": float(inside_pts.mean()) if len(inside_pts) else None,
        "removed_mean_points": float(removed_pts.mean()) if len(removed_pts) else None,
        "difference_points": (
            float(inside_pts.mean() - removed_pts.mean())
            if len(inside_pts) and len(removed_pts)
            else None
        ),
    }


def session_bootstrap_mean_ci(
    frame: pd.DataFrame,
    value_col: str,
    *,
    session_col: str = "session",
    samples: int = 5000,
    alpha: float = 0.05,
    seed: int = 2503,
) -> dict:
    """Deterministic session-level bootstrap CI for a mean."""
    if frame is None or frame.empty or value_col not in frame.columns:
        return {"n_sessions": 0, "mean": None, "ci_low": None, "ci_high": None}
    work = frame[[session_col, value_col]].copy()
    work[value_col] = pd.to_numeric(work[value_col], errors="coerce")
    work = work.dropna(subset=[session_col, value_col])
    if work.empty:
        return {"n_sessions": 0, "mean": None, "ci_low": None, "ci_high": None}
    per_session = work.groupby(session_col, sort=True)[value_col].mean().astype(float)
    values = per_session.to_numpy()
    rng = np.random.default_rng(int(seed))
    means = np.empty(max(1, int(samples)), dtype=float)
    n = len(values)
    for i in range(len(means)):
        means[i] = rng.choice(values, size=n, replace=True).mean()
    lo = float(np.quantile(means, alpha / 2.0))
    hi = float(np.quantile(means, 1.0 - alpha / 2.0))
    return {
        "n_sessions": int(n),
        "mean": float(values.mean()),
        "ci_low": lo,
        "ci_high": hi,
        "bootstrap_samples": int(len(means)),
        "alpha": float(alpha),
        "seed": int(seed),
    }


def load_v121_micro_jsonl(paths: Iterable[str | Path]) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Load V12.1 micro JSONL into option-quote and reference-snapshot frames."""
    quote_rows: list[dict] = []
    ref_rows: list[dict] = []
    for raw_path in sorted(Path(p) for p in paths):
        if not raw_path.exists() or raw_path.stat().st_size == 0:
            continue
        with raw_path.open("r", encoding="utf-8") as handle:
            for line_number, line in enumerate(handle, start=1):
                line = line.strip()
                if not line:
                    continue
                try:
                    payload = json.loads(line)
                except json.JSONDecodeError as exc:
                    raise ValueError(f"invalid JSONL {raw_path}:{line_number}") from exc
                ts = pd.Timestamp(payload.get("ts"))
                refs = payload.get("refs") or {}
                ref_rows.append(
                    {
                        "snapshot_ts": ts,
                        "spot": refs.get("spot"),
                        "nifty_future": refs.get("future"),
                        "india_vix": refs.get("vix"),
                        "source_file": raw_path.name,
                    }
                )
                for row in payload.get("rows") or []:
                    if not isinstance(row, dict):
                        continue
                    out = dict(row)
                    out["snapshot_ts"] = ts
                    out["source_file"] = raw_path.name
                    if out.get("spot") is None:
                        out["spot"] = refs.get("spot")
                    if out.get("nifty_future") is None:
                        out["nifty_future"] = refs.get("future")
                    quote_rows.append(out)
    quotes = pd.DataFrame.from_records(quote_rows)
    refs = pd.DataFrame.from_records(ref_rows)
    if not quotes.empty:
        quotes = quotes.sort_values(["snapshot_ts", "strike", "type"]).reset_index(drop=True)
    if not refs.empty:
        refs = refs.drop_duplicates(subset=["snapshot_ts"], keep="last").sort_values("snapshot_ts").reset_index(drop=True)
    return quotes, refs


def build_spot_1m_from_refs(refs: pd.DataFrame) -> pd.DataFrame:
    """Reconstruct observed NIFTY 1-minute OHLC from recorder spot snapshots."""
    if refs is None or refs.empty:
        return pd.DataFrame(columns=["open", "high", "low", "close"])
    work = refs[["snapshot_ts", "spot"]].copy()
    work["snapshot_ts"] = pd.to_datetime(work["snapshot_ts"])
    work["spot"] = pd.to_numeric(work["spot"], errors="coerce")
    work = work.dropna().drop_duplicates(subset=["snapshot_ts"], keep="last")
    if work.empty:
        return pd.DataFrame(columns=["open", "high", "low", "close"])
    work = work.set_index("snapshot_ts").sort_index()
    bars = work["spot"].resample("1min").agg(["first", "max", "min", "last"])
    bars.columns = ["open", "high", "low", "close"]
    return bars.dropna(how="any")


def _first_snapshot_at_or_after(
    frame: pd.DataFrame,
    target,
    *,
    max_delay_seconds: int = 30,
) -> pd.DataFrame:
    if frame is None or frame.empty:
        return pd.DataFrame()
    target = pd.Timestamp(target)
    ts = pd.to_datetime(frame["snapshot_ts"])
    eligible = frame[ts >= target].copy()
    if eligible.empty:
        return pd.DataFrame()
    first_ts = pd.to_datetime(eligible["snapshot_ts"]).min()
    if (first_ts - target).total_seconds() > max(0, int(max_delay_seconds)):
        return pd.DataFrame()
    return eligible[pd.to_datetime(eligible["snapshot_ts"]) == first_ts].copy()


def _select_entry_contract(snapshot: pd.DataFrame, direction: str, moneyness: str) -> dict | None:
    if snapshot is None or snapshot.empty:
        return None
    typ = "CE" if str(direction) == "Bullish" else "PE"
    rows = snapshot[snapshot["type"].astype(str).str.upper().eq(typ)].copy()
    if rows.empty:
        return None
    rows["strike"] = pd.to_numeric(rows["strike"], errors="coerce")
    rows["spot"] = pd.to_numeric(rows.get("spot"), errors="coerce")
    rows = rows.dropna(subset=["strike", "spot"])
    if rows.empty:
        return None
    spot = float(rows["spot"].median())
    strikes = sorted(rows["strike"].unique())
    atm = min(strikes, key=lambda strike: (abs(float(strike) - spot), float(strike)))
    moneyness = str(moneyness).upper()
    if moneyness == "ATM":
        strike = atm
    elif moneyness == "ITM1":
        ix = strikes.index(atm)
        if typ == "CE":
            if ix == 0:
                return None
            strike = strikes[ix - 1]
        else:
            if ix + 1 >= len(strikes):
                return None
            strike = strikes[ix + 1]
    else:
        raise ValueError("moneyness must be ATM or ITM1")
    chosen = rows[rows["strike"].eq(strike)].copy()
    if chosen.empty:
        return None
    if "expiry" in chosen.columns:
        chosen["expiry_sort"] = pd.to_datetime(chosen["expiry"], errors="coerce")
        chosen = chosen.sort_values(["expiry_sort", "tradingsymbol"], na_position="last")
    return chosen.iloc[0].to_dict()


def _fresh_executable(row: dict, side: str, lot_size: int, max_quote_age_seconds: float) -> tuple[bool, str | None, float | None]:
    side = str(side).upper()
    age = row.get("quote_age_seconds")
    if _finite(age) and float(age) > float(max_quote_age_seconds):
        return False, "STALE_QUOTE", None
    if side == "BUY":
        price = row.get("best_ask")
        qty = row.get("ask_qty")
    elif side == "SELL":
        price = row.get("best_bid")
        qty = row.get("bid_qty")
    else:
        return False, "INVALID_SIDE", None
    if not _finite(price) or float(price) <= 0:
        return False, "NO_EXECUTABLE_PRICE", None
    if int(qty or 0) < int(lot_size or 0):
        return False, "INSUFFICIENT_TOP_QTY", None
    return True, None, float(price)


def _same_contract(frame: pd.DataFrame, entry: dict) -> pd.DataFrame:
    if frame is None or frame.empty:
        return pd.DataFrame()
    token = entry.get("instrument_token")
    if token is not None and "instrument_token" in frame.columns:
        exact = frame[pd.to_numeric(frame["instrument_token"], errors="coerce").eq(float(token))]
        if not exact.empty:
            return exact.copy()
    symbol = entry.get("tradingsymbol")
    if symbol is not None and "tradingsymbol" in frame.columns:
        return frame[frame["tradingsymbol"].astype(str).eq(str(symbol))].copy()
    return pd.DataFrame()


def _nearest_ref(refs: pd.DataFrame, target, *, max_delay_seconds: int = 30) -> dict | None:
    snap = _first_snapshot_at_or_after(refs, target, max_delay_seconds=max_delay_seconds)
    return None if snap.empty else snap.iloc[0].to_dict()


def map_signal_to_option_pnl(
    signal: dict,
    quotes: pd.DataFrame,
    refs: pd.DataFrame,
    *,
    moneyness: str,
    max_delay_seconds: int = 30,
    max_quote_age_seconds: float = 15.0,
) -> dict:
    """Map one frozen signal to an executable one-lot long option round trip."""
    signal_time = pd.Timestamp(signal["signal_time"])
    exit_target = signal_time + pd.Timedelta(minutes=FROZEN_SPEC.hold_minutes)
    entry_snap = _first_snapshot_at_or_after(quotes, signal_time, max_delay_seconds=max_delay_seconds)
    if entry_snap.empty:
        return {"status": "NO_ENTRY_SNAPSHOT", "moneyness": moneyness}
    entry = _select_entry_contract(entry_snap, signal["direction"], moneyness)
    if entry is None:
        return {"status": "NO_ENTRY_CONTRACT", "moneyness": moneyness}
    lot = int(entry.get("lot_size") or 0)
    if lot <= 0:
        return {"status": "INVALID_LOT_SIZE", "moneyness": moneyness}
    ok, reason, entry_px = _fresh_executable(entry, "BUY", lot, max_quote_age_seconds)
    if not ok:
        return {"status": reason, "moneyness": moneyness}

    same = _same_contract(quotes, entry)
    exit_snap = _first_snapshot_at_or_after(same, exit_target, max_delay_seconds=max_delay_seconds)
    if exit_snap.empty:
        return {"status": "NO_EXIT_SNAPSHOT", "moneyness": moneyness}
    exit_row = exit_snap.iloc[0].to_dict()
    ok, reason, exit_px = _fresh_executable(exit_row, "SELL", lot, max_quote_age_seconds)
    if not ok:
        return {"status": reason, "moneyness": moneyness}

    fills = [
        {"side": "BUY", "price": entry_px, "quantity": lot},
        {"side": "SELL", "price": exit_px, "quantity": lot},
    ]
    charges = calculate_option_charges(fills)
    gross = (float(exit_px) - float(entry_px)) * lot
    net = gross - float(charges["total"])
    entry_notional = float(entry_px) * lot
    expiry = str(entry.get("expiry") or "")
    session = pd.Timestamp(signal["session"]).date()
    try:
        expiry_day = dt.date.fromisoformat(expiry[:10]) == session
    except ValueError:
        expiry_day = None

    entry_ref = _nearest_ref(refs, signal_time, max_delay_seconds=max_delay_seconds)
    exit_ref = _nearest_ref(refs, exit_target, max_delay_seconds=max_delay_seconds)
    fut_points = None
    if entry_ref and exit_ref and _finite(entry_ref.get("nifty_future")) and _finite(exit_ref.get("nifty_future")):
        e = float(entry_ref["nifty_future"])
        x = float(exit_ref["nifty_future"])
        fut_points = (x - e) if signal["direction"] == "Bullish" else (e - x)

    return {
        "status": "OK",
        "session": str(signal["session"]),
        "direction": signal["direction"],
        "signal_time": signal_time.isoformat(),
        "exit_target": exit_target.isoformat(),
        "moneyness": str(moneyness).upper(),
        "tradingsymbol": entry.get("tradingsymbol"),
        "instrument_token": entry.get("instrument_token"),
        "expiry": expiry or None,
        "expiry_day": expiry_day,
        "strike": float(entry["strike"]),
        "lot_size": lot,
        "entry_snapshot_ts": pd.Timestamp(entry["snapshot_ts"]).isoformat(),
        "exit_snapshot_ts": pd.Timestamp(exit_row["snapshot_ts"]).isoformat(),
        "entry_ask": float(entry_px),
        "exit_bid": float(exit_px),
        "gross_pnl": float(gross),
        "charges": float(charges["total"]),
        "net_pnl": float(net),
        "net_return_pct_entry_premium": float(net / entry_notional * 100.0) if entry_notional > 0 else None,
        "fee_model_version": charges["model_version"],
        "futures_directional_points": fut_points,
        "frozen_spec_sha256": FROZEN_SPEC_SHA256,
    }


def build_forward_paper_ledger(
    quotes: pd.DataFrame,
    refs: pd.DataFrame,
    *,
    instrument: str = "NIFTY 50",
    moneyness: Sequence[str] = ("ATM", "ITM1"),
    forward_start: str = STAGE3_FORWARD_START,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Create frozen signals and executable paper P&L from V12.1 recorder data."""
    bars = build_spot_1m_from_refs(refs)
    signals = evaluate_frozen_spec(bars, instrument=instrument)
    if not signals.empty:
        signals = signals[pd.to_datetime(signals["session"]) >= pd.Timestamp(forward_start)].copy()
    ledger = []
    for _, signal in signals.iterrows():
        payload = signal.to_dict()
        for bucket in moneyness:
            row = map_signal_to_option_pnl(payload, quotes, refs, moneyness=bucket)
            row["instrument"] = instrument
            ledger.append(row)
    return signals.reset_index(drop=True), pd.DataFrame.from_records(ledger)


def summarize_option_pnl(ledger: pd.DataFrame) -> pd.DataFrame:
    if ledger is None or ledger.empty:
        return pd.DataFrame()
    ok = ledger[ledger["status"].astype(str).eq("OK")].copy()
    if ok.empty:
        return pd.DataFrame()
    rows = []
    for moneyness, group in ok.groupby("moneyness", sort=True):
        net = pd.to_numeric(group["net_pnl"], errors="coerce").dropna()
        ci = session_bootstrap_mean_ci(group, "net_pnl")
        rows.append(
            {
                "moneyness": moneyness,
                "trade_count": int(len(net)),
                "session_count": int(group["session"].nunique()),
                "mean_net_pnl": float(net.mean()) if len(net) else None,
                "median_net_pnl": float(net.median()) if len(net) else None,
                "total_net_pnl": float(net.sum()) if len(net) else None,
                "win_rate_pct": float((net > 0).mean() * 100.0) if len(net) else None,
                "bootstrap_ci_low": ci["ci_low"],
                "bootstrap_ci_high": ci["ci_high"],
                "expiry_day_trades": int(group["expiry_day"].fillna(False).astype(bool).sum()),
                "non_expiry_day_trades": int((~group["expiry_day"].fillna(False).astype(bool)).sum()),
                "fee_model_version": FEE_MODEL_VERSION,
            }
        )
    return pd.DataFrame.from_records(rows)


def cross_index_replication(bars_map: dict[str, pd.DataFrame]) -> pd.DataFrame:
    """Apply the NIFTY-frozen rule unchanged to other indices."""
    rows = []
    for instrument, bars in (bars_map or {}).items():
        ledger = evaluate_frozen_spec(bars, instrument=instrument)
        points = pd.to_numeric(ledger.get("return_120m_points"), errors="coerce").dropna() if not ledger.empty else pd.Series(dtype=float)
        ci = session_bootstrap_mean_ci(ledger, "return_120m_points") if not ledger.empty else {
            "ci_low": None,
            "ci_high": None,
        }
        rows.append(
            {
                "instrument": instrument,
                "trade_count": int(len(points)),
                "mean_120m_points": float(points.mean()) if len(points) else None,
                "median_120m_points": float(points.median()) if len(points) else None,
                "bootstrap_ci_low": ci.get("ci_low"),
                "bootstrap_ci_high": ci.get("ci_high"),
                "frozen_spec_sha256": FROZEN_SPEC_SHA256,
                "retuned": False,
            }
        )
    return pd.DataFrame.from_records(rows)


def max_drawdown_from_pnl(ledger: pd.DataFrame) -> float | None:
    if ledger is None or ledger.empty:
        return None
    ok = ledger[ledger["status"].astype(str).eq("OK")].copy()
    if ok.empty:
        return None
    ok = ok.sort_values(["session", "signal_time", "moneyness"])
    pnl = pd.to_numeric(ok["net_pnl"], errors="coerce").dropna()
    if pnl.empty:
        return None
    equity = pnl.cumsum()
    drawdown = equity.cummax() - equity
    return float(drawdown.max())


def stage3_gate_report(
    *,
    option_ledger: pd.DataFrame,
    gate_points: dict,
    cross_index: pd.DataFrame,
    drawdown_budget: float | None = None,
    forward_min_trades: int = 40,
) -> dict:
    """Report the preregistered Stage-3 gates without silently relaxing them."""
    ok = (
        option_ledger[option_ledger["status"].astype(str).eq("OK")].copy()
        if option_ledger is not None and not option_ledger.empty
        else pd.DataFrame()
    )
    option_status = "WAITING_DATA"
    option_mean = None
    if not ok.empty:
        option_mean = float(pd.to_numeric(ok["net_pnl"], errors="coerce").mean())
        has_expiry = bool(ok["expiry_day"].fillna(False).astype(bool).any())
        has_non_expiry = bool((~ok["expiry_day"].fillna(False).astype(bool)).any())
        option_status = "PASS" if option_mean > 0 and has_expiry and has_non_expiry else "FAIL"

    cross_status = "WAITING_DATA"
    if cross_index is not None and not cross_index.empty:
        means = pd.to_numeric(cross_index["mean_120m_points"], errors="coerce").dropna()
        cross_status = "PASS" if bool((means > 0).any()) else "FAIL"

    forward_status = "WAITING_40_TRADES"
    dd = max_drawdown_from_pnl(ok)
    if len(ok) >= int(forward_min_trades):
        if drawdown_budget is None:
            forward_status = "WAITING_DRAWDOWN_BUDGET"
        else:
            total = float(pd.to_numeric(ok["net_pnl"], errors="coerce").sum())
            forward_status = "PASS" if total >= 0 and dd is not None and dd <= float(drawdown_budget) else "FAIL"

    report = {
        "frozen_spec_sha256": FROZEN_SPEC_SHA256,
        "option_pnl": {
            "status": option_status,
            "trade_count": int(len(ok)),
            "mean_net_pnl": option_mean,
            "bootstrap": session_bootstrap_mean_ci(ok, "net_pnl") if not ok.empty else None,
        },
        "cross_index_replication": {"status": cross_status},
        "gate_points_check": gate_points,
        "forward_paper": {
            "status": forward_status,
            "trade_count": int(len(ok)),
            "required_trades": int(forward_min_trades),
            "total_net_pnl": float(pd.to_numeric(ok["net_pnl"], errors="coerce").sum()) if not ok.empty else None,
            "max_drawdown": dd,
            "drawdown_budget": drawdown_budget,
        },
        "production_ready": False,
    }
    statuses = [
        report["option_pnl"]["status"],
        report["cross_index_replication"]["status"],
        report["gate_points_check"].get("status"),
        report["forward_paper"]["status"],
    ]
    report["all_stage3_gates_pass"] = all(status == "PASS" for status in statuses)
    report["production_ready"] = bool(report["all_stage3_gates_pass"])
    return report


def write_stage3_artifacts(
    output_dir,
    *,
    signals: pd.DataFrame,
    option_ledger: pd.DataFrame,
    option_summary: pd.DataFrame,
    cross_index: pd.DataFrame,
    gate_report: dict,
    source_files: Iterable[str | Path] = (),
) -> dict:
    """Write a reproducible Stage-3 evidence package with SHA-256 hashes."""
    output = Path(output_dir)
    output.mkdir(parents=True, exist_ok=True)
    files = {}
    for name, frame in (
        ("frozen_signals", signals),
        ("option_pnl_ledger", option_ledger),
        ("option_pnl_summary", option_summary),
        ("cross_index_replication", cross_index),
    ):
        path = output / f"{name}.csv"
        (frame if frame is not None else pd.DataFrame()).to_csv(path, index=False)
        files[name] = path

    report_path = output / "stage3_gate_report.json"
    report_path.write_text(json.dumps(gate_report, indent=2, sort_keys=True, default=str), encoding="utf-8")
    files["gate_report"] = report_path

    def sha(path: Path) -> str:
        h = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                h.update(chunk)
        return h.hexdigest()

    manifest = {
        "research_stage": "Index Option Buying V1 / Stage 3",
        "audit_date": AUDIT_DATE,
        "forward_start": STAGE3_FORWARD_START,
        "frozen_spec": asdict(FROZEN_SPEC),
        "frozen_spec_sha256": FROZEN_SPEC_SHA256,
        "fee_model_version": FEE_MODEL_VERSION,
        "source_files": [str(Path(p)) for p in source_files],
        "artifacts": {
            key: {"file": path.name, "sha256": sha(path)}
            for key, path in files.items()
        },
        "research_only": True,
        "production_deployed": False,
        "v12_v121_recorder_modified": False,
        "trial25_modified": False,
        "notes": [
            "No parameter grid is allowed in Stage 3.",
            "The 2026 historical holdout is already viewed and cannot confirm the frozen spec.",
            "Historical option P&L must use real executable quotes; no synthetic option prices are fabricated.",
        ],
    }
    manifest_path = output / "stage3_manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8")
    return {"manifest": manifest, "manifest_path": manifest_path, **files}
