"""Live ITM1 friction extraction for Stage-3B.

Consumes the existing V12.1 NIFTY micro recorder output. It does not create a
second market-data stream, place orders, or alter the frozen Stage-3 signal.

Primary friction measure is the observed one-lot top-of-book round trip:
  entry half-spread + exit half-spread + statutory/broker charges per option unit.

This is LIVE_EXECUTABLE_TOP_OF_BOOK evidence when:
- the exact frozen Stage-3 signal exists,
- ITM1 entry ask and +120m same-contract exit bid exist,
- top-of-book quantity covers one lot,
- quotes pass the Stage-3 freshness limit,
- both sides of the book are present at entry and exit.

Optional real fill prices can upgrade a row to LIVE_ACTUAL_FILL. The actual-fill
friction is measured versus contemporaneous mids and includes charges calculated
from the real fill prices.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Iterable

import pandas as pd

from .index_option_fees import FEE_MODEL_VERSION, calculate_option_charges
from .index_option_stage3 import (
    FROZEN_SPEC_SHA256,
    build_forward_paper_ledger,
    load_v121_micro_jsonl,
)

TOP_BOOK_CLASS = "LIVE_EXECUTABLE_TOP_OF_BOOK"
ACTUAL_FILL_CLASS = "LIVE_ACTUAL_FILL"
PRIMARY_EXPRESSION = "ITM1"
DEFAULT_FORWARD_START = "2026-09-22"


def _numeric(value):
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if pd.notna(out) else None


def _exact_quote(
    quotes: pd.DataFrame,
    *,
    snapshot_ts,
    instrument_token=None,
    tradingsymbol=None,
) -> dict | None:
    if quotes is None or quotes.empty:
        return None
    target = pd.Timestamp(snapshot_ts)
    work = quotes[pd.to_datetime(quotes["snapshot_ts"], errors="coerce").eq(target)].copy()
    if work.empty:
        return None

    if instrument_token is not None and "instrument_token" in work.columns:
        tok = pd.to_numeric(work["instrument_token"], errors="coerce")
        exact = work[tok.eq(float(instrument_token))]
        if not exact.empty:
            return exact.iloc[0].to_dict()

    if tradingsymbol and "tradingsymbol" in work.columns:
        exact = work[work["tradingsymbol"].astype(str).eq(str(tradingsymbol))]
        if not exact.empty:
            return exact.iloc[0].to_dict()
    return None


def _valid_book(row: dict | None) -> tuple[bool, str | None]:
    if not row:
        return False, "MISSING_QUOTE_ROW"
    bid = _numeric(row.get("best_bid"))
    ask = _numeric(row.get("best_ask"))
    if bid is None or ask is None or bid <= 0 or ask <= 0:
        return False, "MISSING_TWO_SIDED_BOOK"
    if bid > ask:
        return False, "CROSSED_BOOK"
    return True, None


def _mid(row: dict) -> float:
    return (float(row["best_bid"]) + float(row["best_ask"])) / 2.0


def build_live_itm1_friction_ledger(
    quotes: pd.DataFrame,
    refs: pd.DataFrame,
    *,
    actual_fills: pd.DataFrame | None = None,
    forward_start: str = DEFAULT_FORWARD_START,
) -> pd.DataFrame:
    """Build one live friction row per frozen Stage-3 ITM1 signal.

    The Stage-3 mapper already enforces ask entry, bid exit, quote freshness,
    top-quantity >= one lot, same absolute contract, and +120m exit.
    """
    signals, option_ledger = build_forward_paper_ledger(
        quotes,
        refs,
        moneyness=(PRIMARY_EXPRESSION,),
        forward_start=forward_start,
    )
    if option_ledger is None or option_ledger.empty:
        return pd.DataFrame()

    fills = None
    if actual_fills is not None and not actual_fills.empty:
        fills = actual_fills.copy()
        if "session" not in fills.columns or "tradingsymbol" not in fills.columns:
            raise ValueError("actual fills require session and tradingsymbol")
        fills["session"] = pd.to_datetime(fills["session"], errors="coerce").dt.date.astype(str)
        required = {"entry_fill_price", "exit_fill_price"}
        missing = sorted(required.difference(fills.columns))
        if missing:
            raise ValueError(f"actual fills missing columns: {missing}")

    rows = []
    for _, paper in option_ledger.iterrows():
        base = paper.to_dict()
        session = str(base.get("session") or "")
        status = str(base.get("status") or "")
        out = {
            "session": session,
            "direction": base.get("direction"),
            "signal_time": base.get("signal_time"),
            "exit_target": base.get("exit_target"),
            "moneyness": PRIMARY_EXPRESSION,
            "status": status,
            "tradingsymbol": base.get("tradingsymbol"),
            "instrument_token": base.get("instrument_token"),
            "expiry": base.get("expiry"),
            "expiry_day": base.get("expiry_day"),
            "strike": base.get("strike"),
            "lot_size": base.get("lot_size"),
            "entry_snapshot_ts": base.get("entry_snapshot_ts"),
            "exit_snapshot_ts": base.get("exit_snapshot_ts"),
            "entry_ask": base.get("entry_ask"),
            "exit_bid": base.get("exit_bid"),
            "paper_net_pnl_rupees": base.get("net_pnl"),
            "paper_gross_pnl_rupees": base.get("gross_pnl"),
            "fee_model_version": base.get("fee_model_version"),
            "frozen_spec_sha256": FROZEN_SPEC_SHA256,
            "production_deployed": False,
        }
        if status != "OK":
            out.update({
                "friction_status": "NOT_MEASURABLE",
                "friction_reason": status,
                "evidence_class": None,
                "round_trip_friction_points": None,
                "decision_eligible": False,
            })
            rows.append(out)
            continue

        entry_row = _exact_quote(
            quotes,
            snapshot_ts=base["entry_snapshot_ts"],
            instrument_token=base.get("instrument_token"),
            tradingsymbol=base.get("tradingsymbol"),
        )
        exit_row = _exact_quote(
            quotes,
            snapshot_ts=base["exit_snapshot_ts"],
            instrument_token=base.get("instrument_token"),
            tradingsymbol=base.get("tradingsymbol"),
        )
        entry_ok, entry_reason = _valid_book(entry_row)
        exit_ok, exit_reason = _valid_book(exit_row)
        if not entry_ok or not exit_ok:
            out.update({
                "friction_status": "NOT_MEASURABLE",
                "friction_reason": entry_reason or exit_reason,
                "evidence_class": None,
                "round_trip_friction_points": None,
                "decision_eligible": False,
            })
            rows.append(out)
            continue

        lot = int(base.get("lot_size") or 0)
        if lot <= 0:
            out.update({
                "friction_status": "NOT_MEASURABLE",
                "friction_reason": "INVALID_LOT_SIZE",
                "evidence_class": None,
                "round_trip_friction_points": None,
                "decision_eligible": False,
            })
            rows.append(out)
            continue

        entry_bid = float(entry_row["best_bid"])
        entry_ask = float(entry_row["best_ask"])
        exit_bid = float(exit_row["best_bid"])
        exit_ask = float(exit_row["best_ask"])
        entry_mid = (entry_bid + entry_ask) / 2.0
        exit_mid = (exit_bid + exit_ask) / 2.0
        entry_half_spread = entry_ask - entry_mid
        exit_half_spread = exit_mid - exit_bid

        paper_charges = calculate_option_charges([
            {"side": "BUY", "price": entry_ask, "quantity": lot},
            {"side": "SELL", "price": exit_bid, "quantity": lot},
        ])
        charge_points = float(paper_charges["total"]) / lot
        top_book_friction = entry_half_spread + exit_half_spread + charge_points

        out.update({
            "friction_status": "OK",
            "friction_reason": None,
            "entry_bid": entry_bid,
            "entry_mid": entry_mid,
            "entry_spread_points": entry_ask - entry_bid,
            "entry_half_spread_cost_points": entry_half_spread,
            "entry_bid_qty": int(entry_row.get("bid_qty") or 0),
            "entry_ask_qty": int(entry_row.get("ask_qty") or 0),
            "entry_quote_age_seconds": _numeric(entry_row.get("quote_age_seconds")),
            "exit_ask": exit_ask,
            "exit_mid": exit_mid,
            "exit_spread_points": exit_ask - exit_bid,
            "exit_half_spread_cost_points": exit_half_spread,
            "exit_bid_qty": int(exit_row.get("bid_qty") or 0),
            "exit_ask_qty": int(exit_row.get("ask_qty") or 0),
            "exit_quote_age_seconds": _numeric(exit_row.get("quote_age_seconds")),
            "charges_rupees": float(paper_charges["total"]),
            "charge_points": charge_points,
            "top_of_book_friction_points": float(top_book_friction),
            "entry_fill_price": None,
            "exit_fill_price": None,
            "entry_slippage_points": None,
            "exit_slippage_points": None,
            "actual_fill_charge_points": None,
            "evidence_class": TOP_BOOK_CLASS,
            # Top-of-book ask/bid with sufficient quantity is the preregistered
            # live executable friction observation. Actual fills are an upgrade,
            # not a requirement to count the row.
            "round_trip_friction_points": float(top_book_friction),
            "decision_eligible": True,
            "actual_slippage_measured": False,
        })

        if fills is not None:
            matched = fills[
                fills["session"].eq(session)
                & fills["tradingsymbol"].astype(str).eq(str(base.get("tradingsymbol")))
            ]
            if len(matched) > 1:
                raise ValueError(
                    f"multiple actual-fill rows for session={session} symbol={base.get('tradingsymbol')}"
                )
            if len(matched) == 1:
                fill = matched.iloc[0]
                entry_fill = _numeric(fill.get("entry_fill_price"))
                exit_fill = _numeric(fill.get("exit_fill_price"))
                if entry_fill is None or exit_fill is None or entry_fill <= 0 or exit_fill <= 0:
                    raise ValueError(
                        f"invalid actual fill prices for session={session} symbol={base.get('tradingsymbol')}"
                    )
                actual_charges = calculate_option_charges([
                    {"side": "BUY", "price": entry_fill, "quantity": lot},
                    {"side": "SELL", "price": exit_fill, "quantity": lot},
                ])
                entry_slippage = entry_fill - entry_ask
                exit_slippage = exit_bid - exit_fill
                actual_charge_points = float(actual_charges["total"]) / lot
                actual_friction = (
                    (entry_fill - entry_mid)
                    + (exit_mid - exit_fill)
                    + actual_charge_points
                )
                out.update({
                    "entry_fill_price": entry_fill,
                    "exit_fill_price": exit_fill,
                    "entry_slippage_points": float(entry_slippage),
                    "exit_slippage_points": float(exit_slippage),
                    "actual_fill_charge_points": actual_charge_points,
                    "round_trip_friction_points": float(actual_friction),
                    "evidence_class": ACTUAL_FILL_CLASS,
                    "actual_slippage_measured": True,
                })

        rows.append(out)

    ledger = pd.DataFrame.from_records(rows)
    if ledger.empty:
        return ledger
    sort_cols = [c for c in ("session", "signal_time", "tradingsymbol") if c in ledger.columns]
    return ledger.sort_values(sort_cols).reset_index(drop=True)


def summarize_live_friction(ledger: pd.DataFrame) -> dict:
    if ledger is None or ledger.empty:
        return {
            "status": "WAITING_FRICTION",
            "eligible_count": 0,
            "minimum_samples": 20,
        }
    ok = ledger[
        ledger.get("decision_eligible", False).fillna(False).astype(bool)
        & ledger["friction_status"].astype(str).eq("OK")
    ].copy()
    x = pd.to_numeric(ok["round_trip_friction_points"], errors="coerce").dropna()
    n = len(x)
    class_counts = (
        ok["evidence_class"].fillna("UNKNOWN").astype(str).value_counts().to_dict()
        if not ok.empty else {}
    )
    return {
        "status": "READY" if n >= 20 else "WAITING_FRICTION",
        "eligible_count": int(n),
        "minimum_samples": 20,
        "mean_round_trip_friction_points": float(x.mean()) if n else None,
        "median_round_trip_friction_points": float(x.median()) if n else None,
        "p75_round_trip_friction_points": float(x.quantile(0.75)) if n else None,
        "p90_round_trip_friction_points": float(x.quantile(0.90)) if n else None,
        "max_round_trip_friction_points": float(x.max()) if n else None,
        "actual_fill_count": int((ok["evidence_class"] == ACTUAL_FILL_CLASS).sum()) if not ok.empty else 0,
        "top_of_book_count": int((ok["evidence_class"] == TOP_BOOK_CLASS).sum()) if not ok.empty else 0,
        "evidence_class_counts": class_counts,
        "fee_model_version": FEE_MODEL_VERSION,
        "frozen_spec_sha256": FROZEN_SPEC_SHA256,
    }


def build_from_v121_files(
    paths: Iterable[str | Path],
    *,
    actual_fills: pd.DataFrame | None = None,
    forward_start: str = DEFAULT_FORWARD_START,
) -> tuple[pd.DataFrame, dict]:
    quotes, refs = load_v121_micro_jsonl(paths)
    ledger = build_live_itm1_friction_ledger(
        quotes,
        refs,
        actual_fills=actual_fills,
        forward_start=forward_start,
    )
    return ledger, summarize_live_friction(ledger)


def write_friction_artifacts(
    output_dir,
    *,
    ledger: pd.DataFrame,
    summary: dict,
    source_files: Iterable[str | Path],
) -> dict:
    output = Path(output_dir)
    output.mkdir(parents=True, exist_ok=True)
    ledger_path = output / "stage3b_live_itm1_friction.csv"
    summary_path = output / "stage3b_live_itm1_friction_summary.json"
    manifest_path = output / "stage3b_live_itm1_friction_manifest.json"

    ledger.to_csv(ledger_path, index=False)
    summary_path.write_text(
        json.dumps(summary, indent=2, sort_keys=True, default=str),
        encoding="utf-8",
    )
    manifest = {
        "research_stage": "Index Option Buying V1 / Stage 3B live friction",
        "frozen_spec_sha256": FROZEN_SPEC_SHA256,
        "primary_expression": PRIMARY_EXPRESSION,
        "forward_start": DEFAULT_FORWARD_START,
        "source": "V12.1 NIFTY 5-second micro recorder",
        "top_of_book_evidence_class": TOP_BOOK_CLASS,
        "actual_fill_evidence_class": ACTUAL_FILL_CLASS,
        "primary_metric": "round_trip_friction_points",
        "minimum_samples": 20,
        "top_of_book_definition": (
            "entry half-spread + exit half-spread + modeled one-lot charges per option unit"
        ),
        "actual_fill_definition": (
            "entry fill versus entry mid + exit mid versus exit fill + actual-fill charges per option unit"
        ),
        "source_files": [str(Path(p)) for p in source_files],
        "research_only": True,
        "production_deployed": False,
        "v12_v121_recorder_modified": False,
        "trial25_modified": False,
    }
    manifest_path.write_text(
        json.dumps(manifest, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    return {
        "ledger_path": ledger_path,
        "summary_path": summary_path,
        "manifest_path": manifest_path,
        "summary": summary,
    }
