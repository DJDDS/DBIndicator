"""Point-in-time monthly data builder for V11 Trial 24."""
from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path

import numpy as np
import pandas as pd

from .iima_factors import IIMAFactorClient
from .nse_cash_history import NSECashArchiveClient
from .nse_futures_history import NSEFuturesArchiveClient
from .nse_corporate_actions import NSECorporateActionClient, UnhandledCorporateAction, total_return_between

WARMUP_START = "2010-01-01"
TRIAL24_PREFINAL_OUTCOME_END = "2023-05-31"
PLANNED_FINAL_SIGNAL_MONTHS = 31
MIN_MEMBER_RETURN_COVERAGE = 0.95


def _month_key(day) -> pd.Timestamp:
    return pd.Timestamp(day).to_period("M").to_timestamp("M")


def _actions_between(actions, start_day, end_day):
    start = pd.Timestamp(start_day)
    end = pd.Timestamp(end_day)
    out = []
    for a in actions or []:
        d = pd.to_datetime(a.get("ex_date"), errors="coerce")
        if not pd.isna(d) and start < pd.Timestamp(d) <= end:
            out.append(a)
    return out


def selected_futstk_contract_metadata(fo: pd.DataFrame, trade_date) -> dict[str, dict]:
    """Coverage-only metadata for the frozen V11.1 FUTSTK execution rule.

    The selected contract is the nearest expiry that is not expired on the
    signal month-end trading date.  We retain only availability/provenance
    metadata; V11.1 does not compute futures P&L from these values.
    """
    if fo is None or fo.empty:
        return {}
    required = {"symbol", "expiry"}
    if not required.issubset(set(fo.columns)):
        return {}
    day = pd.Timestamp(trade_date).normalize()
    x = fo.copy()
    x["symbol"] = x["symbol"].astype(str).str.strip().str.upper()
    x["expiry"] = pd.to_datetime(x["expiry"], errors="coerce").dt.normalize()
    x = x[x["symbol"].ne("") & x["expiry"].notna() & x["expiry"].ge(day)].copy()
    if x.empty:
        return {}
    x = x.sort_values(["symbol", "expiry"], kind="mergesort")
    out: dict[str, dict] = {}
    for symbol, sub in x.groupby("symbol", sort=True):
        row = sub.iloc[0]
        settle = pd.to_numeric(pd.Series([row.get("settle")]), errors="coerce").iloc[0]
        close = pd.to_numeric(pd.Series([row.get("close")]), errors="coerce").iloc[0]
        lot = pd.to_numeric(pd.Series([row.get("lot_size")]), errors="coerce").iloc[0]
        settle_ok = bool(pd.notna(settle) and float(settle) > 0)
        close_ok = bool(pd.notna(close) and float(close) > 0)
        price_field = "settle" if settle_ok else ("close" if close_ok else None)
        out[str(symbol)] = {
            "expiry": pd.Timestamp(row["expiry"]).date().isoformat(),
            "lot_size_available": bool(pd.notna(lot) and float(lot) > 0),
            "price_available": bool(price_field is not None),
            "price_field": price_field,
            "source_format": str(row.get("source_format") or "UNKNOWN"),
        }
    return out


def build_monthly_inputs_from_snapshots(snapshots: list[dict], actions_by_symbol: dict[str, list[dict]]):
    """Convert month-end cash/F&O snapshots into adjusted monthly returns.

    ``snapshots`` store the *actual trading date*, but frames are indexed by
    calendar month-end so they align with monthly factor data.
    """
    snaps = sorted(list(snapshots or []), key=lambda x: pd.Timestamp(x["date"]))
    symbols = sorted({str(s).upper() for x in snaps for s in set(x.get("cash_close") or {}) | set(x.get("fno_symbols") or set())})
    idx = pd.DatetimeIndex([_month_key(x["date"]) for x in snaps])
    closes = pd.DataFrame(np.nan, index=idx, columns=symbols, dtype=float)
    membership = pd.DataFrame(False, index=idx, columns=symbols, dtype=bool)
    actual_dates: list[pd.Timestamp] = []
    for x, month in zip(snaps, idx):
        actual = pd.Timestamp(x["date"]).normalize()
        actual_dates.append(actual)
        for s, v in (x.get("cash_close") or {}).items():
            s = str(s).upper()
            if s in closes.columns:
                closes.at[month, s] = pd.to_numeric(pd.Series([v]), errors="coerce").iloc[0]
        for s in (x.get("fno_symbols") or set()):
            s = str(s).upper()
            if s in membership.columns:
                membership.at[month, s] = True

    returns = pd.DataFrame(np.nan, index=idx, columns=symbols, dtype=float)
    unhandled = 0
    computed = 0
    for i in range(1, len(snaps)):
        prev_day, day = actual_dates[i - 1], actual_dates[i]
        month = idx[i]
        for s in symbols:
            p0, p1 = closes.iloc[i - 1][s], closes.iloc[i][s]
            if not (pd.notna(p0) and pd.notna(p1) and float(p0) > 0 and float(p1) >= 0):
                continue
            acts = _actions_between(actions_by_symbol.get(s, []), prev_day, day)
            try:
                returns.at[month, s] = total_return_between(float(p0), float(p1), acts)
                computed += 1
            except UnhandledCorporateAction:
                unhandled += 1
                returns.at[month, s] = np.nan

    denom = int(membership.iloc[1:].to_numpy(dtype=bool).sum()) if len(membership) > 1 else 0
    valid_member = 0
    if denom:
        mask = membership.iloc[1:]
        valid_member = int((returns.iloc[1:].notna() & mask).to_numpy().sum())
    coverage = valid_member / denom if denom else 0.0
    meta = {
        "months": int(len(idx)),
        "symbols": int(len(symbols)),
        "returns_computed": int(computed),
        "unhandled_action_returns": int(unhandled),
        "member_return_coverage": float(coverage),
        "actual_month_end_dates": [d.date().isoformat() for d in actual_dates],
    }
    return returns, membership, meta


def _resolve_snapshot(month, fo_client, cash_client):
    month_end = pd.Timestamp(month).to_period("M").to_timestamp("M")
    last_missing = None
    for offset in range(0, 8):
        d = (month_end - pd.Timedelta(days=offset)).normalize()
        if d.weekday() >= 5:
            continue
        try:
            fo = fo_client.fetch_day(d.date())
            cash = cash_client.fetch_day(d.date())
        except FileNotFoundError as exc:
            last_missing = exc
            continue
        if fo is None or fo.empty or cash is None or cash.empty:
            continue
        fno = set(fo["symbol"].dropna().astype(str).str.strip().str.upper().unique())
        cash_eq = cash[["symbol", "close"]].copy()
        cash_eq["symbol"] = cash_eq["symbol"].astype(str).str.strip().str.upper()
        cash_eq["close"] = pd.to_numeric(cash_eq["close"], errors="coerce")
        cash_close = dict(cash_eq.dropna(subset=["close"]).drop_duplicates("symbol", keep="last")[["symbol", "close"]].itertuples(index=False, name=None))
        return {
            "date": d.date().isoformat(),
            "fno_symbols": fno,
            "cash_close": cash_close,
            "futures_contracts": selected_futstk_contract_metadata(fo, d),
        }
    raise FileNotFoundError(f"no common NSE FO/CM month-end archive for {month_end:%Y-%m}: {last_missing}")


def build_trial24_inputs(cache_dir: str | Path, progress_cb=None) -> dict:
    """Load pinned factors and official NSE monthly snapshots for Trial 24."""
    root = Path(cache_dir)
    root.mkdir(parents=True, exist_ok=True)
    fo_client = NSEFuturesArchiveClient(cache_dir=root / "fo", prefer_market_activity=False)
    cash_client = NSECashArchiveClient(cache_dir=root / "cm")
    factor_client = IIMAFactorClient(root / "factors")
    factors, factor_meta = factor_client.load_monthly(
        start=WARMUP_START,
        end=TRIAL24_PREFINAL_OUTCOME_END,
        required_factors=("rm_rf", "smb", "hml", "rf"),
        require_complete_window=True,
    )

    months = pd.period_range(WARMUP_START, TRIAL24_PREFINAL_OUTCOME_END, freq="M")
    snapshots = []
    for i, p in enumerate(months, start=1):
        if progress_cb:
            progress_cb("MONTH_END_ARCHIVES", i - 1, len(months), str(p))
        snapshots.append(_resolve_snapshot(p.to_timestamp("M"), fo_client, cash_client))
        if progress_cb:
            progress_cb("MONTH_END_ARCHIVES", i, len(months), snapshots[-1]["date"])

    union_symbols = sorted({s for x in snapshots for s in x["fno_symbols"]})
    ca_client = NSECorporateActionClient(cache_dir=root / "corporate_actions")
    actions, ca_meta = ca_client.load_normalized(2010, 2023, symbols=union_symbols)
    returns, membership, data_meta = build_monthly_inputs_from_snapshots(snapshots, actions)

    # Keep only union F&O names; cash snapshots intentionally contained all EQ.
    returns = returns.reindex(columns=union_symbols)
    membership = membership.reindex(columns=union_symbols).fillna(False).astype(bool)
    factors = factors.reindex(returns.index)
    factor_coverage = float(factors[["rm_rf", "smb", "hml", "rf"]].notna().all(axis=1).mean()) if len(factors) else 0.0
    readiness = bool(data_meta["member_return_coverage"] >= MIN_MEMBER_RETURN_COVERAGE and factor_coverage >= 0.95)

    manifest_payload = {
        "factor_sha256": factor_meta.get("sha256"),
        "factor_release": factor_meta.get("release"),
        "corporate_action_year_sha256": ca_meta.get("year_sha256"),
        "actual_month_end_dates": data_meta.get("actual_month_end_dates"),
        "symbols": union_symbols,
        "member_return_coverage": data_meta.get("member_return_coverage"),
        "factor_coverage": factor_coverage,
    }
    manifest_sha = hashlib.sha256(json.dumps(manifest_payload, sort_keys=True, separators=(",", ":"), default=str).encode()).hexdigest()
    return {
        "monthly_returns": returns,
        "membership": membership,
        "factors": factors,
        "futures_contracts_by_month": {
            _month_key(x["date"]): dict(x.get("futures_contracts") or {}) for x in snapshots
        },
        "data_readiness": readiness,
        "meta": {**data_meta, "factor_coverage": factor_coverage, "factor_source": factor_meta, "corporate_actions": ca_meta, "manifest_sha256": manifest_sha},
    }
