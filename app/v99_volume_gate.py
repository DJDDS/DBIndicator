"""V9.9 / Trial 20 preregistered abnormal-FUTSTK-volume validation.

The V9.8 horse race closed the OI-magnitude hypothesis as association without
incremental information once abnormal futures participation was controlled.
V9.9 therefore tests one new carrier only: abnormal total FUTSTK notional
turnover -> next-session realised variance.

This module is research/shadow only.  It cannot activate TRADE/WATCH, cannot
unlock Trial 18, and contains no optimized volume threshold.
"""
from __future__ import annotations

import math
from typing import Mapping

import numpy as np
import pandas as pd

from . import v96_trial17 as v96
from . import v98_incremental_oi as v98

BUILD_ID = "2026-09-03-INSTITUTIONAL-V9.9.2-TRIAL20-LOG-RV-INTEGRITY-CLOSURE"
TRIAL_NUMBER = 20
INDEPENDENT_START = pd.Timestamp("2015-09-01")
INDEPENDENT_END = pd.Timestamp("2018-08-31")
WARMUP_START = pd.Timestamp("2014-06-01")
CLARK_WEST_HURDLE = 1.645
MIN_TRAIN_OBS = 252
REFIT_EVERY = 20
MIN_OOS_DATES = 120
VAR_FLOOR = 1e-12
VAR_SCALE = 1_000_000.0


def trial20_spec() -> dict:
    return {
        "trial_number": TRIAL_NUMBER,
        "name": "Abnormal FUTSTK Volume Validation",
        "feature": "abnormal total FUTSTK notional turnover",
        "feature_construction": "log(turnover); point-in-time detrend on prior 20/60-day means + weekday + trend; standardize by prior 60-day residual SD",
        "volume_threshold": None,
        "window": [str(INDEPENDENT_START.date()), str(INDEPENDENT_END.date())],
        "primary_target": "next_yz_var",
        "robustness_target": "next_gk_var",
        "benchmark": "HAR daily + weekly + monthly",
        "challenger": "HAR daily + weekly + monthly + abnormal FUTSTK volume",
        "oos_losses": ["MSE", "QLIKE"],
        "nested_test": "Clark-West MSPE-adjusted, one-sided",
        "forecast_space": "log_realized_variance",
        "back_transform": "training_only_lognormal_smearing",
        "closure_rerun": True,
        "promotion_on_corrected_pass": False,
        "clark_west_hurdle": CLARK_WEST_HURDLE,
        "same_day_same_dte_control": True,
        "diagnostic_variance_scale": VAR_SCALE,
        "two_way_cluster": ["date", "symbol"],
        "chronological_blocks": 4,
        "top_day_sensitivity": 3,
        "oi_role": "DIAGNOSTIC_ONLY",
        "trial19_state": "CLOSED_ASSOCIATION_NOT_INCREMENTAL",
        "trial18_locked": True,
        "production_activation": False,
        "active_playbooks_unchanged": True,
    }


def _as_daily_index(index) -> pd.DatetimeIndex:
    idx = pd.DatetimeIndex(index)
    if idx.tz is not None:
        idx = idx.tz_convert("Asia/Kolkata").tz_localize(None)
    return idx.normalize()


def _ols_fit(y: np.ndarray, X: np.ndarray) -> np.ndarray:
    A = np.column_stack([np.ones(len(X)), X])
    return np.linalg.pinv(A) @ y


def _volume_design(log_turnover: pd.Series) -> pd.DataFrame:
    idx = _as_daily_index(log_turnover.index)
    s = pd.Series(pd.to_numeric(log_turnover, errors="coerce").to_numpy(), index=idx, dtype=float)
    out = pd.DataFrame(index=idx)
    out["ma20_prev"] = s.rolling(20, min_periods=15).mean().shift(1)
    out["ma60_prev"] = s.rolling(60, min_periods=40).mean().shift(1)
    # Deterministic time trend is known at t; scaling is numeric conditioning only.
    out["trend"] = np.arange(len(out), dtype=float) / 252.0
    dow = out.index.dayofweek
    for k in range(1, 5):
        out[f"dow_{k}"] = (dow == k).astype(float)
    return out


def build_abnormal_turnover(frame: pd.DataFrame, *, min_fit_obs: int = 60, refit_every: int = 20) -> pd.DataFrame:
    """Add the frozen V9.9 point-in-time abnormal-turnover feature.

    The current session's turnover is the signal observed after the close.  All
    expectation-model coefficients, moving averages and standardization scales
    use prior rows only; future data never enter a historical score.
    """
    out = frame.copy()
    out.index = _as_daily_index(out.index)
    field = "futures_turnover_notional"
    if field not in out:
        out["futstk_log_turnover"] = np.nan
        out["futstk_turnover_resid"] = np.nan
        out["futstk_turnover_resid_sd60_prev"] = np.nan
        out["abnormal_futstk_volume"] = np.nan
        return out
    turn = pd.to_numeric(out[field], errors="coerce").replace([np.inf, -np.inf], np.nan)
    turn = turn.where(turn >= 0)
    logt = np.log1p(turn)
    design = _volume_design(logt)
    columns = list(design.columns)
    residual = pd.Series(np.nan, index=out.index, dtype=float)
    beta = None
    fitted_at = -10**9

    for i, d in enumerate(out.index):
        if not np.isfinite(logt.iloc[i]) or design.iloc[i].isna().any():
            continue
        prior = np.arange(i)
        if len(prior):
            valid = logt.iloc[prior].notna().to_numpy() & design.iloc[prior].notna().all(axis=1).to_numpy()
            prior = prior[valid]
        if len(prior) < int(min_fit_obs):
            continue
        if beta is None or i - fitted_at >= int(refit_every):
            y = logt.iloc[prior].to_numpy(dtype=float)
            X = design.iloc[prior][columns].to_numpy(dtype=float)
            beta = _ols_fit(y, X)
            fitted_at = i
        x = design.iloc[i][columns].to_numpy(dtype=float)
        expected = float(beta[0] + x @ beta[1:])
        residual.iloc[i] = float(logt.iloc[i] - expected)

    sd_prev = residual.rolling(60, min_periods=40).std(ddof=1).shift(1)
    abnormal = residual / sd_prev.where(sd_prev > 1e-12)
    out["futstk_log_turnover"] = logt
    out["futstk_turnover_ma20_prev"] = design["ma20_prev"]
    out["futstk_turnover_ma60_prev"] = design["ma60_prev"]
    out["futstk_turnover_resid"] = residual
    out["futstk_turnover_resid_sd60_prev"] = sd_prev
    out["abnormal_futstk_volume"] = abnormal
    return out


def _prepare_symbol(symbol: str, frame: pd.DataFrame) -> pd.DataFrame:
    if frame is None or len(frame) == 0:
        return pd.DataFrame()
    x = frame.copy()
    x.index = _as_daily_index(x.index)
    x["date"] = x.index
    x["symbol"] = str(symbol).upper()
    member = x.get("fno_member_pti", True)
    if not isinstance(member, pd.Series):
        member = pd.Series(bool(member), index=x.index)
    x = x.loc[member.fillna(False).astype(bool)].copy()
    if "dte_bucket" not in x:
        dte = pd.to_numeric(x.get("nse_near_dte", x.get("days_to_expiry")), errors="coerce")
        x["dte_bucket"] = pd.cut(dte, bins=[-0.001, 5, 10, 20, np.inf], labels=["0-5", "6-10", "11-20", "21+"], include_lowest=True)
    return x


def _stack(symbol_frames: Mapping[str, pd.DataFrame]) -> pd.DataFrame:
    rows = [_prepare_symbol(sym, f) for sym, f in dict(symbol_frames or {}).items()]
    rows = [r for r in rows if r is not None and not r.empty]
    return pd.concat(rows, ignore_index=True) if rows else pd.DataFrame()


def _log_variance_design(X: np.ndarray, *, log_columns: int | None = None) -> np.ndarray:
    arr = np.asarray(X, dtype=float).copy()
    if arr.ndim == 1:
        arr = arr.reshape(1, -1)
    nlog = arr.shape[1] if log_columns is None else int(log_columns)
    if nlog < 0 or nlog > arr.shape[1]:
        raise ValueError("log_columns must be between 0 and the number of regressors")
    if nlog:
        arr[:, :nlog] = np.log(np.maximum(arr[:, :nlog], VAR_FLOOR))
    return arr


def _fit_log_variance_model(y: np.ndarray, X: np.ndarray, *, log_columns: int | None = None) -> tuple[np.ndarray, float]:
    yy = np.asarray(y, dtype=float)
    design = _log_variance_design(X, log_columns=log_columns)
    log_y = np.log(np.maximum(yy, VAR_FLOOR))
    beta = _ols_fit(log_y, design)
    A = np.column_stack([np.ones(len(design)), design])
    resid = log_y - A @ beta
    smear = float(np.mean(np.exp(resid)))
    if not np.isfinite(smear) or smear <= 0:
        smear = 1.0
    return beta, smear


def _predict_log_variance(model: tuple[np.ndarray, float], row: np.ndarray, *, log_columns: int | None = None) -> float | None:
    beta, smear = model
    raw = np.asarray(row, dtype=float).reshape(1, -1)
    if not np.isfinite(raw).all():
        return None
    design = _log_variance_design(raw, log_columns=log_columns)[0]
    log_pred = float(beta[0] + design @ beta[1:])
    pred = float(np.exp(log_pred) * smear)
    if not np.isfinite(pred):
        return None
    return max(VAR_FLOOR, pred)


def _oos_prediction_rows(frame: pd.DataFrame, target: str, *, min_train_obs: int, refit_every: int) -> pd.DataFrame:
    base_cols = ["har_daily_var", "har_weekly_var", "har_monthly_var"]
    aug_cols = base_cols + ["abnormal_futstk_volume"]
    out = []
    for symbol, grp in frame.groupby("symbol", sort=False):
        g = grp.sort_values("date").reset_index(drop=True).copy()
        dates = pd.to_datetime(g["date"]).dt.normalize().to_numpy()
        y = pd.to_numeric(g[target], errors="coerce").to_numpy(dtype=float)
        xb_all = g[base_cols].apply(pd.to_numeric, errors="coerce").to_numpy(dtype=float)
        xa_all = g[aug_cols].apply(pd.to_numeric, errors="coerce").to_numpy(dtype=float)
        base_valid = np.isfinite(y) & np.isfinite(xb_all).all(axis=1)
        aug_valid = np.isfinite(y) & np.isfinite(xa_all).all(axis=1)
        fit_base = fit_aug = None
        last_refit_oos = -10**9
        oos_count = 0
        for i, d64 in enumerate(dates):
            d = pd.Timestamp(d64).normalize()
            if d < INDEPENDENT_START or d > INDEPENDENT_END:
                continue
            if not (base_valid[i] and aug_valid[i]):
                continue
            vb = base_valid[:i]
            va = aug_valid[:i]
            if int(vb.sum()) < int(min_train_obs) or int(va.sum()) < int(min_train_obs):
                continue
            if fit_base is None or fit_aug is None or oos_count - last_refit_oos >= int(refit_every):
                fit_base = _fit_log_variance_model(y[:i][vb], xb_all[:i][vb], log_columns=3)
                fit_aug = _fit_log_variance_model(y[:i][va], xa_all[:i][va], log_columns=3)
                last_refit_oos = oos_count
            pred_b = _predict_log_variance(fit_base, xb_all[i], log_columns=3)
            pred_a = _predict_log_variance(fit_aug, xa_all[i], log_columns=3)
            if pred_b is None or pred_a is None:
                continue
            out.append({
                "date": d, "symbol": str(symbol), "target": float(y[i]),
                "har_forecast": pred_b, "augmented_forecast": pred_a,
                "abnormal_futstk_volume": float(xa_all[i, -1]),
                "dte_bucket": g.iloc[i].get("dte_bucket"),
            })
            oos_count += 1
    return pd.DataFrame(out)


def _qlike(y: pd.Series, forecast: pd.Series) -> pd.Series:
    yy = pd.to_numeric(y, errors="coerce").clip(lower=VAR_FLOOR)
    ff = pd.to_numeric(forecast, errors="coerce").clip(lower=VAR_FLOOR)
    ratio = yy / ff
    return ratio - np.log(ratio) - 1.0




def _forecast_integrity(pred: pd.DataFrame) -> dict:
    if pred is None or pred.empty:
        return {
            "n": 0, "har_min": None, "har_max": None, "augmented_min": None, "augmented_max": None,
            "har_floor_hits": 0, "augmented_floor_hits": 0,
        }
    fb = pd.to_numeric(pred.get("har_forecast"), errors="coerce").dropna()
    fa = pd.to_numeric(pred.get("augmented_forecast"), errors="coerce").dropna()
    tol = VAR_FLOOR * (1.0 + 1e-9)
    return {
        "n": int(len(pred)),
        "har_min": float(fb.min()) if len(fb) else None,
        "har_max": float(fb.max()) if len(fb) else None,
        "augmented_min": float(fa.min()) if len(fa) else None,
        "augmented_max": float(fa.max()) if len(fa) else None,
        "har_floor_hits": int((fb <= tol).sum()) if len(fb) else 0,
        "augmented_floor_hits": int((fa <= tol).sum()) if len(fa) else 0,
    }


def _closure_interpretation(statistical_pass: bool) -> dict:
    if bool(statistical_pass):
        return {
            "status": "SPECIFICATION_SENSITIVE_NOT_PROMOTED",
            "promotion_allowed": False,
            "reason": "Corrected log-RV specification passed only after the Trial-20 outcome window had already been observed.",
        }
    return {
        "status": "CLOSED_REJECTED_LOG_RV_CONFIRMED",
        "promotion_allowed": False,
        "reason": "The frozen abnormal-volume hypothesis still failed after the one-time log-RV integrity repair.",
    }


def _newey_west_t(values: pd.Series, lag: int = 5) -> float | None:
    x = pd.to_numeric(values, errors="coerce").dropna().to_numpy(dtype=float)
    n = len(x)
    if n < 10:
        return None
    mu = float(np.mean(x))
    z = x - mu
    gamma0 = float(np.dot(z, z) / n)
    lrv = gamma0
    max_lag = min(int(lag), n - 1)
    for k in range(1, max_lag + 1):
        gamma = float(np.dot(z[k:], z[:-k]) / n)
        lrv += 2.0 * (1.0 - k / (max_lag + 1.0)) * gamma
    se = math.sqrt(max(lrv, 0.0) / n)
    return float(mu / se) if se > 1e-18 else None


def _forecast_metrics(pred: pd.DataFrame) -> dict:
    if pred is None or pred.empty:
        return {"n": 0, "dates": 0, "mse": {"har": None, "augmented": None}, "qlike": {"har": None, "augmented": None}, "clark_west": {"t": None, "mean_adjusted_loss_diff": None}, "oos_r2": None}
    x = pred.copy()
    y = pd.to_numeric(x["target"], errors="coerce")
    fb = pd.to_numeric(x["har_forecast"], errors="coerce")
    fa = pd.to_numeric(x["augmented_forecast"], errors="coerce")
    valid = y.notna() & fb.notna() & fa.notna()
    x = x.loc[valid].copy(); y = y.loc[valid]; fb = fb.loc[valid]; fa = fa.loc[valid]
    x["mse_har"] = (y - fb) ** 2
    x["mse_aug"] = (y - fa) ** 2
    x["qlike_har"] = _qlike(y, fb)
    x["qlike_aug"] = _qlike(y, fa)
    x["cw"] = (y - fb) ** 2 - (y - fa) ** 2 + (fb - fa) ** 2
    daily = x.groupby("date", observed=True)[["mse_har", "mse_aug", "qlike_har", "qlike_aug", "cw"]].mean()
    mse_h = float(daily["mse_har"].mean()) if len(daily) else None
    mse_a = float(daily["mse_aug"].mean()) if len(daily) else None
    q_h = float(daily["qlike_har"].mean()) if len(daily) else None
    q_a = float(daily["qlike_aug"].mean()) if len(daily) else None
    cw_t = _newey_west_t(daily["cw"], lag=5)
    r2 = None if mse_h is None or mse_h <= 1e-18 or mse_a is None else float(1.0 - mse_a / mse_h)
    return {
        "n": int(len(x)), "dates": int(len(daily)),
        "mse": {"har": mse_h, "augmented": mse_a, "improves": bool(mse_a is not None and mse_h is not None and mse_a < mse_h)},
        "qlike": {"har": q_h, "augmented": q_a, "improves": bool(q_a is not None and q_h is not None and q_a < q_h)},
        "clark_west": {"t": cw_t, "mean_adjusted_loss_diff": float(daily["cw"].mean()) if len(daily) else None, "hurdle": CLARK_WEST_HURDLE, "pass": bool(cw_t is not None and cw_t > CLARK_WEST_HURDLE)},
        "oos_r2": r2,
    }


def _chronological_stability(pred: pd.DataFrame) -> dict:
    if pred is None or pred.empty:
        return {"positive_blocks": 0, "total_blocks": 0, "required": 3, "blocks": []}
    dates = np.array(sorted(pd.to_datetime(pred["date"]).dt.normalize().unique()))
    blocks = []
    positive = 0
    for i, ds in enumerate(np.array_split(dates, 4), start=1):
        sub = pred[pd.to_datetime(pred["date"]).dt.normalize().isin(ds)]
        m = _forecast_metrics(sub)
        ok = bool((m.get("mse") or {}).get("improves") and (m.get("qlike") or {}).get("improves"))
        positive += int(ok)
        blocks.append({"block": i, "start": str(pd.Timestamp(ds[0]).date()) if len(ds) else None, "end": str(pd.Timestamp(ds[-1]).date()) if len(ds) else None, "mse_improves": bool((m.get("mse") or {}).get("improves")), "qlike_improves": bool((m.get("qlike") or {}).get("improves")), "oos_r2": m.get("oos_r2")})
    return {"positive_blocks": int(positive), "total_blocks": int(len(blocks)), "required": 3, "pass": bool(positive >= 3), "blocks": blocks}


def _top_day_sensitivity(pred: pd.DataFrame) -> dict:
    if pred is None or pred.empty:
        return {"removed_dates": [], "pass": False, "metrics": _forecast_metrics(pd.DataFrame())}
    x = pred.copy()
    y = pd.to_numeric(x["target"], errors="coerce"); fb = pd.to_numeric(x["har_forecast"], errors="coerce"); fa = pd.to_numeric(x["augmented_forecast"], errors="coerce")
    x["cw"] = (y - fb) ** 2 - (y - fa) ** 2 + (fb - fa) ** 2
    daily = x.groupby("date", observed=True)["cw"].mean().sort_values(ascending=False)
    removed = [pd.Timestamp(d).normalize() for d in daily.head(3).index]
    sub = x[~pd.to_datetime(x["date"]).dt.normalize().isin(removed)].drop(columns=["cw"])
    m = _forecast_metrics(sub)
    ok = bool((m.get("mse") or {}).get("improves") and (m.get("qlike") or {}).get("improves"))
    return {"removed_dates": [str(d.date()) for d in removed], "pass": ok, "metrics": m}


def _two_way_cluster_robust_ols_fast(y, x: pd.DataFrame, date_clusters, symbol_clusters) -> dict:
    """Cameron-Gelbach-Miller two-way clustered OLS without O(N^2) masks.

    This is algebraically equivalent to the frozen V9.6 implementation, but
    cluster score vectors are aggregated once with factorized labels.  Trial 20
    has nearly one unique date-symbol intersection per row, so the old
    per-cluster boolean-mask loop becomes quadratic on the full OOS panel.
    """
    y = pd.Series(y).reset_index(drop=True)
    X = pd.DataFrame(x).reset_index(drop=True)
    dc = pd.Series(date_clusters).reset_index(drop=True)
    sc = pd.Series(symbol_clusters).reset_index(drop=True)
    valid = y.notna() & dc.notna() & sc.notna() & X.notna().all(axis=1)
    yv = y.loc[valid].astype(float).to_numpy()
    Xv = X.loc[valid].astype(float)
    dv = dc.loc[valid].astype(str).to_numpy()
    sv = sc.loc[valid].astype(str).to_numpy()
    names = list(Xv.columns)
    if len(yv) <= len(names) + 2:
        return {
            "n": int(len(yv)),
            "date_clusters": int(pd.Series(dv).nunique()),
            "symbol_clusters": int(pd.Series(sv).nunique()),
            "coef": {}, "se": {}, "t": {},
        }

    A = np.column_stack([np.ones(len(Xv)), Xv.to_numpy()])
    N, k = A.shape
    bread = np.linalg.pinv(A.T @ A)
    beta = bread @ A.T @ yv
    resid = yv - A @ beta
    scores = A * resid[:, None]

    def _cluster_cov_from_codes(codes: np.ndarray, groups: int) -> np.ndarray:
        meat_scores = np.zeros((int(groups), k), dtype=float)
        np.add.at(meat_scores, codes, scores)
        meat = meat_scores.T @ meat_scores
        G = int(groups)
        corr = (G / (G - 1)) * ((N - 1) / (N - k)) if G > 1 and N > k else 1.0
        return bread @ meat @ bread * corr

    date_codes, date_uniques = pd.factorize(dv, sort=False)
    symbol_codes, symbol_uniques = pd.factorize(sv, sort=False)
    pair_index = pd.MultiIndex.from_arrays([dv, sv])
    pair_codes, pair_uniques = pd.factorize(pair_index, sort=False)

    cov = (
        _cluster_cov_from_codes(date_codes, len(date_uniques))
        + _cluster_cov_from_codes(symbol_codes, len(symbol_uniques))
        - _cluster_cov_from_codes(pair_codes, len(pair_uniques))
    )
    se = np.sqrt(np.maximum(0.0, np.diag(cov)))
    tvals = np.divide(beta, se, out=np.full_like(beta, np.nan), where=se > 0)
    all_names = ["intercept"] + names
    return {
        "n": int(N),
        "date_clusters": int(len(date_uniques)),
        "symbol_clusters": int(len(symbol_uniques)),
        "coef": {n: float(v) for n, v in zip(all_names, beta)},
        "se": {n: float(v) for n, v in zip(all_names, se)},
        "t": {n: float(v) for n, v in zip(all_names, tvals)},
    }


def _same_day_dte_regression(frame: pd.DataFrame, target: str) -> dict:
    cols = ["har_daily_var", "har_weekly_var", "har_monthly_var", "abnormal_futstk_volume"]
    need = ["date", "dte_bucket", "symbol", target] + cols
    if frame is None or frame.empty or not set(need).issubset(frame.columns):
        return {"n": 0, "date_clusters": 0, "symbol_clusters": 0, "coef": {}, "se": {}, "t": {}}
    use = frame[need].copy()
    use[target] = pd.to_numeric(use[target], errors="coerce") * VAR_SCALE
    for c in cols[:3]:
        use[c] = pd.to_numeric(use[c], errors="coerce") * VAR_SCALE
    use["abnormal_futstk_volume"] = pd.to_numeric(use["abnormal_futstk_volume"], errors="coerce")
    valid = use[target].notna() & use[cols].notna().all(axis=1) & use["date"].notna() & use["dte_bucket"].notna()
    use = use.loc[valid].copy()
    if use.empty:
        return {"n": 0, "date_clusters": 0, "symbol_clusters": 0, "coef": {}, "se": {}, "t": {}}
    g = use.groupby(["date", "dte_bucket"], observed=True)
    y = use[target] - g[target].transform("mean")
    X = pd.DataFrame(index=use.index)
    for c in cols:
        X[c] = use[c] - g[c].transform("mean")
    return _two_way_cluster_robust_ols_fast(y, X, use["date"], use["symbol"])


def _earnings_flags(frame: pd.DataFrame, earnings_map: dict | None) -> tuple[pd.Series, dict]:
    if earnings_map is None:
        return pd.Series(False, index=frame.index, dtype=bool), {"provided": False, "audit_valid": False}
    flags, matched_symbols, examples = v98._earnings_window_flags(frame, dict(earnings_map or {}), radius=5)
    meta = dict((earnings_map or {}).get("_meta") or {})
    requested = int(meta.get("symbols_requested") or len({k for k in earnings_map if k != "_meta"}))
    with_dates = int(meta.get("symbols_with_dates") or sum(1 for k, v in earnings_map.items() if k != "_meta" and v is not None and len(v)))
    coverage = float(meta.get("symbol_date_coverage") if meta.get("symbol_date_coverage") is not None else (with_dates / requested if requested else 0.0))
    return flags, {"provided": True, "audit_valid": bool(requested > 0 and coverage >= 0.80 and len(matched_symbols) > 0), "symbols_requested": requested, "symbols_with_dates": with_dates, "symbol_date_coverage": coverage, "matched_symbol_count": int(len(matched_symbols)), "examples": examples}


def _symbol_concentration(pred: pd.DataFrame) -> dict:
    if pred is None or pred.empty:
        return {"symbols": 0, "top5_positive_cw_share": None}
    x = pred.copy(); y = pd.to_numeric(x["target"], errors="coerce"); fb = pd.to_numeric(x["har_forecast"], errors="coerce"); fa = pd.to_numeric(x["augmented_forecast"], errors="coerce")
    x["cw"] = (y - fb) ** 2 - (y - fa) ** 2 + (fb - fa) ** 2
    sums = x.groupby("symbol")["cw"].sum().clip(lower=0).sort_values(ascending=False)
    total = float(sums.sum())
    share = float(sums.head(5).sum() / total) if total > 1e-18 else None
    return {"symbols": int(x["symbol"].nunique()), "top5_positive_cw_share": share}


def evaluate_trial20(symbol_frames: Mapping[str, pd.DataFrame], *, earnings_map=None, min_train_obs: int = MIN_TRAIN_OBS, refit_every: int = REFIT_EVERY, require_earnings: bool = False) -> dict:
    frame = _stack(symbol_frames)
    base = {
        "build": BUILD_ID, "spec": trial20_spec(), "trial18_state": "LOCKED",
        "trial19_state": "CLOSED_ASSOCIATION_NOT_INCREMENTAL", "oi_role": "DIAGNOSTIC_ONLY",
        "research_only": True, "production_activation": False,
    }
    if frame.empty:
        return {**base, "status": "INCONCLUSIVE_NO_DATA", "pass": False}
    required = {"date", "symbol", "har_daily_var", "har_weekly_var", "har_monthly_var", "abnormal_futstk_volume", "next_yz_var", "next_gk_var", "dte_bucket"}
    missing = sorted(required - set(frame.columns))
    if missing:
        return {**base, "status": "INCONCLUSIVE_MISSING_TRIAL20_FIELDS", "pass": False, "missing_fields": missing}

    oos_frame = frame[(pd.to_datetime(frame["date"]).dt.normalize() >= INDEPENDENT_START) & (pd.to_datetime(frame["date"]).dt.normalize() <= INDEPENDENT_END)].copy()
    yz_pred = _oos_prediction_rows(frame, "next_yz_var", min_train_obs=min_train_obs, refit_every=refit_every)
    gk_pred = _oos_prediction_rows(frame, "next_gk_var", min_train_obs=min_train_obs, refit_every=refit_every)
    yz = _forecast_metrics(yz_pred); gk = _forecast_metrics(gk_pred)
    stability = _chronological_stability(yz_pred)
    topday = _top_day_sensitivity(yz_pred)
    concentration = _symbol_concentration(yz_pred)
    diagnostic = _same_day_dte_regression(oos_frame, "next_yz_var")

    flags, earn_audit = _earnings_flags(oos_frame, earnings_map)
    outside_metrics = None
    inside_metrics = None
    earnings_pass = True
    if earnings_map is not None:
        key_flags = pd.DataFrame({"date": pd.to_datetime(oos_frame["date"]).dt.normalize(), "symbol": oos_frame["symbol"].astype(str), "earnings_window": flags.to_numpy(dtype=bool)})
        yz_e = yz_pred.merge(key_flags, on=["date", "symbol"], how="left") if not yz_pred.empty else yz_pred.copy()
        if not yz_e.empty:
            yz_e["earnings_window"] = yz_e["earnings_window"].fillna(False).astype(bool)
            outside_metrics = _forecast_metrics(yz_e.loc[~yz_e["earnings_window"]])
            inside_metrics = _forecast_metrics(yz_e.loc[yz_e["earnings_window"]])
            earnings_pass = bool(earn_audit.get("audit_valid") and (outside_metrics.get("mse") or {}).get("improves") and (outside_metrics.get("qlike") or {}).get("improves"))
        else:
            earnings_pass = False
    elif require_earnings:
        earnings_pass = False

    enough = bool(yz.get("dates", 0) >= MIN_OOS_DATES)
    primary = bool((yz.get("mse") or {}).get("improves") and (yz.get("qlike") or {}).get("improves") and (yz.get("clark_west") or {}).get("pass") and (yz.get("oos_r2") or 0) > 0)
    gk_ok = bool((gk.get("mse") or {}).get("improves") and (gk.get("qlike") or {}).get("improves") and (gk.get("oos_r2") or 0) > 0)
    robust = bool(stability.get("pass") and topday.get("pass"))

    if not enough:
        gate_status = "INCONCLUSIVE_OOS_SAMPLE"
    elif require_earnings and not earn_audit.get("audit_valid"):
        gate_status = "INCONCLUSIVE_EARNINGS_JOIN"
    elif not primary:
        gate_status = "FAIL_VOLUME_NO_OOS_INCREMENTAL_VALUE"
    elif not gk_ok:
        gate_status = "FAIL_GK_ROBUSTNESS"
    elif not robust:
        gate_status = "FAIL_OOS_CONCENTRATION_OR_INSTABILITY"
    elif not earnings_pass:
        gate_status = "FAIL_EARNINGS_CONFOUND"
    else:
        gate_status = "PASS_TRIAL20_VOLUME_OOS_GATE"

    statistical_pass = gate_status == "PASS_TRIAL20_VOLUME_OOS_GATE"
    if gate_status.startswith("INCONCLUSIVE"):
        closure = {"status": gate_status, "promotion_allowed": False, "reason": "Integrity/data gate incomplete; Trial 20 cannot be closed from this run."}
    else:
        closure = _closure_interpretation(statistical_pass)

    return {
        **base, "status": closure["status"], "gate_status": gate_status, "statistical_pass": statistical_pass,
        "pass": False, "promotion_allowed": False, "closure": closure,
        "primary_oos": yz, "gk_oos": gk,
        "forecast_integrity": {"yang_zhang": _forecast_integrity(yz_pred), "garman_klass": _forecast_integrity(gk_pred)},
        "same_day_same_dte_regression": diagnostic,
        "chronological_stability": stability,
        "top3_day_sensitivity": topday,
        "concentration": concentration,
        "earnings": {"audit": earn_audit, "outside_oos": outside_metrics, "inside_oos": inside_metrics, "pass": earnings_pass},
        "gates": {"oos_sample_ok": enough, "mse_qlike_cw_ok": primary, "gk_robustness_ok": gk_ok, "chronological_stability_ok": bool(stability.get("pass")), "top3_day_sensitivity_ok": bool(topday.get("pass")), "earnings_ok": earnings_pass},
        "oos_prediction_count": int(len(yz_pred)), "oos_date_count": int(yz.get("dates") or 0),
    }
