#!/usr/bin/env python3
"""V1.1 research-only auditor follow-up.

NO PRODUCTION CONTROL. NO DEPLOYMENT.

Implements the preregistered 1-minute approximation experiments in:
research/V123_V11_EMPIRICAL_NULL_PREREG_2026-09-30.md

Expected normalized input columns:
timestamp, trade_date, symbol, close, nifty_close, sector_close, sector_id

The runner is intentionally independent from production scanner code.
"""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path

import numpy as np
import pandas as pd

HORIZONS = (1, 2, 3, 5, 10, 15)
CALIBRATION_MIN_DAYS = 40
CALIBRATION_MAX_DAYS = 60
EMPIRICAL_Q = 0.99
RIDGE_LAMBDA_FRACTION = 0.05
ROLLING_FACTOR_LOOKBACK = 15
ROLLING_SCALE_LOOKBACK = 15
CUSUM_K = 0.50
CUSUM_H = 6.0
COST_HURDLES_BPS = (5.0, 10.0, 15.0)
FORWARD_HORIZONS = (5, 15, 30)
RNG_SEED = 20260930


def _median_abs_dev_scale(values: np.ndarray) -> float:
    vals = values[np.isfinite(values)]
    if vals.size < 4:
        return np.nan
    med = float(np.median(vals))
    mad = float(np.median(np.abs(vals - med)))
    scale = 1.4826 * mad
    if not np.isfinite(scale) or scale <= 1e-12:
        scale = float(np.std(vals, ddof=1)) if vals.size > 1 else np.nan
    return max(scale, 1e-12) if np.isfinite(scale) else np.nan


def _ridge_betas(y: np.ndarray, m: np.ndarray, s: np.ndarray) -> tuple[float, float]:
    mask = np.isfinite(y) & np.isfinite(m)
    use_sector = np.sum(mask & np.isfinite(s)) >= max(6, int(np.sum(mask)) - 2)
    if np.sum(mask) < 6:
        return np.nan, np.nan
    yv = y[mask]
    mv = m[mask]
    if use_sector:
        sv = s[mask]
        good = np.isfinite(sv)
        yv, mv, sv = yv[good], mv[good], sv[good]
        if yv.size < 6:
            use_sector = False
    if use_sector:
        v_m = float(np.mean(mv * mv))
        v_s = float(np.mean(sv * sv))
        c_ms = float(np.mean(mv * sv))
        c_ym = float(np.mean(yv * mv))
        c_ys = float(np.mean(yv * sv))
        lam = RIDGE_LAMBDA_FRACTION * max(v_m + v_s, 1e-12)
        a, d = v_m + lam, v_s + lam
        det = a * d - c_ms * c_ms
        if abs(det) < 1e-18:
            return c_ym / max(a, 1e-12), 0.0
        return (
            (c_ym * d - c_ys * c_ms) / det,
            (c_ys * a - c_ym * c_ms) / det,
        )
    v_m = float(np.mean(mv * mv))
    c_ym = float(np.mean(yv * mv))
    lam = RIDGE_LAMBDA_FRACTION * max(v_m, 1e-12)
    return c_ym / max(v_m + lam, 1e-12), 0.0


def load_input(path: str) -> pd.DataFrame:
    p = Path(path)
    if p.suffix.lower() in {".parquet", ".pq"}:
        df = pd.read_parquet(p)
    else:
        df = pd.read_csv(p)
    required = {
        "timestamp", "symbol", "close", "nifty_close", "sector_close", "sector_id"
    }
    missing = required - set(df.columns)
    if missing:
        raise ValueError(f"Missing required columns: {sorted(missing)}")
    df = df.copy()
    df["timestamp"] = pd.to_datetime(df["timestamp"])
    if "trade_date" not in df.columns:
        df["trade_date"] = df["timestamp"].dt.date.astype(str)
    else:
        df["trade_date"] = df["trade_date"].astype(str)
    for col in ("close", "nifty_close", "sector_close"):
        df[col] = pd.to_numeric(df[col], errors="coerce")
    df = df.sort_values(["symbol", "timestamp"]).reset_index(drop=True)
    return df


def causal_features(df: pd.DataFrame) -> pd.DataFrame:
    parts = []
    for symbol, g in df.groupby("symbol", sort=False):
        g = g.sort_values("timestamp").copy()
        # Do not allow overnight returns into the intraday detector.
        same_day = g["trade_date"].eq(g["trade_date"].shift(1))
        g["r_stock"] = np.where(
            same_day, np.log(g["close"] / g["close"].shift(1)), np.nan
        )
        g["r_market"] = np.where(
            same_day, np.log(g["nifty_close"] / g["nifty_close"].shift(1)), np.nan
        )
        g["r_sector"] = np.where(
            same_day, np.log(g["sector_close"] / g["sector_close"].shift(1)), np.nan
        )

        y = g["r_stock"].to_numpy(float)
        m = g["r_market"].to_numpy(float)
        s = g["r_sector"].to_numpy(float)
        dates = g["trade_date"].to_numpy()

        beta_m = np.full(len(g), np.nan)
        beta_s = np.full(len(g), np.nan)
        resid = np.full(len(g), np.nan)
        scale = np.full(len(g), np.nan)

        for i in range(len(g)):
            lo = max(0, i - ROLLING_FACTOR_LOOKBACK)
            idx = np.arange(lo, i)
            idx = idx[dates[idx] == dates[i]]
            if idx.size < 6 or not np.isfinite(y[i]) or not np.isfinite(m[i]):
                continue
            bm, bs = _ridge_betas(y[idx], m[idx], s[idx])
            beta_m[i], beta_s[i] = bm, bs
            if np.isfinite(bm):
                sec = s[i] if np.isfinite(s[i]) and np.isfinite(bs) else 0.0
                resid[i] = y[i] - bm * m[i] - (bs if np.isfinite(bs) else 0.0) * sec

            slo = max(0, i - ROLLING_SCALE_LOOKBACK)
            ridx = np.arange(slo, i)
            ridx = ridx[dates[ridx] == dates[i]]
            if ridx.size >= 6:
                scale[i] = _median_abs_dev_scale(resid[ridx])

        g["beta_market"] = beta_m
        g["beta_sector"] = beta_s
        g["residual"] = resid
        g["robust_sigma"] = scale

        for h in HORIZONS:
            rsum = (
                g.groupby("trade_date", sort=False)["residual"]
                .rolling(h, min_periods=h)
                .sum()
                .reset_index(level=0, drop=True)
            )
            g[f"z_{h}m"] = rsum / (g["robust_sigma"] * math.sqrt(h))

        zcols = [f"z_{h}m" for h in HORIZONS]
        zmat = g[zcols].to_numpy(float)
        abszmat = np.abs(zmat)
        all_nan = np.all(~np.isfinite(abszmat), axis=1)
        tmp = np.where(np.isfinite(abszmat), abszmat, -np.inf)
        argmax = np.argmax(tmp, axis=1)
        g["max_abs_z"] = np.where(all_nan, np.nan, tmp[np.arange(len(g)), argmax])
        g["selected_horizon_min"] = np.where(
            all_nan, np.nan, np.array(HORIZONS, dtype=float)[argmax]
        )
        selected_z = np.full(len(g), np.nan)
        good = ~all_nan
        selected_z[good] = zmat[np.arange(len(g))[good], argmax[good]]
        g["selected_z"] = selected_z
        g["direction"] = np.where(selected_z >= 0, 1.0, -1.0)

        # Residual path coherence on the selected horizon.
        coherence = np.full(len(g), np.nan)
        for i in range(len(g)):
            if not np.isfinite(g["selected_horizon_min"].iat[i]):
                continue
            h = int(g["selected_horizon_min"].iat[i])
            lo = i - h + 1
            if lo < 0 or dates[lo] != dates[i]:
                continue
            w = resid[lo:i+1]
            if np.all(np.isfinite(w)):
                denom = float(np.sum(np.abs(w)))
                coherence[i] = abs(float(np.sum(w))) / denom if denom > 1e-15 else 0.0
        g["residual_coherence"] = coherence

        # Sequential stock-return CUSUM, reset daily.
        stock_scale = np.full(len(g), np.nan)
        z_stock = np.full(len(g), np.nan)
        c_up = np.zeros(len(g))
        c_dn = np.zeros(len(g))
        state = np.zeros(len(g))
        up = dn = 0.0
        current_day = None
        for i in range(len(g)):
            if dates[i] != current_day:
                current_day = dates[i]
                up = dn = 0.0
            lo = max(0, i - ROLLING_SCALE_LOOKBACK)
            idx = np.arange(lo, i)
            idx = idx[dates[idx] == dates[i]]
            if idx.size >= 6:
                sc = _median_abs_dev_scale(y[idx])
                stock_scale[i] = sc
                if np.isfinite(sc) and np.isfinite(y[i]):
                    z = y[i] / sc
                    z_stock[i] = z
                    up = max(0.0, up + z - CUSUM_K)
                    dn = max(0.0, dn - z - CUSUM_K)
            c_up[i], c_dn[i] = up, dn
            state[i] = 1 if up >= CUSUM_H and up > dn else (-1 if dn >= CUSUM_H and dn > up else 0)
        g["stock_z_1m"] = z_stock
        g["cusum_up"] = c_up
        g["cusum_down"] = c_dn
        g["cusum_state"] = state

        for h in FORWARD_HORIZONS:
            future = g.groupby("trade_date", sort=False)["close"].shift(-h)
            g[f"fwd_{h}m"] = future / g["close"] - 1.0
            g[f"signed_fwd_{h}m"] = g["direction"] * g[f"fwd_{h}m"]
        parts.append(g)
    return pd.concat(parts, ignore_index=True)


def prior_days(days: list[str], current: str) -> list[str]:
    i = days.index(current)
    prior = days[:i]
    prior = prior[-CALIBRATION_MAX_DAYS:]
    return prior if len(prior) >= CALIBRATION_MIN_DAYS else []


def apply_empirical_thresholds(df: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    days = sorted(df["trade_date"].dropna().unique().tolist())
    rows = []
    thresholds = []
    for day in days:
        train_days = prior_days(days, day)
        if not train_days:
            continue
        train = df[df["trade_date"].isin(train_days)]
        vals = train["max_abs_z"].to_numpy(float)
        vals = vals[np.isfinite(vals)]
        if vals.size == 0:
            continue
        q = float(np.quantile(vals, EMPIRICAL_Q))
        thresholds.append({
            "trade_date": day,
            "train_start": train_days[0],
            "train_end": train_days[-1],
            "train_days": len(train_days),
            "q99_max_abs_z": q,
        })
        d = df[df["trade_date"].eq(day)].copy()
        d["empirical_threshold"] = q
        d["empirical_detect"] = d["max_abs_z"] >= q
        d["fixed_3188_detect"] = d["max_abs_z"] >= 3.188815
        d["empirical_cusum_detect"] = (
            d["empirical_detect"] & d["cusum_state"].eq(d["direction"])
        )
        rows.append(d)
    if not rows:
        return pd.DataFrame(), pd.DataFrame(thresholds)
    return pd.concat(rows, ignore_index=True), pd.DataFrame(thresholds)


def bootstrap_day_ci(events: pd.DataFrame, col: str, reps: int = 1000) -> tuple[float, float, float]:
    x = events[["trade_date", col]].dropna()
    if x.empty:
        return np.nan, np.nan, np.nan
    day_means = x.groupby("trade_date")[col].mean()
    days = day_means.index.to_numpy()
    vals = day_means.to_numpy(float)
    point = float(x[col].mean())
    if len(vals) < 2:
        return point, np.nan, np.nan
    rng = np.random.default_rng(RNG_SEED)
    boots = np.empty(reps)
    for i in range(reps):
        pick = rng.integers(0, len(vals), len(vals))
        boots[i] = float(np.mean(vals[pick]))
    return point, float(np.quantile(boots, 0.025)), float(np.quantile(boots, 0.975))


def matched_placebo(events: pd.DataFrame, full: pd.DataFrame, horizon: int) -> pd.DataFrame:
    rng = np.random.default_rng(RNG_SEED + horizon)
    pools = {}
    for key, g in full.groupby(["trade_date", "symbol"], sort=False):
        valid = g[np.isfinite(g[f"fwd_{horizon}m"])]
        if not valid.empty:
            pools[key] = valid
    out = []
    for _, e in events.iterrows():
        key = (e["trade_date"], e["symbol"])
        pool = pools.get(key)
        if pool is None or pool.empty:
            continue
        row = pool.iloc[int(rng.integers(0, len(pool)))]
        out.append({
            "trade_date": e["trade_date"],
            "symbol": e["symbol"],
            f"signed_fwd_{horizon}m": float(e["direction"]) * float(row[f"fwd_{horizon}m"]),
        })
    return pd.DataFrame(out)


def auc_score(y: np.ndarray, p: np.ndarray) -> float:
    mask = np.isfinite(y) & np.isfinite(p)
    y, p = y[mask].astype(int), p[mask]
    pos, neg = int(np.sum(y == 1)), int(np.sum(y == 0))
    if pos == 0 or neg == 0:
        return np.nan
    order = np.argsort(p, kind="mergesort")
    ranks = np.empty(len(p), dtype=float)
    ranks[order] = np.arange(1, len(p) + 1)
    # average tied ranks
    _, inv, counts = np.unique(p, return_inverse=True, return_counts=True)
    for grp, cnt in enumerate(counts):
        if cnt > 1:
            idx = np.where(inv == grp)[0]
            ranks[idx] = float(np.mean(ranks[idx]))
    rank_sum = float(np.sum(ranks[y == 1]))
    return (rank_sum - pos * (pos + 1) / 2.0) / (pos * neg)


def fit_logistic_ridge(X: np.ndarray, y: np.ndarray, lam: float = 1.0, steps: int = 100) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    mask = np.all(np.isfinite(X), axis=1) & np.isfinite(y)
    X, y = X[mask], y[mask].astype(float)
    if len(y) < 100 or len(np.unique(y)) < 2:
        raise ValueError("insufficient logistic training data")
    mu = X.mean(axis=0)
    sd = X.std(axis=0)
    sd[sd < 1e-9] = 1.0
    Z = (X - mu) / sd
    Z = np.column_stack([np.ones(len(Z)), Z])
    beta = np.zeros(Z.shape[1])
    penalty = np.eye(Z.shape[1]) * lam
    penalty[0, 0] = 0.0
    for _ in range(steps):
        eta = np.clip(Z @ beta, -30, 30)
        p = 1.0 / (1.0 + np.exp(-eta))
        w = np.clip(p * (1 - p), 1e-6, None)
        grad = Z.T @ (p - y) + penalty @ beta
        hess = (Z.T * w) @ Z + penalty
        step = np.linalg.solve(hess, grad)
        beta -= step
        if float(np.max(np.abs(step))) < 1e-7:
            break
    return beta, mu, sd


def predict_logistic(X: np.ndarray, beta: np.ndarray, mu: np.ndarray, sd: np.ndarray) -> np.ndarray:
    Z = (X - mu) / sd
    Z = np.column_stack([np.ones(len(Z)), Z])
    eta = np.clip(Z @ beta, -30, 30)
    return 1.0 / (1.0 + np.exp(-eta))


def conditional_model_oos(detected: pd.DataFrame, all_days: list[str], horizon: int) -> pd.DataFrame:
    feature_cols = [f"z_{h}m" for h in HORIZONS] + [
        "selected_horizon_min", "selected_z", "max_abs_z",
        "cusum_up", "cusum_down", "robust_sigma", "residual_coherence"
    ]
    scored = []
    for day in sorted(detected["trade_date"].unique()):
        train_days = prior_days(all_days, day)
        if not train_days:
            continue
        train = detected[detected["trade_date"].isin(train_days)].copy()
        test = detected[detected["trade_date"].eq(day)].copy()
        target = f"signed_fwd_{horizon}m"
        train["y"] = (train[target] > 0).astype(float)
        try:
            beta, mu, sd = fit_logistic_ridge(
                train[feature_cols].to_numpy(float),
                train["y"].to_numpy(float),
                lam=1.0,
            )
        except ValueError:
            continue
        X = test[feature_cols].to_numpy(float)
        good = np.all(np.isfinite(X), axis=1)
        test["pred_p"] = np.nan
        if np.any(good):
            test.loc[good, "pred_p"] = predict_logistic(X[good], beta, mu, sd)
        scored.append(test)
    return pd.concat(scored, ignore_index=True) if scored else pd.DataFrame()


def summarize_variant(name: str, events: pd.DataFrame, full: pd.DataFrame) -> dict:
    out = {"variant": name, "events": int(len(events))}
    if events.empty:
        return out
    out["days"] = int(events["trade_date"].nunique())
    out["events_per_stock_day"] = float(len(events) / max(1, events[["trade_date","symbol"]].drop_duplicates().shape[0]))
    for h in FORWARD_HORIZONS:
        col = f"signed_fwd_{h}m"
        point, lo, hi = bootstrap_day_ci(events, col)
        out[f"mean_{h}m_bps"] = point * 10000 if np.isfinite(point) else None
        out[f"ci95_{h}m_bps"] = [
            lo * 10000 if np.isfinite(lo) else None,
            hi * 10000 if np.isfinite(hi) else None,
        ]
        vals = events[col].dropna()
        out[f"hit_{h}m"] = float((vals > 0).mean()) if len(vals) else None
        placebo = matched_placebo(events, full, h)
        pv = placebo[col].dropna() if not placebo.empty else pd.Series(dtype=float)
        out[f"placebo_mean_{h}m_bps"] = float(pv.mean() * 10000) if len(pv) else None
        for cost in COST_HURDLES_BPS:
            out[f"p_gt_{int(cost)}bps_{h}m"] = float((vals * 10000 > cost).mean()) if len(vals) else None
            out[f"mean_after_{int(cost)}bps_{h}m"] = float(vals.mean() * 10000 - cost) if len(vals) else None
    return out


def run(path: str, output_dir: str) -> dict:
    raw = load_input(path)
    feat = causal_features(raw)
    oos, thresholds = apply_empirical_thresholds(feat)
    if oos.empty:
        raise RuntimeError("No OOS sessions available after calibration window")

    variants = {
        "fixed_3.1888_multiscale": oos[oos["fixed_3188_detect"]].copy(),
        "empirical_q99_multiscale": oos[oos["empirical_detect"]].copy(),
        "empirical_q99_plus_cusum": oos[oos["empirical_cusum_detect"]].copy(),
    }

    # Fixed-horizon empirical thresholds, each calibrated from prior sessions.
    days = sorted(feat["trade_date"].unique().tolist())
    for h in HORIZONS:
        selected = []
        for day in days:
            train_days = prior_days(days, day)
            if not train_days:
                continue
            tr = feat[feat["trade_date"].isin(train_days)][f"z_{h}m"].abs().dropna()
            if tr.empty:
                continue
            q = float(tr.quantile(EMPIRICAL_Q))
            d = feat[feat["trade_date"].eq(day)].copy()
            d["direction"] = np.where(d[f"z_{h}m"] >= 0, 1.0, -1.0)
            hit = d[f"z_{h}m"].abs() >= q
            selected.append(d[hit])
        variants[f"fixed_{h}m_empirical_q99"] = (
            pd.concat(selected, ignore_index=True) if selected else pd.DataFrame()
        )

    summary = {
        "research_version": "V1.1-EMPIRICAL-NULL-PREREG-2026-09-30",
        "input_rows": int(len(raw)),
        "symbols": int(raw["symbol"].nunique()),
        "sessions": int(raw["trade_date"].nunique()),
        "oos_sessions": int(oos["trade_date"].nunique()),
        "thresholds": {
            "mean_q99": float(thresholds["q99_max_abs_z"].mean()),
            "median_q99": float(thresholds["q99_max_abs_z"].median()),
            "min_q99": float(thresholds["q99_max_abs_z"].min()),
            "max_q99": float(thresholds["q99_max_abs_z"].max()),
        },
        "variants": [],
        "conditional_models": {},
    }

    for name, ev in variants.items():
        if not ev.empty and "direction" not in ev.columns:
            ev["direction"] = np.where(ev["selected_z"] >= 0, 1.0, -1.0)
        summary["variants"].append(summarize_variant(name, ev, feat))

    detected = variants["empirical_q99_multiscale"].copy()
    for h in FORWARD_HORIZONS:
        scored = conditional_model_oos(detected, days, h)
        if scored.empty:
            summary["conditional_models"][str(h)] = {"status": "insufficient_data"}
            continue
        y = (scored[f"signed_fwd_{h}m"] > 0).astype(float).to_numpy()
        p = scored["pred_p"].to_numpy(float)
        mask = np.isfinite(y) & np.isfinite(p)
        summary["conditional_models"][str(h)] = {
            "rows": int(np.sum(mask)),
            "auc": float(auc_score(y[mask], p[mask])) if np.sum(mask) else None,
            "brier": float(np.mean((p[mask] - y[mask]) ** 2)) if np.sum(mask) else None,
        }

    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)
    thresholds.to_csv(out / "empirical_thresholds_by_day.csv", index=False)
    detected.to_csv(out / "empirical_multiscale_events.csv", index=False)
    with open(out / "summary.json", "w", encoding="utf-8") as fh:
        json.dump(summary, fh, indent=2)
    return summary


def self_test() -> None:
    rng = np.random.default_rng(7)
    days = pd.bdate_range("2026-01-01", periods=45)
    rows = []
    symbols = ["AAA", "BBB", "CCC"]
    for day in days:
        times = pd.date_range(day + pd.Timedelta(hours=9, minutes=15), periods=80, freq="min")
        nifty = 100.0
        sectors = {"S1": 100.0, "S2": 100.0}
        prices = {s: 100.0 for s in symbols}
        for i, ts in enumerate(times):
            mn = rng.normal(0, 0.0004)
            nifty *= math.exp(mn)
            for sec in sectors:
                sectors[sec] *= math.exp(0.7 * mn + rng.normal(0, 0.00025))
            for j, symbol in enumerate(symbols):
                sec = "S1" if j < 2 else "S2"
                drift = 0.0008 if (symbol == "AAA" and i > 45 and day == days[-1]) else 0.0
                r = 0.5 * mn + 0.3 * math.log(sectors[sec] / (sectors[sec] / math.exp(0))) + drift + rng.normal(0, 0.0005)
                prices[symbol] *= math.exp(r)
                rows.append({
                    "timestamp": ts,
                    "trade_date": str(day.date()),
                    "symbol": symbol,
                    "close": prices[symbol],
                    "nifty_close": nifty,
                    "sector_close": sectors[sec],
                    "sector_id": sec,
                })
    df = pd.DataFrame(rows)
    tmp = Path("/tmp/v11_self_test.csv")
    df.to_csv(tmp, index=False)
    result = run(str(tmp), "/tmp/v11_self_test_out")
    assert result["sessions"] == 45
    assert result["oos_sessions"] == 5
    assert result["thresholds"]["mean_q99"] > 0
    print(json.dumps({"self_test": "PASS", "thresholds": result["thresholds"]}, indent=2))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--input", help="normalized CSV or Parquet")
    ap.add_argument("--output-dir", default="research/v11_results")
    ap.add_argument("--self-test", action="store_true")
    args = ap.parse_args()
    if args.self_test:
        self_test()
        return
    if not args.input:
        ap.error("--input is required unless --self-test is used")
    print(json.dumps(run(args.input, args.output_dir), indent=2))


if __name__ == "__main__":
    main()
