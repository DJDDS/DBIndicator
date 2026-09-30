#!/usr/bin/env python3
"""Underlying-first impulse -> pullback -> reclaim research.

Research-only. The purpose is to test whether waiting for a causal pullback/
reclaim after an abnormal underlying impulse improves forward continuation
relative to entering the impulse immediately.

No option information is used to discover the stock signal.
"""
from __future__ import annotations

import argparse
import json
import math
from pathlib import Path

import numpy as np
import pandas as pd

DEV_END = "2024-09-30"
VAL_START = "2024-10-01"
VAL_END = "2025-09-30"
OOS_START = "2025-10-01"
OOS_END = "2026-09-25"
BARRIERS_BPS = (10, 20, 30)
PB_BINS = [(0.0, 0.20), (0.20, 0.35), (0.35, 0.50), (0.50, 0.65), (0.65, 0.80), (0.80, 1.00)]
COVID_START = "2020-03-11"
COVID_END = "2021-12-31"
POST_OCT24_START = "2024-10-01"
US_IRAN_START = "2026-02-28"


def split_name(d: str) -> str:
    if d <= DEV_END:
        return "DEV"
    if VAL_START <= d <= VAL_END:
        return "VAL"
    if OOS_START <= d <= OOS_END:
        return "OOS"
    return "OTHER"


def robust_sigma(x: pd.Series, window: int = 60) -> pd.Series:
    # Causal scale: use only returns completed before the current bar.
    lag = x.shift(1)
    med = lag.rolling(window, min_periods=30).median()
    mad = (lag - med).abs().rolling(window, min_periods=30).median()
    sig = 1.4826 * mad
    fallback = lag.rolling(window, min_periods=30).std(ddof=0)
    return sig.where(sig > 1e-8, fallback).clip(lower=1e-8)


def load_nifty(root: Path) -> pd.DataFrame:
    x = pd.read_parquet(root / "context" / "NIFTY50_5m.parquet")
    x = x[["timestamp", "close"]].dropna().drop_duplicates("timestamp").sort_values("timestamp")
    x["r_mkt"] = np.log(x["close"] / x["close"].shift(1))
    return x[["timestamp", "r_mkt"]]


def nifty_daily_regimes(root: Path) -> pd.DataFrame:
    """Prior-completed-day NIFTY regime diagnostics for intraday event stratification."""
    nf = root / "context" / "NIFTY50_daily.parquet"
    n = pd.read_parquet(nf)[["timestamp", "close"]].dropna().drop_duplicates("timestamp").sort_values("timestamp")
    n["trade_date"] = n.timestamp.dt.date.astype(str)
    n["nifty_ret1"] = n.close.pct_change()
    n["nifty_ret20"] = n.close / n.close.shift(20) - 1.0
    n["nifty_ret60"] = n.close / n.close.shift(60) - 1.0
    n["nifty_dd126"] = n.close / n.close.shift(1).rolling(126, min_periods=40).max() - 1.0
    n["nifty_regime"] = np.select(
        [
            n.nifty_dd126 <= -0.10,
            (n.nifty_ret20 < 0) & (n.nifty_ret60 < 0),
            (n.nifty_ret20 > 0) & (n.nifty_ret60 > 0),
        ],
        ["STRESS_DRAWDOWN", "BEAR", "BULL"],
        default="MIXED",
    )
    # All regime fields used for an intraday date are known only from the prior
    # completed NIFTY session. Shifting the date forward enforces causality.
    n["trade_date"] = n["trade_date"].shift(-1)
    return n[["trade_date", "nifty_ret20", "nifty_ret60", "nifty_dd126", "nifty_regime"]].dropna(subset=["trade_date"])


def named_regime_flags(d: str) -> dict:
    return {
        "covid_period": COVID_START <= d <= COVID_END,
        "post_oct2024_stress_window": d >= POST_OCT24_START,
        "us_iran_conflict": d >= US_IRAN_START,
    }


def daily_market_period_summary(root: Path) -> dict:
    """Daily NIFTY descriptors for the requested historical stress periods."""
    nf = root / "context" / "NIFTY50_daily.parquet"
    n = pd.read_parquet(nf)[["timestamp", "close"]].dropna().drop_duplicates("timestamp").sort_values("timestamp")
    n["trade_date"] = n.timestamp.dt.date.astype(str)
    n["ret"] = n.close.pct_change()
    periods = {
        "COVID_2020_TO_LATE_2021": (COVID_START, COVID_END),
        "POST_OCT2024_STRESS_WINDOW": (POST_OCT24_START, OOS_END),
        "US_IRAN_CONFLICT": (US_IRAN_START, OOS_END),
    }
    out = {}
    for name, (a, b) in periods.items():
        z = n[(n.trade_date >= a) & (n.trade_date <= b)].copy()
        if z.empty:
            out[name] = {"days": 0}
            continue
        peak = z.close.cummax()
        dd = z.close / peak - 1.0
        out[name] = {
            "start": a,
            "end": b,
            "days": int(len(z)),
            "total_return_pct": float((z.close.iloc[-1] / z.close.iloc[0] - 1.0) * 100.0),
            "mean_daily_bps": float(z.ret.mean() * 10000.0),
            "daily_vol_annualized_pct": float(z.ret.std(ddof=0) * np.sqrt(252.0) * 100.0),
            "max_drawdown_pct": float(dd.min() * 100.0),
            "down_day_rate": float((z.ret < 0).mean()),
        }
    return out


def daily_context(root: Path, sym: str) -> pd.DataFrame:
    sf = root / "daily" / f"{sym}.parquet"
    nf = root / "context" / "NIFTY50_daily.parquet"
    if not sf.exists() or not nf.exists():
        return pd.DataFrame(columns=["trade_date", "daily_vol_ratio", "daily_rel5"])
    s = pd.read_parquet(sf)[["timestamp", "close"]].dropna().sort_values("timestamp")
    n = pd.read_parquet(nf)[["timestamp", "close"]].dropna().sort_values("timestamp")
    s["trade_date"] = s.timestamp.dt.date.astype(str)
    n["trade_date"] = n.timestamp.dt.date.astype(str)
    s["rs"] = np.log(s.close / s.close.shift(1))
    n["rm"] = np.log(n.close / n.close.shift(1))
    z = s.merge(n[["trade_date", "rm"]], on="trade_date", how="left")
    vol20 = z.rs.rolling(20, min_periods=15).std(ddof=0)
    med252 = vol20.shift(1).rolling(252, min_periods=60).median()
    z["daily_vol_ratio"] = vol20 / med252
    z["daily_rel5"] = (z.rs - z.rm).rolling(5, min_periods=3).sum()
    # Context available at intraday t is prior completed daily bar only.
    z["trade_date"] = z["trade_date"].shift(-1)
    return z[["trade_date", "daily_vol_ratio", "daily_rel5"]].dropna(subset=["trade_date"])


def features_for_symbol(root: Path, sym: str, nifty: pd.DataFrame) -> pd.DataFrame:
    f = root / "intraday5" / f"{sym}.parquet"
    if not f.exists():
        return pd.DataFrame()
    x = pd.read_parquet(f)
    if x.empty:
        return x
    x = x.dropna(subset=["timestamp", "open", "high", "low", "close"]).sort_values("timestamp")
    x = x.merge(nifty, on="timestamp", how="left")
    x["trade_date"] = x.timestamp.dt.date.astype(str)
    x["r_stock"] = np.log(x.close / x.close.shift(1))

    # Keep beta strictly causal by fitting on lagged completed bars.
    rs_lag = x.r_stock.shift(1)
    rm_lag = x.r_mkt.shift(1)
    cov = (rs_lag * rm_lag).rolling(60, min_periods=30).mean()
    var = (rm_lag * rm_lag).rolling(60, min_periods=30).mean()
    beta = cov / (var + 1e-12)
    x["resid"] = x.r_stock - beta * x.r_mkt
    x["sigma"] = robust_sigma(x.resid, 60)

    # Reset rolling sums at session boundaries.
    g = x.groupby("trade_date", sort=False)
    x["resid3"] = g.resid.rolling(3, min_periods=3).sum().reset_index(level=0, drop=True)
    x["z3"] = x.resid3 / (x.sigma * math.sqrt(3.0))
    absres = x.resid.abs()
    gross3 = g.apply(lambda q: q.resid.abs().rolling(3, min_periods=3).sum(), include_groups=False)
    if isinstance(gross3.index, pd.MultiIndex):
        gross3 = gross3.reset_index(level=0, drop=True)
    x["impulse_eff"] = x.resid3.abs() / gross3.replace(0, np.nan)

    # Volume participation ratio: impulse 3-bar median / prior 12-bar median.
    v = pd.to_numeric(x.volume, errors="coerce")
    x["vol_prior12"] = g.volume.rolling(12, min_periods=6).median().reset_index(level=0, drop=True).shift(3)
    x["vol_imp3"] = g.volume.rolling(3, min_periods=3).median().reset_index(level=0, drop=True)
    x["impulse_vol_ratio"] = x.vol_imp3 / x.vol_prior12.replace(0, np.nan)

    dc = daily_context(root, sym)
    if not dc.empty:
        x = x.merge(dc, on="trade_date", how="left")
    else:
        x["daily_vol_ratio"] = np.nan
        x["daily_rel5"] = np.nan
    x["symbol"] = sym
    return x.reset_index(drop=True)


def forward_metrics(day: pd.DataFrame, pos: int, direction: int, prefix: str) -> dict:
    out = {}
    p0 = float(day.close.iloc[pos])
    for bars, mins in ((1, 5), (2, 10), (3, 15), (6, 30)):
        j = pos + bars
        out[f"{prefix}_fwd_{mins}m_bps"] = (
            direction * (float(day.close.iloc[j]) / p0 - 1.0) * 10000.0
            if j < len(day) else np.nan
        )
    end = min(len(day), pos + 7)
    for b in BARRIERS_BPS:
        status = "NONE"
        for j in range(pos + 1, end):
            hi = float(day.high.iloc[j])
            lo = float(day.low.iloc[j])
            if direction > 0:
                tgt = (hi / p0 - 1.0) * 10000.0 >= b
                stp = (lo / p0 - 1.0) * 10000.0 <= -b
            else:
                tgt = (1.0 - lo / p0) * 10000.0 >= b
                stp = (1.0 - hi / p0) * 10000.0 <= -b
            if tgt or stp:
                status = "STOP" if stp else "TARGET"
                break
        out[f"{prefix}_fp_{b}bps"] = status
    return out


def campaigns_for_symbol(x: pd.DataFrame, threshold: float) -> list[dict]:
    rows = []
    if x.empty:
        return rows
    for d, day in x.groupby("trade_date", sort=True):
        day = day.reset_index(drop=True)
        if len(day) < 25:
            continue
        z = day.z3.to_numpy(float)
        prev = np.r_[np.nan, np.abs(z[:-1])]
        candidates = np.where(np.isfinite(z) & (np.abs(z) >= threshold) & ((~np.isfinite(prev)) | (prev < threshold)))[0]
        last_impulse = -99
        for i in candidates:
            if i < 4 or i - last_impulse < 6 or i + 2 >= len(day):
                continue
            direction = 1 if z[i] > 0 else -1
            start = i - 3
            p_start = float(day.close.iloc[start])
            p_imp = float(day.close.iloc[i])
            impulse_abs = direction * (p_imp - p_start)
            if impulse_abs <= 0:
                continue
            last_impulse = i
            base = {
                "symbol": str(day.symbol.iloc[i]),
                "trade_date": str(d),
                "split": split_name(str(d)),
                "impulse_ts": day.timestamp.iloc[i],
                "direction": direction,
                "impulse_z": float(z[i]),
                "impulse_move_bps": direction * (p_imp / p_start - 1.0) * 10000.0,
                "impulse_efficiency": float(day.impulse_eff.iloc[i]) if np.isfinite(day.impulse_eff.iloc[i]) else np.nan,
                "impulse_volume_ratio": float(day.impulse_vol_ratio.iloc[i]) if np.isfinite(day.impulse_vol_ratio.iloc[i]) else np.nan,
                "daily_vol_ratio": float(day.daily_vol_ratio.iloc[i]) if np.isfinite(day.daily_vol_ratio.iloc[i]) else np.nan,
                "daily_rel5_bps": float(day.daily_rel5.iloc[i] * 10000.0) if np.isfinite(day.daily_rel5.iloc[i]) else np.nan,
                "impulse_entry": p_imp,
            }
            base.update(forward_metrics(day, i, direction, "impulse"))

            # Causal state search: wait for at least one adverse bar, then require
            # a directional close through the prior two closes with positive
            # market-adjusted residual. No future minimum is used to trigger.
            adverse_seen = False
            pb_vols = []
            pb_extreme = p_imp
            reclaim = None
            for k in range(i + 1, min(len(day), i + 9)):
                cc = float(day.close.iloc[k])
                pp = float(day.close.iloc[k - 1])
                bar_dir = direction * (cc / pp - 1.0)
                if bar_dir < 0:
                    adverse_seen = True
                if adverse_seen:
                    if np.isfinite(day.volume.iloc[k]):
                        pb_vols.append(float(day.volume.iloc[k]))
                    if direction > 0:
                        pb_extreme = min(pb_extreme, float(day.low.iloc[k]))
                    else:
                        pb_extreme = max(pb_extreme, float(day.high.iloc[k]))
                    adverse = direction * (p_imp - pb_extreme)
                    ratio = adverse / impulse_abs if impulse_abs > 0 else np.nan
                    if np.isfinite(ratio) and ratio >= 1.0:
                        break
                    if k >= i + 2:
                        prior2 = day.close.iloc[k-2:k].to_numpy(float)
                        breaks = cc > np.max(prior2) if direction > 0 else cc < np.min(prior2)
                        resid_ok = np.isfinite(day.resid.iloc[k]) and direction * float(day.resid.iloc[k]) > 0
                        if breaks and resid_ok:
                            reclaim = k
                            break
            if reclaim is None:
                base["campaign_state"] = "NO_RECLAIM"
                rows.append(base)
                continue

            k = reclaim
            adverse = direction * (p_imp - pb_extreme)
            pb_ratio = adverse / impulse_abs if impulse_abs > 0 else np.nan
            pb_med_vol = float(np.median(pb_vols)) if pb_vols else np.nan
            imp_vol = float(day.vol_imp3.iloc[i]) if np.isfinite(day.vol_imp3.iloc[i]) else np.nan
            reclaim_vol = float(day.volume.iloc[k]) if np.isfinite(day.volume.iloc[k]) else np.nan
            base.update({
                "campaign_state": "RECLAIM",
                "reclaim_ts": day.timestamp.iloc[k],
                "reclaim_entry": float(day.close.iloc[k]),
                "pullback_ratio": float(pb_ratio),
                "pullback_duration_bars": int(k - i),
                "pullback_volume_ratio": pb_med_vol / imp_vol if np.isfinite(pb_med_vol) and np.isfinite(imp_vol) and imp_vol > 0 else np.nan,
                "reclaim_volume_ratio": reclaim_vol / pb_med_vol if np.isfinite(reclaim_vol) and np.isfinite(pb_med_vol) and pb_med_vol > 0 else np.nan,
                "reclaim_resid_z": direction * float(day.resid.iloc[k] / day.sigma.iloc[k]) if np.isfinite(day.resid.iloc[k]) and np.isfinite(day.sigma.iloc[k]) else np.nan,
            })
            base.update(forward_metrics(day, k, direction, "reclaim"))
            rows.append(base)
    return rows


def day_boot_ci(df: pd.DataFrame, col: str, reps: int = 1500) -> list[float]:
    z = df[["trade_date", col]].dropna()
    if z.empty:
        return [np.nan, np.nan, np.nan]
    point = float(z[col].mean())
    dm = z.groupby("trade_date")[col].mean().to_numpy(float)
    if len(dm) < 2:
        return [point, np.nan, np.nan]
    rng = np.random.default_rng(20260930)
    vals = np.empty(reps)
    for i in range(reps):
        vals[i] = np.mean(dm[rng.integers(0, len(dm), len(dm))])
    return [point, float(np.quantile(vals, .025)), float(np.quantile(vals, .975))]


def summarize(df: pd.DataFrame, prefix: str) -> dict:
    out = {"events": int(len(df)), "days": int(df.trade_date.nunique()) if len(df) else 0}
    for m in (5, 10, 15, 30):
        col = f"{prefix}_fwd_{m}m_bps"
        if col not in df:
            continue
        p, lo, hi = day_boot_ci(df, col)
        out[f"mean_{m}m_bps"] = p
        out[f"ci95_{m}m_bps"] = [lo, hi]
        out[f"hit_{m}m"] = float((df[col] > 0).mean()) if df[col].notna().any() else None
    for b in BARRIERS_BPS:
        col = f"{prefix}_fp_{b}bps"
        if col in df:
            r = df[col].isin(["TARGET", "STOP"])
            out[f"target_first_{b}bps"] = float((df.loc[r, col] == "TARGET").mean()) if r.any() else None
    return out


def pb_bin_label(v):
    if not np.isfinite(v):
        return None
    for lo, hi in PB_BINS:
        if lo <= v < hi or (hi == 1.0 and v <= hi):
            return f"{int(lo*100)}-{int(hi*100)}%"
    return "OUTSIDE"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data-root", required=True)
    ap.add_argument("--out", required=True)
    args = ap.parse_args()
    root, out = Path(args.data_root), Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    nifty = load_nifty(root)

    symbols = sorted(p.stem for p in (root / "intraday5").glob("*.parquet"))
    # Pass 1: single locked abnormal-impulse threshold from development data only.
    dev_abs = []
    coverage = {}
    for n, sym in enumerate(symbols, 1):
        x = features_for_symbol(root, sym, nifty)
        if x.empty:
            continue
        a = x.loc[x.trade_date <= DEV_END, "z3"].abs().replace([np.inf, -np.inf], np.nan).dropna().to_numpy(float)
        if len(a):
            dev_abs.append(a)
        coverage[sym] = {"bars": int(len(x)), "days": int(x.trade_date.nunique())}
        if n % 25 == 0:
            print(f"threshold pass {n}/{len(symbols)}", flush=True)
    if not dev_abs:
        raise RuntimeError("No development z3 observations")
    q99 = float(np.quantile(np.concatenate(dev_abs), 0.99))

    # Pass 2: causal event/state extraction.
    all_rows = []
    for n, sym in enumerate(symbols, 1):
        x = features_for_symbol(root, sym, nifty)
        all_rows.extend(campaigns_for_symbol(x, q99))
        if n % 25 == 0:
            print(f"campaign pass {n}/{len(symbols)} rows={len(all_rows)}", flush=True)
    ev = pd.DataFrame(all_rows)
    if ev.empty:
        raise RuntimeError("No campaign events extracted")
    ev["pb_bin"] = ev.pullback_ratio.apply(pb_bin_label) if "pullback_ratio" in ev else None

    # Requested market/event regimes are explicit overlapping flags. They are
    # diagnostics, never signal inputs. The broad post-Oct-2024 bucket is a
    # user-requested stress window, not a claim that every day was bearish.
    ev["covid_period"] = ev.trade_date.between(COVID_START, COVID_END)
    ev["post_oct2024_stress_window"] = ev.trade_date >= POST_OCT24_START
    ev["us_iran_conflict"] = ev.trade_date >= US_IRAN_START

    # Add an independent, data-derived NIFTY regime from the prior completed day.
    nr = nifty_daily_regimes(root)
    ev = ev.merge(nr, on="trade_date", how="left")
    ev.to_parquet(out / "campaign_events.parquet", index=False, compression="zstd")

    report = {
        "research_version": "UNDERLYING-CAMPAIGN-V1",
        "production_changed": False,
        "data": {
            "symbols": len(symbols),
            "impulse_threshold_dev_q99_abs_z3": q99,
            "development_end": DEV_END,
            "validation": [VAL_START, VAL_END],
            "oos": [OOS_START, OOS_END],
            "bar_interval_minutes": 5,
        },
        "named_periods": {
            "covid": [COVID_START, COVID_END],
            "post_oct2024_stress_window": [POST_OCT24_START, OOS_END],
            "us_iran_conflict": [US_IRAN_START, OOS_END],
        },
        "daily_market_periods": daily_market_period_summary(root),
        "splits": {},
    }
    for sp in ("DEV", "VAL", "OOS"):
        z = ev[ev.split == sp]
        rec = z[z.campaign_state == "RECLAIM"].copy()
        report["splits"][sp] = {
            "impulse": summarize(z, "impulse"),
            "reclaim_any": summarize(rec, "reclaim"),
            "reclaim_volume_contraction": summarize(rec[rec.pullback_volume_ratio <= 1.0], "reclaim"),
            "reclaim_contract_expand": summarize(rec[(rec.pullback_volume_ratio <= 1.0) & (rec.reclaim_volume_ratio >= 1.0)], "reclaim"),
            "reclaim_rate": float(len(rec) / len(z)) if len(z) else None,
        }

    # Requested regime distinctions. Named periods can overlap. The independent
    # NIFTY regime is based only on prior completed daily data.
    report["regime_results"] = {}
    named_filters = {
        "COVID_2020_TO_LATE_2021_INTRADAY_OVERLAP": ev.covid_period,
        "POST_OCT2024_STRESS_WINDOW": ev.post_oct2024_stress_window,
        "US_IRAN_CONFLICT": ev.us_iran_conflict,
    }
    for name, mask in named_filters.items():
        z = ev[mask].copy()
        rec = z[z.campaign_state == "RECLAIM"].copy()
        report["regime_results"][name] = {
            "coverage_note": (
                "Dhan intraday history begins within the last-five-year window, so the COVID "
                "intraday sample contains only the available late-2021 overlap; full 2020-late-2021 "
                "context is reported from daily history."
                if name.startswith("COVID") else None
            ),
            "impulse": summarize(z, "impulse"),
            "reclaim": summarize(rec, "reclaim"),
            "reclaim_volume_contraction": summarize(rec[rec.pullback_volume_ratio <= 1.0], "reclaim"),
        }

    report["data_driven_nifty_regimes"] = {}
    rec_reg = ev[ev.campaign_state == "RECLAIM"].copy()
    for name, z in rec_reg.groupby("nifty_regime", dropna=False):
        report["data_driven_nifty_regimes"][str(name)] = summarize(z, "reclaim")

    # Pullback-depth response surface is reported for every split. No OOS bin
    # is selected from OOS itself.
    report["pullback_bins"] = {}
    rec_all = ev[ev.campaign_state == "RECLAIM"].copy()
    for sp in ("VAL", "OOS"):
        zz = rec_all[rec_all.split == sp]
        rows = {}
        for lo, hi in PB_BINS:
            label = f"{int(lo*100)}-{int(hi*100)}%"
            q = zz[(zz.pullback_ratio >= lo) & (zz.pullback_ratio < hi if hi < 1 else zz.pullback_ratio <= hi)]
            rows[label] = summarize(q, "reclaim")
        report["pullback_bins"][sp] = rows

    # Selection is allowed on validation only, then frozen for OOS check.
    val_bins = report["pullback_bins"]["VAL"]
    eligible = [(k, v) for k, v in val_bins.items() if v.get("events", 0) >= 200 and np.isfinite(v.get("mean_15m_bps", np.nan))]
    selected = max(eligible, key=lambda kv: kv[1]["mean_15m_bps"])[0] if eligible else None
    report["validation_selected_pullback_bin"] = selected
    if selected:
        lo, hi = next((a, b) for a, b in PB_BINS if f"{int(a*100)}-{int(b*100)}%" == selected)
        o = rec_all[(rec_all.split == "OOS") & (rec_all.pullback_ratio >= lo) & (rec_all.pullback_ratio < hi if hi < 1 else rec_all.pullback_ratio <= hi)]
        report["oos_validation_selected_bin"] = summarize(o, "reclaim")

    # Long daily-history context diagnostics.
    oos = rec_all[rec_all.split == "OOS"].copy()
    if len(oos):
        oos["vol_regime"] = pd.cut(oos.daily_vol_ratio, [-np.inf, .8, 1.2, np.inf], labels=["LOW", "NORMAL", "HIGH"])
        oos["daily_align"] = np.where(oos.direction * oos.daily_rel5_bps > 0, "ALIGNED", "OPPOSED")
        report["oos_daily_context"] = {
            "vol_regime": {str(k): summarize(v, "reclaim") for k, v in oos.groupby("vol_regime", observed=True)},
            "daily_alignment": {str(k): summarize(v, "reclaim") for k, v in oos.groupby("daily_align")},
        }

    (out / "summary.json").write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")

    lines = [
        "# Underlying campaign research",
        "",
        "**RESEARCH ONLY — production unchanged.**",
        "",
        f"- 5-minute symbols: {len(symbols)}",
        f"- Development-only abnormal impulse q99 |Z3|: {q99:.3f}",
        f"- Final OOS: {OOS_START} to {OOS_END}",
        "",
        "## Core comparison",
        "",
        "| Split | Entry state | Events | +5m bps | +15m bps | 95% CI +15m | +30m bps | 20bps target-first |",
        "|---|---|---:|---:|---:|---:|---:|---:|",
    ]
    for sp in ("VAL", "OOS"):
        for key, label in (("impulse", "Immediate impulse"), ("reclaim_any", "Pullback → reclaim"), ("reclaim_volume_contraction", "Reclaim + pullback vol contraction"), ("reclaim_contract_expand", "Reclaim + contraction + re-expansion")):
            s = report["splits"][sp][key]
            ci = s.get("ci95_15m_bps", [np.nan, np.nan])
            tf = s.get("target_first_20bps")
            lines.append(
                f"| {sp} | {label} | {s.get('events',0)} | {s.get('mean_5m_bps',np.nan):.2f} | "
                f"{s.get('mean_15m_bps',np.nan):.2f} | [{ci[0]:.2f}, {ci[1]:.2f}] | "
                f"{s.get('mean_30m_bps',np.nan):.2f} | {100*tf:.1f}% |" if tf is not None else
                f"| {sp} | {label} | {s.get('events',0)} | {s.get('mean_5m_bps',np.nan):.2f} | "
                f"{s.get('mean_15m_bps',np.nan):.2f} | [{ci[0]:.2f}, {ci[1]:.2f}] | "
                f"{s.get('mean_30m_bps',np.nan):.2f} | n/a |"
            )
    lines += ["", "## Requested market/event regimes", "", "| Regime | Entry state | Events | +15m bps | 95% CI | Hit rate |", "|---|---|---:|---:|---:|---:|"]
    for name, rr in report["regime_results"].items():
        for key, label in (("impulse", "Immediate impulse"), ("reclaim", "Pullback → reclaim")):
            s = rr[key]
            ci = s.get("ci95_15m_bps", [np.nan, np.nan])
            hit = s.get("hit_15m")
            lines.append(
                f"| {name} | {label} | {s.get('events',0)} | {s.get('mean_15m_bps',np.nan):.2f} | "
                f"[{ci[0]:.2f}, {ci[1]:.2f}] | {100*hit:.1f}% |"
                if hit is not None else
                f"| {name} | {label} | {s.get('events',0)} | n/a | n/a | n/a |"
            )

    lines += ["", "## Data-driven prior-day NIFTY regimes", "", "| NIFTY regime | Reclaim events | +15m bps | 95% CI | Hit rate |", "|---|---:|---:|---:|---:|"]
    for name, s in report["data_driven_nifty_regimes"].items():
        ci = s.get("ci95_15m_bps", [np.nan, np.nan])
        hit = s.get("hit_15m")
        lines.append(
            f"| {name} | {s.get('events',0)} | {s.get('mean_15m_bps',np.nan):.2f} | "
            f"[{ci[0]:.2f}, {ci[1]:.2f}] | {100*hit:.1f}% |"
            if hit is not None else f"| {name} | {s.get('events',0)} | n/a | n/a | n/a |"
        )

    lines += ["", "## Pullback depth — OOS", "", "| Pullback depth | Events | +15m bps | 95% CI | Hit rate |", "|---|---:|---:|---:|---:|"]
    for k, s in report["pullback_bins"]["OOS"].items():
        ci = s.get("ci95_15m_bps", [np.nan, np.nan])
        hit = s.get("hit_15m")
        lines.append(f"| {k} | {s.get('events',0)} | {s.get('mean_15m_bps',np.nan):.2f} | [{ci[0]:.2f}, {ci[1]:.2f}] | {100*hit:.1f}% |" if hit is not None else f"| {k} | {s.get('events',0)} | n/a | n/a | n/a |")
    (out / "report.md").write_text("\n".join(lines), encoding="utf-8")
    print("\n".join(lines))


if __name__ == "__main__":
    main()
