"""V9.7 Trial 19 — nonlinear extreme total-FUTSTK-OI event validation.

Trial 19 is a third, older, non-overlapping evidence test.  It does not alter
V9.5.3 discovery thresholds or V9.6 Trial 17.  The claim is explicitly
nonlinear: only the binary tail event total OI z >= 1.5 is tested.
"""
from __future__ import annotations

from typing import Mapping
import numpy as np
import pandas as pd

from . import v95_daily_evidence as v95
from . import v953_contract_structure as cs
from . import v96_trial17 as v96

BUILD_ID = "2026-09-02-INSTITUTIONAL-V9.7.2-TRIAL19-CONFOUND-INTEGRITY-CLOSURE"
TRIAL19_NUMBER = 19
TRIAL18_NUMBER = 18
TOTAL_OI_Z_MIN = 1.5
INDEPENDENT_START = pd.Timestamp("2018-09-01")
INDEPENDENT_END = pd.Timestamp("2021-08-31")
MIN_EVENTS = 250
MIN_EVENT_DAYS = 250
MIN_MATCHED_LIFT = 1.10
T_STAT_HURDLE = 3.0
MWPL_BOUND_MAX_EVENT_OVERLAP = 0.05
MWPL_BOUND_MAX_LIFT_DELTA = 0.02


def trial19_spec() -> dict:
    return {
        "trial_number": TRIAL19_NUMBER,
        "name": "Nonlinear Extreme Total Futures OI Event -> Next-session Magnitude",
        "total_oi_z_min": TOTAL_OI_Z_MIN,
        "independent_start": str(INDEPENDENT_START.date()),
        "independent_end": str(INDEPENDENT_END.date()),
        "primary_horizon": "1D",
        "secondary_horizon": "2D",
        "secondary_2D_cannot_rescue_1D": True,
        "primary_baseline": "SAME_DAY_SAME_DTE_NON_EVENT",
        "inference_variable": "extreme_oi_event",
        "directional_prediction": False,
        "research_only": True,
        "min_events": MIN_EVENTS,
        "min_event_days": MIN_EVENT_DAYS,
        "min_matched_lift": MIN_MATCHED_LIFT,
        "binary_event_t_hurdle": T_STAT_HURDLE,
        "prior_locked_finals_untouched": True,
    }


def trial18_spec() -> dict:
    return {
        "trial_number": TRIAL18_NUMBER,
        "name": "Direction conditional on independently validated extreme OI event",
        "locked": True,
        "auto_run": False,
        "eligibility": "Only after frozen Trial-19 efficacy plus volatility, earnings and MWPL applied/bounded controls pass",
        "research_only": True,
    }


def _dte_bucket(frame: pd.DataFrame) -> pd.Series:
    field = "nse_near_dte" if "nse_near_dte" in frame.columns else "days_to_expiry"
    dte = pd.to_numeric(frame.get(field), errors="coerce")
    return pd.cut(dte, bins=[-0.001, 5, 10, 20, np.inf], labels=["0-5", "6-10", "11-20", "21+"], include_lowest=True)


def _prepare_symbol(symbol: str, frame: pd.DataFrame) -> pd.DataFrame:
    if frame is None or frame.empty:
        return pd.DataFrame()
    x = cs.build_contract_structure_frame(frame).copy()
    x["date"] = pd.DatetimeIndex(x.index).tz_localize(None).normalize()
    x = x[(x["date"] >= INDEPENDENT_START) & (x["date"] <= INDEPENDENT_END)].copy()
    if x.empty:
        return x
    x["symbol"] = str(symbol).upper()
    eligible = x.get("eligible", True)
    if not isinstance(eligible, pd.Series):
        eligible = pd.Series(bool(eligible), index=x.index)
    if "fno_member_pti" in x:
        eligible = eligible.fillna(False).astype(bool) & x["fno_member_pti"].fillna(False).astype(bool)
    x["trial19_eligible"] = eligible.fillna(False).astype(bool)
    x["extreme_oi_event"] = (pd.to_numeric(x.get("total_z"), errors="coerce") >= TOTAL_OI_Z_MIN).fillna(False).astype(bool)
    x["dte_bucket"] = _dte_bucket(x)
    return x.reset_index(drop=True)


def _stack(frames: Mapping[str, pd.DataFrame]) -> pd.DataFrame:
    rows = [_prepare_symbol(s, f) for s, f in frames.items()]
    rows = [r for r in rows if r is not None and not r.empty]
    return pd.concat(rows, ignore_index=True) if rows else pd.DataFrame()


def _matched_controls(events: pd.DataFrame, baseline: pd.DataFrame) -> pd.DataFrame:
    if events.empty or baseline.empty:
        return baseline.iloc[0:0].copy()
    ev = events.copy(); ba = baseline.copy()
    ev["date"] = pd.to_datetime(ev["date"]).dt.normalize(); ba["date"] = pd.to_datetime(ba["date"]).dt.normalize()
    if "dte_bucket" not in ev: ev["dte_bucket"] = _dte_bucket(ev)
    if "dte_bucket" not in ba: ba["dte_bucket"] = _dte_bucket(ba)
    groups = ev[["date", "dte_bucket"]].dropna().drop_duplicates()
    keys = pd.MultiIndex.from_frame(groups.astype({"dte_bucket": str}))
    ba_keys = pd.MultiIndex.from_frame(ba[["date", "dte_bucket"]].astype({"dte_bucket": str}))
    out = ba.loc[ba_keys.isin(keys)].copy()
    event_flag = out.get("extreme_oi_event", False)
    if not isinstance(event_flag, pd.Series): event_flag = pd.Series(bool(event_flag), index=out.index)
    out = out.loc[~event_flag.fillna(False).astype(bool)].copy()
    return out


def same_day_dte_matched_report(events: pd.DataFrame, baseline: pd.DataFrame, field: str, *, reps=1000, seed=970) -> dict:
    if events is None or baseline is None or events.empty or baseline.empty:
        return {"event_count":0,"baseline_count":0,"event_days":0,"matched_groups":0,"lift":None,"ci95_low":None,"ci95_high":None}
    ev=events.copy(); ba=baseline.copy()
    if "dte_bucket" not in ev: ev["dte_bucket"]=_dte_bucket(ev)
    if "dte_bucket" not in ba: ba["dte_bucket"]=_dte_bucket(ba)
    controls=_matched_controls(ev, ba)
    if len(controls):
        valid_groups=controls[["date","dte_bucket"]].dropna().drop_duplicates()
        valid_keys=pd.MultiIndex.from_frame(valid_groups.astype({"dte_bucket":str}))
        ev_keys=pd.MultiIndex.from_frame(ev[["date","dte_bucket"]].astype({"dte_bucket":str}))
        ev=ev.loc[ev_keys.isin(valid_keys)].copy()
    else:
        ev=ev.iloc[0:0].copy()
    matched_groups=int(ev[["date","dte_bucket"]].dropna().drop_duplicates().shape[0])
    boot=v95.day_cluster_bootstrap_lift(ev, controls, field, reps=reps, seed=seed) if len(controls) and len(ev) else {"lift":None,"ci95_low":None,"ci95_high":None,"clusters":0,"reps":int(reps)}
    return {
        "event_count":int(len(ev)),"baseline_count":int(len(controls)),
        "event_days":int(pd.to_datetime(ev["date"]).dt.normalize().nunique()),"matched_groups":matched_groups,
        "event_mean":float(pd.to_numeric(ev[field],errors="coerce").mean()) if len(ev) else None,
        "matched_baseline_mean":float(pd.to_numeric(controls[field],errors="coerce").mean()) if len(controls) else None,
        **boot,
    }


def _with_rv5_bucket(frame: pd.DataFrame) -> pd.DataFrame:
    """Attach pre-signal cross-sectional realised-volatility quintiles.

    Buckets are recomputed independently on each trading date using only the
    already-shifted ``realized_vol5_prev`` field.  This is a confound-control
    view and never changes the frozen Trial-19 event or baseline.
    """
    if frame is None or frame.empty:
        return pd.DataFrame() if frame is None else frame.copy()
    x = frame.copy()
    x["date"] = pd.to_datetime(x["date"]).dt.normalize()
    rv = pd.to_numeric(x.get("realized_vol5_prev"), errors="coerce")
    x["realized_vol5_prev"] = rv
    pct = x.groupby("date", observed=True)["realized_vol5_prev"].rank(method="average", pct=True)
    bucket = np.ceil(pct * 5.0).clip(1, 5)
    x["rv5_bucket"] = pd.Series(bucket, index=x.index).astype("Float64")
    return x


def _matched_controls_with_vol(events: pd.DataFrame, baseline: pd.DataFrame) -> pd.DataFrame:
    if events is None or baseline is None or events.empty or baseline.empty:
        return baseline.iloc[0:0].copy() if baseline is not None else pd.DataFrame()
    ev = _with_rv5_bucket(events)
    ba = _with_rv5_bucket(baseline)
    if "dte_bucket" not in ev:
        ev["dte_bucket"] = _dte_bucket(ev)
    if "dte_bucket" not in ba:
        ba["dte_bucket"] = _dte_bucket(ba)
    cols = ["date", "dte_bucket", "rv5_bucket"]
    groups = ev[cols].dropna().drop_duplicates()
    if groups.empty:
        return ba.iloc[0:0].copy()
    ev_keys = pd.MultiIndex.from_frame(groups.astype({"dte_bucket": str, "rv5_bucket": float}))
    ba_keys = pd.MultiIndex.from_frame(ba[cols].astype({"dte_bucket": str, "rv5_bucket": float}))
    out = ba.loc[ba_keys.isin(ev_keys)].copy()
    flag = out.get("extreme_oi_event", False)
    if not isinstance(flag, pd.Series):
        flag = pd.Series(bool(flag), index=out.index)
    return out.loc[~flag.fillna(False).astype(bool)].copy()


def same_day_dte_vol_matched_report(events: pd.DataFrame, baseline: pd.DataFrame, field: str, *, reps=1000, seed=974) -> dict:
    if events is None or baseline is None or events.empty or baseline.empty:
        return {"event_count": 0, "baseline_count": 0, "event_days": 0, "matched_groups": 0, "lift": None, "ci95_low": None, "ci95_high": None, "matched_group_columns": ["date", "dte_bucket", "rv5_bucket"]}
    ev = _with_rv5_bucket(events)
    ba = _with_rv5_bucket(baseline)
    if "dte_bucket" not in ev:
        ev["dte_bucket"] = _dte_bucket(ev)
    if "dte_bucket" not in ba:
        ba["dte_bucket"] = _dte_bucket(ba)
    controls = _matched_controls_with_vol(ev, ba)
    cols = ["date", "dte_bucket", "rv5_bucket"]
    if len(controls):
        valid_groups = controls[cols].dropna().drop_duplicates()
        valid_keys = pd.MultiIndex.from_frame(valid_groups.astype({"dte_bucket": str, "rv5_bucket": float}))
        event_keys = pd.MultiIndex.from_frame(ev[cols].astype({"dte_bucket": str, "rv5_bucket": float}))
        ev = ev.loc[event_keys.isin(valid_keys)].copy()
    else:
        ev = ev.iloc[0:0].copy()
    boot = v95.day_cluster_bootstrap_lift(ev, controls, field, reps=reps, seed=seed) if len(ev) and len(controls) else {"lift": None, "ci95_low": None, "ci95_high": None, "clusters": 0, "reps": int(reps)}
    return {
        "event_count": int(len(ev)),
        "baseline_count": int(len(controls)),
        "event_days": int(pd.to_datetime(ev["date"]).dt.normalize().nunique()) if len(ev) else 0,
        "matched_groups": int(ev[cols].dropna().drop_duplicates().shape[0]) if len(ev) else 0,
        "event_mean": float(pd.to_numeric(ev[field], errors="coerce").mean()) if len(ev) else None,
        "matched_baseline_mean": float(pd.to_numeric(controls[field], errors="coerce").mean()) if len(controls) else None,
        "matched_group_columns": cols,
        **boot,
    }


def pre_signal_persistence_report(events: pd.DataFrame, baseline: pd.DataFrame, *, reps=500) -> dict:
    reports = {}
    for key, field, seed in (("t_minus_1", "movement_prev1_atr", 9751), ("t_minus_2", "movement_prev2_atr", 9752)):
        if field not in baseline.columns or field not in events.columns:
            reports[key] = {"lift": None, "ci95_low": None, "ci95_high": None, "event_count": 0}
            continue
        ev = events[pd.to_numeric(events[field], errors="coerce").notna()].copy()
        ba = baseline[pd.to_numeric(baseline[field], errors="coerce").notna()].copy()
        reports[key] = same_day_dte_matched_report(ev, ba, field, reps=reps, seed=seed)
    reports["warning"] = any(
        (r.get("lift") or 0) >= 1.10 and (r.get("ci95_low") or 0) > 1.0
        for r in (reports["t_minus_1"], reports["t_minus_2"])
    )
    return reports


def evaluate_volatility_confound(symbol_frames: Mapping[str, pd.DataFrame], *, frozen_result: dict, bootstrap_reps=1000) -> dict:
    """Promotion-only prior-volatility control; never rewrites Trial 19."""
    gates = dict(frozen_result.get("gates") or {})
    efficacy_ok = all(bool(gates.get(k)) for k in ("sample_ok", "matched_lift_ok", "binary_event_t_ok", "tail_ok", "stability_ok"))
    if not efficacy_ok:
        return {"status": "LOCKED_TRIAL19_EFFICACY_NOT_PASSED", "pass": False, "research_only": True}
    stacked = _stack(symbol_frames)
    if stacked.empty or "realized_vol5_prev" not in stacked:
        return {"status": "INCONCLUSIVE_VOLATILITY_DATA", "pass": False, "research_only": True}
    baseline = stacked[stacked["trial19_eligible"].fillna(False).astype(bool)].copy()
    baseline = baseline[baseline["movement_1d_atr"].notna() & pd.to_numeric(baseline["realized_vol5_prev"], errors="coerce").notna()].copy()
    events = baseline[baseline["extreme_oi_event"].fillna(False).astype(bool)].copy()
    rep = same_day_dte_vol_matched_report(events, baseline, "movement_1d_atr", reps=bootstrap_reps, seed=9741)
    persistence = pre_signal_persistence_report(events, baseline, reps=max(100, min(int(bootstrap_reps), 500)))
    passed = bool((rep.get("lift") or 0) >= MIN_MATCHED_LIFT and (rep.get("ci95_low") or 0) > 1.0)
    return {
        "status": "PASS_VOLATILITY_CONFOUND" if passed else "FAIL_VOLATILITY_CONFOUND",
        "pass": passed,
        "matched_1D": rep,
        "pre_signal_persistence": persistence,
        "research_only": True,
        "production_activation": False,
    }


def _matched_universe(frame: pd.DataFrame) -> pd.DataFrame:
    if frame is None or frame.empty: return pd.DataFrame()
    x=frame.copy()
    x["date"]=pd.to_datetime(x["date"]).dt.normalize()
    if "dte_bucket" not in x: x["dte_bucket"]=_dte_bucket(x)
    grp=x.groupby(["date","dte_bucket"], observed=True)["extreme_oi_event"].agg(["any","all"])
    valid=grp[(grp["any"]) & (~grp["all"])].index
    key=pd.MultiIndex.from_frame(x[["date","dte_bucket"]])
    return x.loc[key.isin(valid)].copy()


def binary_event_two_way_report(frame: pd.DataFrame) -> dict:
    matched=_matched_universe(frame)
    if matched.empty:
        return {"n":0,"date_clusters":0,"symbol_clusters":0,"coef":{},"se":{},"t":{}}
    matched["event_indicator"] = matched["extreme_oi_event"].fillna(False).astype(float)
    group_cols=["date","dte_bucket"]
    y=pd.to_numeric(matched["movement_1d_atr"],errors="coerce")
    x=pd.DataFrame({"extreme_oi_event":matched["event_indicator"]},index=matched.index)
    for c in ("realized_vol20_prev","atr_pct_prev"):
        if c in matched: x[c]=pd.to_numeric(matched[c],errors="coerce")
    # Within same date+DTE transformation removes shared macro/session and roll state.
    g=matched.groupby(group_cols, observed=True)
    y_dm=y-g["movement_1d_atr"].transform(lambda s: pd.to_numeric(s,errors="coerce").mean())
    x_dm=x.copy()
    for c in x.columns:
        x_dm[c]=x[c]-g[c].transform("mean") if c in matched.columns else x[c]-g["event_indicator"].transform("mean")
    return v96.two_way_cluster_robust_ols(y_dm, x_dm, matched["date"], matched["symbol"])


def _top3_matched(events: pd.DataFrame, baseline: pd.DataFrame, field: str) -> dict:
    if events.empty: return {"removed_days":[],"lift":None}
    ev=events.copy(); ev["date"]=pd.to_datetime(ev["date"]).dt.normalize()
    by=ev.groupby("date")[field].mean().sort_values(ascending=False)
    removed=list(by.head(3).index)
    e2=ev[~ev["date"].isin(removed)].copy()
    b2=baseline[~pd.to_datetime(baseline["date"]).dt.normalize().isin(removed)].copy()
    rep=same_day_dte_matched_report(e2,b2,field,reps=200,seed=973)
    return {"removed_days":[str(pd.Timestamp(d).date()) for d in removed],"lift":rep.get("lift")}


def _chronological_matched(events: pd.DataFrame, baseline: pd.DataFrame, field: str, blocks=4) -> list[dict]:
    dates=np.asarray(sorted(pd.to_datetime(events["date"]).dt.normalize().unique()),dtype="datetime64[ns]") if len(events) else np.asarray([])
    out=[]
    for i,chunk in enumerate(np.array_split(dates,int(blocks)),start=1):
        if len(chunk)==0: continue
        ds=set(pd.Timestamp(d) for d in chunk)
        ee=events[pd.to_datetime(events["date"]).dt.normalize().isin(ds)]
        bb=baseline[pd.to_datetime(baseline["date"]).dt.normalize().isin(ds)]
        rep=same_day_dte_matched_report(ee,bb,field,reps=100,seed=970+i)
        out.append({"block":i,"start":str(pd.Timestamp(chunk[0]).date()),"end":str(pd.Timestamp(chunk[-1]).date()),"lift":rep.get("lift")})
    return out


def _concentration(events: pd.DataFrame) -> dict:
    if events.empty or "symbol" not in events:
        return {"symbols":0,"top5_symbol_event_share":None}
    c=events["symbol"].astype(str).value_counts()
    return {"symbols":int(len(c)),"top5_symbol_event_share":float(c.head(5).sum()/c.sum()) if c.sum() else None}


def trial19_verdict(*, event_count,event_days,matched_lift,ci_low,binary_event_t,top3_lift,positive_blocks,integrity_ok) -> str:
    if int(event_count)<MIN_EVENTS or int(event_days)<MIN_EVENT_DAYS: return "FAIL_INSUFFICIENT_SAMPLE"
    if matched_lift is None or ci_low is None or matched_lift<MIN_MATCHED_LIFT or ci_low<=1.0: return "FAIL_NO_MATCHED_LIFT"
    if binary_event_t is None or not np.isfinite(binary_event_t) or binary_event_t<T_STAT_HURDLE: return "FAIL_BINARY_EVENT_INFERENCE"
    if top3_lift is None or top3_lift<=1.0: return "FAIL_TAIL_DEPENDENCE"
    if int(positive_blocks)<3: return "FAIL_TIME_STABILITY"
    if not integrity_ok: return "INCONCLUSIVE_INTEGRITY"
    return "PASS_TRIAL19_INDEPENDENT"


def evaluate_trial19(symbol_frames: Mapping[str,pd.DataFrame], *, controls=None, bootstrap_reps=1000) -> dict:
    controls=dict(controls or {})
    stacked=_stack(symbol_frames)
    if stacked.empty:
        return {"build":BUILD_ID,"trial19":trial19_spec(),"trial18":trial18_spec(),"status":"INCONCLUSIVE_NO_DATA","primary_pass":False,"research_only":True,"production_activation":False,"prior_locked_finals_untouched":True}
    baseline=stacked[stacked["trial19_eligible"].fillna(False).astype(bool)].copy()
    baseline=baseline[baseline["movement_1d_atr"].notna()].copy()
    if controls.get("mwpl_available") and {"mwpl_pct","ban_flag"}.issubset(baseline.columns):
        mw=pd.to_numeric(baseline["mwpl_pct"],errors="coerce"); ban=baseline["ban_flag"].fillna(False).astype(bool)
        baseline=baseline[(~ban)&(mw<95.0)].copy()
    events=baseline[baseline["extreme_oi_event"].fillna(False).astype(bool)].copy()
    primary=same_day_dte_matched_report(events,baseline,"movement_1d_atr",reps=bootstrap_reps,seed=9719)
    secondary=same_day_dte_matched_report(events,baseline,"movement_2d_atr",reps=bootstrap_reps,seed=9720)
    tw=binary_event_two_way_report(baseline)
    t_event=(tw.get("t") or {}).get("extreme_oi_event")
    top3=_top3_matched(events,baseline,"movement_1d_atr")
    blocks=_chronological_matched(events,baseline,"movement_1d_atr",4)
    positive_blocks=sum(1 for b in blocks if (b.get("lift") or 0)>1.0)
    integrity_ok=all(bool(controls.get(k)) for k in ("historical_membership_available","historical_cash_price_available","lot_size_normalization_available","mwpl_available"))
    status=trial19_verdict(event_count=primary.get("event_count",0),event_days=primary.get("event_days",0),matched_lift=primary.get("lift"),ci_low=primary.get("ci95_low"),binary_event_t=t_event,top3_lift=top3.get("lift"),positive_blocks=positive_blocks,integrity_ok=integrity_ok)
    return {
        "build":BUILD_ID,"trial19":trial19_spec(),"trial18":trial18_spec(),"status":status,
        "primary_pass":status=="PASS_TRIAL19_INDEPENDENT","trial19_closed":status.startswith("FAIL_"),
        "research_only":True,"production_activation":False,"directional_prediction":False,"prior_locked_finals_untouched":True,
        "evidence_window":{"start":str(INDEPENDENT_START.date()),"end":str(INDEPENDENT_END.date()),"days":int(pd.to_datetime(baseline["date"]).dt.normalize().nunique())},
        "validation":{"1D":primary,"2D":secondary},"binary_event_regression_1D":tw,"top3_day_removed":top3,"chronological_blocks":blocks,"concentration":_concentration(events),
        "event_symbols":sorted(events["symbol"].astype(str).str.upper().unique().tolist()) if len(events) else [],
        "controls":{"historical_membership":"APPLIED" if controls.get("historical_membership_available") else "UNAVAILABLE","historical_cash_price":"APPLIED" if controls.get("historical_cash_price_available") else "UNAVAILABLE","lot_size_normalization":"APPLIED" if controls.get("lot_size_normalization_available") else "UNAVAILABLE","mwpl_control":"APPLIED" if controls.get("mwpl_available") else "UNAVAILABLE"},
        "gates":{"sample_ok":primary.get("event_count",0)>=MIN_EVENTS and primary.get("event_days",0)>=MIN_EVENT_DAYS,"matched_lift_ok":bool((primary.get("lift") or 0)>=MIN_MATCHED_LIFT and (primary.get("ci95_low") or 0)>1.0),"binary_event_t_ok":bool(t_event is not None and np.isfinite(t_event) and t_event>=T_STAT_HURDLE),"tail_ok":bool((top3.get("lift") or 0)>1.0),"stability_ok":positive_blocks>=3,"integrity_ok":integrity_ok},
    }


def mwpl_bound_non_load_bearing(event_overlap_fraction, absolute_lift_delta) -> bool:
    if event_overlap_fraction is None or absolute_lift_delta is None:
        return False
    try:
        overlap = float(event_overlap_fraction); delta = float(absolute_lift_delta)
    except Exception:
        return False
    return bool(np.isfinite(overlap) and np.isfinite(delta) and overlap <= MWPL_BOUND_MAX_EVENT_OVERLAP and delta <= MWPL_BOUND_MAX_LIFT_DELTA)


def evaluate_mwpl_bound(frame: pd.DataFrame, *, bootstrap_reps=500) -> dict:
    """Empirically bound ban/>=95% MWPL sensitivity on a recent window.

    This diagnostic never changes the frozen Trial-19 efficacy result.  It is
    used only when the older historical MWPL control cannot be completed.
    """
    if frame is None or frame.empty:
        return {"status": "INCONCLUSIVE_NO_DATA", "non_load_bearing": False, "event_count": 0, "risk_event_count": 0, "event_overlap_fraction": None, "absolute_lift_delta": None}
    x = frame.copy()
    x["date"] = pd.to_datetime(x["date"]).dt.normalize()
    if "dte_bucket" not in x:
        x["dte_bucket"] = _dte_bucket(x)
    eligible = x.get("trial19_eligible", True)
    if not isinstance(eligible, pd.Series):
        eligible = pd.Series(bool(eligible), index=x.index)
    baseline = x.loc[eligible.fillna(False).astype(bool) & pd.to_numeric(x.get("movement_1d_atr"), errors="coerce").notna()].copy()
    if baseline.empty:
        return {"status": "INCONCLUSIVE_NO_DATA", "non_load_bearing": False, "event_count": 0, "risk_event_count": 0, "event_overlap_fraction": None, "absolute_lift_delta": None}
    events = baseline[baseline.get("extreme_oi_event", False).fillna(False).astype(bool)].copy()
    ban = baseline.get("ban_flag", False)
    if not isinstance(ban, pd.Series):
        ban = pd.Series(bool(ban), index=baseline.index)
    mw = pd.to_numeric(baseline.get("mwpl_pct"), errors="coerce")
    risk = ban.fillna(False).astype(bool) | (mw >= 95.0).fillna(False)
    baseline["mwpl_risk"] = risk
    event_risk = baseline.loc[events.index, "mwpl_risk"] if len(events) else pd.Series(dtype=bool)
    overlap = float(event_risk.mean()) if len(event_risk) else None
    all_rep = same_day_dte_matched_report(events, baseline, "movement_1d_atr", reps=bootstrap_reps, seed=9761)
    clean = baseline.loc[~baseline["mwpl_risk"]].copy()
    clean_events = clean[clean.get("extreme_oi_event", False).fillna(False).astype(bool)].copy()
    clean_rep = same_day_dte_matched_report(clean_events, clean, "movement_1d_atr", reps=bootstrap_reps, seed=9762)
    a = all_rep.get("lift"); b = clean_rep.get("lift")
    delta = abs(float(a) - float(b)) if a is not None and b is not None and np.isfinite(a) and np.isfinite(b) else None
    non_load = mwpl_bound_non_load_bearing(overlap, delta)
    return {
        "status": "NON_LOAD_BEARING" if non_load else "LOAD_BEARING_OR_UNRESOLVED",
        "non_load_bearing": non_load,
        "event_count": int(len(events)),
        "risk_event_count": int(event_risk.sum()) if len(event_risk) else 0,
        "event_overlap_fraction": overlap,
        "all_1D": all_rep,
        "clean_1D": clean_rep,
        "absolute_lift_delta": delta,
        "bars": {"max_event_overlap": MWPL_BOUND_MAX_EVENT_OVERLAP, "max_lift_delta": MWPL_BOUND_MAX_LIFT_DELTA},
        "research_only": True,
    }


def trial19_efficacy_passed(frozen_result: dict) -> bool:
    gates = dict((frozen_result or {}).get("gates") or {})
    return all(bool(gates.get(k)) for k in ("sample_ok", "matched_lift_ok", "binary_event_t_ok", "tail_ok", "stability_ok"))


def evaluate_trial18_eligibility(*, frozen_result: dict, volatility_control: dict, earnings_control: dict, integrity_controls: dict, recent_mwpl_bound=None) -> dict:
    """Combine pre-registered promotion controls without altering Trial 19."""
    reasons = []
    if not trial19_efficacy_passed(frozen_result):
        reasons.append("TRIAL19_EFFICACY")
    for key, label in (("historical_membership_available", "HISTORICAL_MEMBERSHIP"), ("historical_cash_price_available", "HISTORICAL_CASH"), ("lot_size_normalization_available", "OI_NORMALIZATION")):
        if not bool((integrity_controls or {}).get(key)):
            reasons.append(label)
    if not bool((volatility_control or {}).get("pass")):
        reasons.append("VOLATILITY_CONFOUND")
    if not bool((earnings_control or {}).get("confound_pass")):
        reasons.append("EARNINGS_CONFOUND")
    mwpl_applied = bool((integrity_controls or {}).get("mwpl_available"))
    mwpl_bounded = bool((recent_mwpl_bound or {}).get("non_load_bearing"))
    if not (mwpl_applied or mwpl_bounded):
        reasons.append("MWPL_INTEGRITY_OR_BOUND")
    eligible = not reasons
    return {
        "status": "ELIGIBLE_FOR_PREREGISTRATION" if eligible else "LOCKED",
        "trial18_eligible": eligible,
        "trial18_state": "ELIGIBLE_FOR_PREREGISTRATION" if eligible else "LOCKED",
        "reasons": reasons,
        "mwpl_resolution": "APPLIED" if mwpl_applied else ("EMPIRICALLY_NON_LOAD_BEARING" if mwpl_bounded else "UNRESOLVED"),
        "research_only": True,
        "auto_run": False,
        "production_activation": False,
    }


def evaluate_earnings_promotion(symbol_frames: Mapping[str,pd.DataFrame], *, frozen_result:dict, earnings_map=None, bootstrap_reps=1000) -> dict:
    # V9.7.2: earnings is a competing-hypothesis diagnostic and therefore
    # runs once the frozen efficacy gates pass even if MWPL integrity remains
    # unresolved.  It can never by itself unlock Trial 18.
    if not trial19_efficacy_passed(frozen_result):
        return {"status":"LOCKED_TRIAL19_EFFICACY_NOT_PASSED","trial18_eligible":False,"confound_pass":False,"research_only":True}
    stacked=_stack(symbol_frames)
    if stacked.empty: return {"status":"INCONCLUSIVE_NO_DATA","trial18_eligible":False,"research_only":True}
    baseline=stacked[stacked["trial19_eligible"].fillna(False).astype(bool)].copy()
    if {"mwpl_pct","ban_flag"}.issubset(baseline.columns):
        mw=pd.to_numeric(baseline["mwpl_pct"],errors="coerce"); ban=baseline["ban_flag"].fillna(False).astype(bool); baseline=baseline[(~ban)&(mw<95.0)].copy()
    events=baseline[baseline["extreme_oi_event"].fillna(False).astype(bool)].copy()
    emap=dict(earnings_map or {}); meta=dict(emap.get("_meta") or {})
    coverage=float(meta.get("symbol_coverage") or 0.0)
    excluded=set()
    for sym, dates in emap.items():
        if sym=="_meta": continue
        for d in list(dates or []):
            d=pd.Timestamp(d).normalize()
            # +/-5 trading sessions approximated on the observed eligible symbol calendar below.
            sym_dates=sorted(pd.to_datetime(baseline.loc[baseline["symbol"].eq(str(sym).upper()),"date"]).dt.normalize().unique())
            if not sym_dates: continue
            arr=pd.DatetimeIndex(sym_dates); pos=arr.searchsorted(d)
            lo=max(0,pos-5); hi=min(len(arr),pos+6)
            excluded.update((str(sym).upper(),pd.Timestamp(x).normalize()) for x in arr[lo:hi])
    if excluded:
        keys=pd.MultiIndex.from_frame(baseline[["symbol","date"]].assign(symbol=lambda x:x["symbol"].astype(str).str.upper(),date=lambda x:pd.to_datetime(x["date"]).dt.normalize()))
        exidx=pd.MultiIndex.from_tuples(list(excluded),names=["symbol","date"])
        clean=baseline.loc[~keys.isin(exidx)].copy()
    else:
        clean=baseline.copy()
    cevents=clean[clean["extreme_oi_event"].fillna(False).astype(bool)].copy()
    rep=same_day_dte_matched_report(cevents,clean,"movement_1d_atr",reps=bootstrap_reps,seed=9718)
    pass_control=bool(coverage>=0.90 and (rep.get("lift") or 0)>=MIN_MATCHED_LIFT and (rep.get("ci95_low") or 0)>1.0)
    removed_events = max(0, int(len(events) - len(cevents)))
    return {
        "status":"PASS_EARNINGS_PROMOTION" if pass_control else ("INCONCLUSIVE_EARNINGS_COVERAGE" if coverage<0.90 else "FAIL_EARNINGS_PROMOTION"),
        "trial18_eligible":False,
        "trial18_state":"LOCKED",
        "confound_pass":pass_control,
        "earnings_symbol_coverage":coverage,
        "events_removed":removed_events,
        "earnings_excluded_1D":rep,
        "research_only":True,
        "production_activation":False,
    }
