"""V10.0 directional edge laboratory.

Research-only Trials 21/22.  Trial 23 is locked unless both independent
families later pass their preregistered validation gates.  This module has no
production activation path and does not modify the live Opportunity Radar.
"""
from __future__ import annotations

import math
from typing import Mapping

import numpy as np
import pandas as pd

BUILD_ID = "2026-09-03-INSTITUTIONAL-V10.0.0-DIRECTIONAL-EDGE-LAB"
RESEARCH_START = pd.Timestamp("2018-09-01")
WARMUP_START = pd.Timestamp("2018-05-01")
RESEARCH_END = pd.Timestamp("2026-08-31")
ROUND_TRIP_COST = 0.0018
TRIAL21_RESID_BULL_PCT = 90.0
TRIAL21_RESID_BEAR_PCT = 10.0
TRIAL21_SECTOR_BULL_PCT = 70.0
TRIAL21_SECTOR_BEAR_PCT = 30.0
TRIAL22_BASIS_Z = 1.5
MIN_BETA_OBS = 40
BETA_WINDOW = 60


def spec() -> dict:
    return {
        "build": BUILD_ID,
        "research_only": True,
        "window": [str(RESEARCH_START.date()), str(RESEARCH_END.date())],
        "split": "60/20/20 chronological; final unread",
        "cost_round_trip": ROUND_TRIP_COST,
        "trial21": "hierarchical residual strength",
        "trial22": "carry-normalized futures basis innovation",
        "trial23": "LOCKED_PENDING_TRIAL21_AND_22",
        "production_activation": False,
    }


def _daily_index(index) -> pd.DatetimeIndex:
    idx = pd.DatetimeIndex(index)
    if idx.tz is not None:
        idx = idx.tz_convert("Asia/Kolkata").tz_localize(None)
    return idx.normalize()


def _close_returns(frame: pd.DataFrame) -> pd.Series:
    close = pd.to_numeric(frame["close"], errors="coerce")
    close.index = _daily_index(frame.index)
    return close.pct_change(fill_method=None)


def trial21_features(stock: pd.DataFrame, market: pd.DataFrame, sector: pd.DataFrame,
                     *, beta_window: int = BETA_WINDOW, min_beta_obs: int = MIN_BETA_OBS) -> pd.DataFrame:
    """Build no-lookahead stock-vs-market-and-sector residual strength."""
    if stock is None or market is None or sector is None or stock.empty or market.empty or sector.empty:
        return pd.DataFrame()
    idx = _daily_index(stock.index)
    out = stock.copy()
    out.index = idx
    rs = _close_returns(stock).reindex(idx)
    rm = _close_returns(market).reindex(idx)
    rsec = _close_returns(sector).reindex(idx)
    resid = pd.Series(np.nan, index=idx, dtype=float)
    for i in range(len(idx)):
        # Fit only on observations strictly before t, then score return t.
        start = max(0, i - int(beta_window))
        hist = pd.DataFrame({"y": rs.iloc[start:i], "m": rm.iloc[start:i], "s": rsec.iloc[start:i]}).dropna()
        if len(hist) < int(min_beta_obs) or not np.isfinite([rs.iloc[i], rm.iloc[i], rsec.iloc[i]]).all():
            continue
        X = np.column_stack([np.ones(len(hist)), hist["m"].to_numpy(), hist["s"].to_numpy()])
        beta = np.linalg.pinv(X) @ hist["y"].to_numpy()
        resid.iloc[i] = float(rs.iloc[i] - (beta[0] + beta[1] * rm.iloc[i] + beta[2] * rsec.iloc[i]))
    out["residual_return"] = resid
    out["resid_5d"] = resid.rolling(5, min_periods=5).sum()
    sector_close = pd.to_numeric(sector["close"], errors="coerce").copy()
    sector_close.index = _daily_index(sector.index)
    out["sector_5d"] = sector_close.pct_change(5, fill_method=None).reindex(idx)
    close = pd.to_numeric(out["close"], errors="coerce")
    out["abs_ret_20d"] = close.pct_change(20, fill_method=None)
    return out


def build_directional_outcomes(frame: pd.DataFrame) -> pd.DataFrame:
    """Attach executable next-open -> next-close and two-session net returns."""
    out = frame.copy()
    out.index = _daily_index(out.index)
    op = pd.to_numeric(out["open"], errors="coerce")
    cl = pd.to_numeric(out["close"], errors="coerce")
    entry = op.shift(-1)
    exit1 = cl.shift(-1)
    exit2 = cl.shift(-2)
    out["entry_next_open"] = entry
    out["exit_1d_close"] = exit1
    out["exit_2d_close"] = exit2
    out["long_1d_net"] = exit1 / entry - 1.0 - ROUND_TRIP_COST
    out["short_1d_net"] = entry / exit1 - 1.0 - ROUND_TRIP_COST
    out["long_2d_net"] = exit2 / entry - 1.0 - ROUND_TRIP_COST
    out["short_2d_net"] = entry / exit2 - 1.0 - ROUND_TRIP_COST
    return out


def _rank_0_100(s: pd.Series) -> pd.Series:
    x = pd.to_numeric(s, errors="coerce")
    n = int(x.notna().sum())
    if n <= 1:
        return pd.Series(np.nan, index=s.index, dtype=float)
    r = x.rank(method="average", ascending=True)
    return (r - 1.0) / (n - 1.0) * 100.0


def apply_trial21_cross_sectional_rules(rows: pd.DataFrame) -> pd.DataFrame:
    out = rows.copy()
    if out.empty:
        out["resid_pct"] = pd.Series(dtype=float); out["sector_pct"] = pd.Series(dtype=float)
        out["trial21_bull"] = pd.Series(dtype=bool); out["trial21_bear"] = pd.Series(dtype=bool)
        return out
    out["date"] = pd.to_datetime(out["date"]).dt.normalize()
    out["resid_pct"] = out.groupby("date", group_keys=False)["resid_5d"].transform(_rank_0_100)
    if "sector" in out.columns and out["sector"].notna().any():
        sector_rows = (out.dropna(subset=["sector"])
                         .groupby(["date", "sector"], as_index=False)["sector_5d"].mean())
        sector_rows["sector_pct"] = sector_rows.groupby("date", group_keys=False)["sector_5d"].transform(_rank_0_100)
        out = out.merge(sector_rows[["date", "sector", "sector_pct"]], on=["date", "sector"], how="left", sort=False)
    else:
        out["sector_pct"] = out.groupby("date", group_keys=False)["sector_5d"].transform(_rank_0_100)
    out["trial21_bull"] = (
        (out["resid_pct"] >= TRIAL21_RESID_BULL_PCT)
        & (out["sector_pct"] >= TRIAL21_SECTOR_BULL_PCT)
        & (pd.to_numeric(out["abs_ret_20d"], errors="coerce") > 0)
    ).fillna(False)
    out["trial21_bear"] = (
        (out["resid_pct"] <= TRIAL21_RESID_BEAR_PCT)
        & (out["sector_pct"] <= TRIAL21_SECTOR_BEAR_PCT)
        & (pd.to_numeric(out["abs_ret_20d"], errors="coerce") < 0)
    ).fillna(False)
    return out


def partition_dates(rows: pd.DataFrame) -> tuple[set[pd.Timestamp], set[pd.Timestamp], set[pd.Timestamp]]:
    dates = sorted(pd.to_datetime(rows["date"]).dt.normalize().dropna().unique()) if len(rows) else []
    n = len(dates); a = int(math.floor(n * 0.60)); b = int(math.floor(n * 0.80))
    norm = [pd.Timestamp(d).normalize() for d in dates]
    return set(norm[:a]), set(norm[a:b]), set(norm[b:])


def trial22_features(frame: pd.DataFrame, *, min_fit_obs: int = 40, refit_every: int = 20) -> pd.DataFrame:
    """Build carry-normalized, point-in-time basis innovation."""
    if frame is None or frame.empty:
        return pd.DataFrame()
    out = frame.copy()
    out.index = _daily_index(out.index)
    spot = pd.to_numeric(out["close"], errors="coerce")
    near = pd.to_numeric(out.get("near_settle"), errors="coerce")
    nxt = pd.to_numeric(out.get("next_settle"), errors="coerce")
    near_exp = pd.to_datetime(out.get("near_expiry"), errors="coerce")
    next_exp = pd.to_datetime(out.get("next_expiry"), errors="coerce")
    dte = pd.Series((near_exp.to_numpy(dtype="datetime64[ns]") - out.index.to_numpy(dtype="datetime64[ns]")) / np.timedelta64(1, "D"), index=out.index, dtype=float)
    next_dte = pd.Series((next_exp.to_numpy(dtype="datetime64[ns]") - out.index.to_numpy(dtype="datetime64[ns]")) / np.timedelta64(1, "D"), index=out.index, dtype=float)
    safe_dte = dte.clip(lower=1.0)
    spread_days = (next_dte - dte).clip(lower=1.0)
    out["near_dte"] = dte
    out["next_dte"] = next_dte
    out["near_basis_ann"] = np.log(near.where(near > 0) / spot.where(spot > 0)) * 365.0 / safe_dte
    out["curve_slope_ann"] = np.log(nxt.where(nxt > 0) / near.where(near > 0)) * 365.0 / spread_days

    basis = pd.to_numeric(out["near_basis_ann"], errors="coerce")
    design = pd.DataFrame(index=out.index)
    design["basis_ma60_prev"] = basis.rolling(60, min_periods=max(15, min_fit_obs // 2)).mean().shift(1)
    design["dte"] = dte
    design["trend"] = np.arange(len(out), dtype=float) / 252.0
    dow = out.index.dayofweek
    for k in range(1, 5):
        design[f"dow_{k}"] = (dow == k).astype(float)
    cols = list(design.columns)
    resid = pd.Series(np.nan, index=out.index, dtype=float)
    beta = None; fitted_at = -10**9
    for i in range(len(out)):
        if not np.isfinite(basis.iloc[i]) or design.iloc[i].isna().any():
            continue
        prior = np.arange(i)
        if len(prior):
            valid = basis.iloc[prior].notna().to_numpy() & design.iloc[prior].notna().all(axis=1).to_numpy()
            prior = prior[valid]
        if len(prior) < int(min_fit_obs):
            continue
        if beta is None or i - fitted_at >= int(refit_every):
            X = design.iloc[prior][cols].to_numpy(dtype=float)
            y = basis.iloc[prior].to_numpy(dtype=float)
            A = np.column_stack([np.ones(len(X)), X])
            beta = np.linalg.pinv(A) @ y
            fitted_at = i
        x = design.iloc[i][cols].to_numpy(dtype=float)
        expected = float(beta[0] + x @ beta[1:])
        resid.iloc[i] = float(basis.iloc[i] - expected)
    sd_prev = resid.rolling(60, min_periods=max(15, min_fit_obs // 2)).std(ddof=1).shift(1)
    out["basis_expected"] = basis - resid
    out["basis_innovation"] = resid
    out["basis_resid_sd60_prev"] = sd_prev
    out["basis_innovation_z"] = resid / sd_prev.where(sd_prev > 1e-12)
    return out


def apply_trial22_rules(rows: pd.DataFrame) -> pd.DataFrame:
    out = rows.copy()
    z = pd.to_numeric(out.get("basis_innovation_z"), errors="coerce")
    slope = pd.to_numeric(out.get("curve_slope_ann"), errors="coerce")
    out["trial22_bull"] = ((z >= TRIAL22_BASIS_Z) & (slope >= 0)).fillna(False)
    out["trial22_bear"] = ((z <= -TRIAL22_BASIS_Z) & (slope <= 0)).fillna(False)
    return out

MIN_EVENTS = 250
MIN_EVENT_DAYS = 120
MIN_T = 3.0
MIN_PF = 1.25
MIN_POSITIVE_BLOCKS = 3
MAX_TOP5_SYMBOL_POS_SHARE = 0.40
MAX_TOP3_SECTOR_POS_SHARE = 0.65


def _finite_or_none(value):
    try:
        x=float(value)
    except (TypeError, ValueError):
        return None
    return x if np.isfinite(x) else None


def _empty_direction_report():
    return {"event_count":0,"event_days":0,"mean_net":None,"win_rate":None,"avg_winner":None,"avg_loser":None,
            "profit_factor":None,"profit_factor_infinite":False,"day_cluster_t":None,"ci95":[None,None],
            "positive_blocks":0,"blocks":[],"top3_removed_days":[],"top3_removed_mean":None,
            "top5_symbol_positive_share":None,"top3_sector_positive_share":None,
            "failed_gates":["EVENTS","DAYS","EXPECTANCY","T","PF","BLOCKS","TOP3","CONCENTRATION"],"pass":False}


def _direction_report(events: pd.DataFrame, field: str, *, bootstrap_reps: int = 300) -> dict:
    x = events.copy() if events is not None else pd.DataFrame()
    if x.empty or field not in x:
        return _empty_direction_report()
    x["date"] = pd.to_datetime(x["date"]).dt.normalize()
    x[field] = pd.to_numeric(x[field], errors="coerce")
    x = x.dropna(subset=["date", field]).copy()
    if x.empty:
        return _empty_direction_report()
    n = int(len(x)); days = int(x["date"].nunique())
    vals = x[field]
    mean = float(vals.mean()) if n else None
    pos = float(vals[vals > 0].sum()); neg = float(-vals[vals < 0].sum())
    pf_infinite = bool(neg <= 0 and pos > 0)
    pf = float(pos / neg) if neg > 0 else None
    day_mean = x.groupby("date")[field].mean().sort_index()
    if len(day_mean) >= 2:
        sd = float(day_mean.std(ddof=1)); se = sd / math.sqrt(len(day_mean))
        t = float(day_mean.mean() / se) if se > 0 else None
    else:
        t = None
    rng = np.random.default_rng(1000)
    boots=[]
    arr=day_mean.to_numpy(dtype=float)
    if len(arr):
        for _ in range(int(bootstrap_reps)):
            boots.append(float(rng.choice(arr, size=len(arr), replace=True).mean()))
    ci=[float(np.quantile(boots,0.025)), float(np.quantile(boots,0.975))] if boots else [None,None]
    blocks=[]
    date_chunks=np.array_split(day_mean.index.to_numpy(), 4) if len(day_mean) else []
    for i, chunk in enumerate(date_chunks,1):
        if len(chunk)==0: continue
        sub=day_mean.loc[pd.DatetimeIndex(chunk)]
        blocks.append({"block":i,"start":str(pd.Timestamp(chunk[0]).date()),"end":str(pd.Timestamp(chunk[-1]).date()),"mean_net":float(sub.mean()),"positive":bool(sub.mean()>0)})
    positive_blocks=sum(bool(b["positive"]) for b in blocks)
    top_days=list(day_mean.sort_values(ascending=False).head(3).index)
    top3 = x[~x["date"].isin(top_days)]
    top3_mean=float(top3[field].mean()) if len(top3) else None
    pos_rows=x[x[field]>0].copy(); total_pos=float(pos_rows[field].sum())
    if total_pos>0 and "symbol" in pos_rows:
        sym=pos_rows.groupby("symbol")[field].sum().sort_values(ascending=False)
        top5_share=float(sym.head(5).sum()/total_pos)
    else: top5_share=None
    if total_pos>0 and "sector" in pos_rows and pos_rows["sector"].notna().any():
        sec=pos_rows.dropna(subset=["sector"]).groupby("sector")[field].sum().sort_values(ascending=False)
        sec_total=float(sec.sum()); top3_sec=float(sec.head(3).sum()/sec_total) if sec_total>0 else None
    else: top3_sec=None
    failed=[]
    if n < MIN_EVENTS: failed.append("EVENTS")
    if days < MIN_EVENT_DAYS: failed.append("DAYS")
    if mean is None or mean <= 0: failed.append("EXPECTANCY")
    if t is None or t < MIN_T: failed.append("T")
    if not (pf_infinite or (pf is not None and pf >= MIN_PF)): failed.append("PF")
    if positive_blocks < MIN_POSITIVE_BLOCKS: failed.append("BLOCKS")
    if top3_mean is None or top3_mean <= 0: failed.append("TOP3")
    if top5_share is not None and top5_share > MAX_TOP5_SYMBOL_POS_SHARE: failed.append("CONCENTRATION")
    if top3_sec is not None and top3_sec > MAX_TOP3_SECTOR_POS_SHARE: failed.append("SECTOR_CONCENTRATION")
    return {"event_count":n,"event_days":days,"mean_net":mean,"win_rate":float((vals>0).mean()),
            "avg_winner":float(vals[vals>0].mean()) if (vals>0).any() else None,
            "avg_loser":float(vals[vals<0].mean()) if (vals<0).any() else None,
            "profit_factor":pf,"profit_factor_infinite":pf_infinite,"day_cluster_t":t,"ci95":ci,"positive_blocks":int(positive_blocks),"blocks":blocks,
            "top3_removed_days":[str(pd.Timestamp(d).date()) for d in top_days],"top3_removed_mean":top3_mean,
            "top5_symbol_positive_share":top5_share,"top3_sector_positive_share":top3_sec,
            "failed_gates":failed,"pass":not failed}


def _trial_status(bull: dict, bear: dict) -> str:
    bp=bool(bull.get("pass")); sp=bool(bear.get("pass"))
    if bp and sp: return "PASS_BOTH"
    if bp: return "PASS_BULL_ONLY"
    if sp: return "PASS_BEAR_ONLY"
    return "FAIL"


def _stack_feature_frames(frames: Mapping[str,pd.DataFrame]) -> pd.DataFrame:
    rows=[]
    for sym,f in dict(frames or {}).items():
        if f is None or f.empty: continue
        x=f.copy(); x.index=_daily_index(x.index); x["date"]=x.index; x["symbol"]=str(sym).upper()
        rows.append(x.reset_index(drop=True))
    if not rows: return pd.DataFrame()
    out=pd.concat(rows,ignore_index=True)
    return out[(out["date"]>=RESEARCH_START)&(out["date"]<=RESEARCH_END)].copy()


def evaluate_trial21(frames: Mapping[str,pd.DataFrame], *, bootstrap_reps: int = 300) -> dict:
    rows=_stack_feature_frames(frames)
    if rows.empty: return {"trial":21,"status":"NO_DATA","pass":False,"final_locked":True}
    rows=apply_trial21_cross_sectional_rules(rows)
    dev,val,final=partition_dates(rows)
    valrows=rows[rows["date"].isin(val)].copy()
    bull_events=valrows[valrows["trial21_bull"]]
    bear_events=valrows[valrows["trial21_bear"]]
    bull=_direction_report(bull_events,"long_1d_net",bootstrap_reps=bootstrap_reps)
    bear=_direction_report(bear_events,"short_1d_net",bootstrap_reps=bootstrap_reps)
    bull_2d=_direction_report(bull_events,"long_2d_net",bootstrap_reps=bootstrap_reps)
    bear_2d=_direction_report(bear_events,"short_2d_net",bootstrap_reps=bootstrap_reps)
    devrows=rows[rows["date"].isin(dev)].copy()
    dev_summary={"bull_mean_net":_finite_or_none(devrows.loc[devrows["trial21_bull"],"long_1d_net"].mean()) if devrows["trial21_bull"].any() else None,
                 "bear_mean_net":_finite_or_none(devrows.loc[devrows["trial21_bear"],"short_1d_net"].mean()) if devrows["trial21_bear"].any() else None}
    status=_trial_status(bull,bear)
    return {"trial":21,"name":"Hierarchical Residual Strength","status":status,"pass":status.startswith("PASS"),
            "bull":bull,"bear":bear,"bull_2d":bull_2d,"bear_2d":bear_2d,"secondary_2d_can_rescue":False,"development_descriptive":dev_summary,"validation_dates":len(val),
            "final_locked":True,"final_read":False,"final_fraction":0.20,"production_activation":False}


def evaluate_trial22(frames: Mapping[str,pd.DataFrame], *, bootstrap_reps: int = 300) -> dict:
    rows=_stack_feature_frames(frames)
    if rows.empty: return {"trial":22,"status":"NO_DATA","pass":False,"final_locked":True}
    rows=apply_trial22_rules(rows)
    dev,val,final=partition_dates(rows)
    valrows=rows[rows["date"].isin(val)].copy()
    bull_events=valrows[valrows["trial22_bull"]]
    bear_events=valrows[valrows["trial22_bear"]]
    bull=_direction_report(bull_events,"long_1d_net",bootstrap_reps=bootstrap_reps)
    bear=_direction_report(bear_events,"short_1d_net",bootstrap_reps=bootstrap_reps)
    bull_2d=_direction_report(bull_events,"long_2d_net",bootstrap_reps=bootstrap_reps)
    bear_2d=_direction_report(bear_events,"short_2d_net",bootstrap_reps=bootstrap_reps)
    devrows=rows[rows["date"].isin(dev)].copy()
    dev_summary={"bull_mean_net":_finite_or_none(devrows.loc[devrows["trial22_bull"],"long_1d_net"].mean()) if devrows["trial22_bull"].any() else None,
                 "bear_mean_net":_finite_or_none(devrows.loc[devrows["trial22_bear"],"short_1d_net"].mean()) if devrows["trial22_bear"].any() else None}
    status=_trial_status(bull,bear)
    return {"trial":22,"name":"Carry-Normalized Futures Basis Innovation","status":status,"pass":status.startswith("PASS"),
            "bull":bull,"bear":bear,"bull_2d":bull_2d,"bear_2d":bear_2d,"secondary_2d_can_rescue":False,"development_descriptive":dev_summary,"validation_dates":len(val),
            "final_locked":True,"final_read":False,"final_fraction":0.20,"production_activation":False}


def evaluate_v10(trial21_frames: Mapping[str,pd.DataFrame], trial22_frames: Mapping[str,pd.DataFrame], *, bootstrap_reps: int = 300) -> dict:
    return {"build":BUILD_ID,"research_only":True,
            "trial21":evaluate_trial21(trial21_frames,bootstrap_reps=bootstrap_reps),
            "trial22":evaluate_trial22(trial22_frames,bootstrap_reps=bootstrap_reps),
            "trial23_state":"LOCKED_PENDING_TRIAL21_AND_22","trial23_evaluated":False,
            "trial18_state":"LOCKED","trial19_state":"CLOSED_ASSOCIATION_NOT_INCREMENTAL","trial20_state":"CLOSED_REJECTED_LOG_RV_CONFIRMED",
            "production_activation":False,"active_playbooks_unchanged":True}
