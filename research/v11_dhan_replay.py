#!/usr/bin/env python3
"""Scalable Dhan-backed V1.1 empirical-null replay.

RESEARCH ONLY. This module is isolated from production and is never imported by
the scanner. It implements the preregistered 1-minute follow-up without loading
the full ~12M-row panel into RAM.
"""

from __future__ import annotations

import argparse
import json
import math
import re
from collections import defaultdict
from pathlib import Path

import numpy as np
import pandas as pd
from numba import njit
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import roc_auc_score, brier_score_loss

HORIZONS = np.array([1, 2, 3, 5, 10, 15], dtype=np.int64)
H_NAMES = ("1m","2m","3m","5m","10m","15m")
CAL_MIN_DAYS = 40
CAL_MAX_DAYS = 60
EMPIRICAL_Q = 0.99
CUSUM_K = 0.50
CUSUM_H = 6.0
RESID_CUSUM_H = 7.5
FORWARD_H = (5, 15, 30)
COST_HURDLES = (5.0, 10.0, 15.0)
RNG_SEED = 20260930
BIN_WIDTH = 0.002
BIN_MAX = 30.0
N_BINS = int(BIN_MAX / BIN_WIDTH) + 1

METRICS = (
    "max_abs_z",
    "z_1m","z_2m","z_3m","z_5m","z_10m","z_15m",
    "raw_z_5m","mkt_z_5m",
)
MIDX = {name:i for i,name in enumerate(METRICS)}
FEATURE_COLS = [
    "z_1m","z_2m","z_3m","z_5m","z_10m","z_15m",
    "selected_horizon_min","selected_z","max_abs_z",
    "cusum_up","cusum_down","resid_cusum_up","resid_cusum_down",
    "robust_sigma","residual_coherence","minute_of_day","recent_vol_ratio",
]


def norm(s: object) -> str:
    return re.sub(r"[^A-Z0-9]+", "", str(s or "").upper())


@njit(cache=True)
def _median(x):
    y = np.sort(x.copy())
    n = len(y)
    if n == 0:
        return np.nan
    return y[n//2] if n % 2 else 0.5*(y[n//2-1]+y[n//2])


@njit(cache=True)
def _prior_scale(arr, day, i, window=15):
    tmp = np.empty(window, dtype=np.float64)
    n = 0
    start = max(0, i-window)
    for j in range(start, i):
        if day[j] == day[i] and np.isfinite(arr[j]):
            tmp[n] = arr[j]
            n += 1
    if n < 6:
        return np.nan
    vals = tmp[:n]
    med = _median(vals)
    dev = np.empty(n, dtype=np.float64)
    for k in range(n):
        dev[k] = abs(vals[k]-med)
    scale = 1.4826*_median(dev)
    if not np.isfinite(scale) or scale <= 1e-12:
        mean = np.mean(vals)
        var = 0.0
        for k in range(n):
            var += (vals[k]-mean)*(vals[k]-mean)
        scale = math.sqrt(max(0.0, var/max(1,n-1)))
    return max(scale,1e-12)


@njit(cache=True)
def _factor_residuals(y, m, s, day):
    n = len(y)
    mkt = np.full(n, np.nan)
    ms = np.full(n, np.nan)
    bm2 = np.full(n, np.nan)
    bs2 = np.full(n, np.nan)
    for i in range(n):
        if not (np.isfinite(y[i]) and np.isfinite(m[i])):
            continue
        start = max(0, i-15)
        n1=0; sym=0.0; smm=0.0
        n2=0; sy2m=0.0; sm2=0.0; ss2=0.0; sms=0.0; sy2s=0.0
        for j in range(start,i):
            if day[j] != day[i] or not (np.isfinite(y[j]) and np.isfinite(m[j])):
                continue
            n1 += 1; sym += y[j]*m[j]; smm += m[j]*m[j]
            if np.isfinite(s[j]):
                n2 += 1; sy2m += y[j]*m[j]; sm2 += m[j]*m[j]
                ss2 += s[j]*s[j]; sms += m[j]*s[j]; sy2s += y[j]*s[j]
        if n1 >= 6:
            vm=smm/n1; cym=sym/n1; lam=0.05*max(vm,1e-12)
            bm=cym/max(vm+lam,1e-12)
            mkt[i]=y[i]-bm*m[i]
        if n2 >= 6 and np.isfinite(s[i]):
            vm=sm2/n2; vs=ss2/n2; cms=sms/n2; cym=sy2m/n2; cys=sy2s/n2
            lam=0.05*max(vm+vs,1e-12); a=vm+lam; d=vs+lam; det=a*d-cms*cms
            if abs(det) < 1e-18:
                bm=cym/max(a,1e-12); bs=0.0
            else:
                bm=(cym*d-cys*cms)/det; bs=(cys*a-cym*cms)/det
            bm2[i]=bm; bs2[i]=bs; ms[i]=y[i]-bm*m[i]-bs*s[i]
        elif np.isfinite(mkt[i]):
            bm2[i]=bm if n1>=6 else np.nan; bs2[i]=0.0; ms[i]=mkt[i]
    return mkt, ms, bm2, bs2


@njit(cache=True)
def _z_matrix(resid, day, horizons):
    n=len(resid); k=len(horizons)
    z=np.full((n,k),np.nan); scales=np.full(n,np.nan); coh=np.full(n,np.nan)
    for i in range(n):
        sc=_prior_scale(resid,day,i); scales[i]=sc
        if not np.isfinite(sc): continue
        for q in range(k):
            h=int(horizons[q]); start=i-h+1
            if start<0 or day[start]!=day[i]: continue
            total=0.0; path=0.0; ok=True
            for j in range(start,i+1):
                if day[j]!=day[i] or not np.isfinite(resid[j]):
                    ok=False; break
                total += resid[j]; path += abs(resid[j])
            if ok:
                z[i,q]=total/(sc*math.sqrt(h))
                if h==5 and path>1e-15: coh[i]=abs(total)/path
    return z,scales,coh


@njit(cache=True)
def _z_h(resid,day,h):
    n=len(resid); out=np.full(n,np.nan)
    for i in range(n):
        sc=_prior_scale(resid,day,i)
        start=i-h+1
        if not np.isfinite(sc) or start<0 or day[start]!=day[i]: continue
        total=0.0; ok=True
        for j in range(start,i+1):
            if day[j]!=day[i] or not np.isfinite(resid[j]): ok=False; break
            total += resid[j]
        if ok: out[i]=total/(sc*math.sqrt(h))
    return out


@njit(cache=True)
def _cusum(arr,day,h):
    n=len(arr); up=np.zeros(n); dn=np.zeros(n); state=np.zeros(n)
    u=0.0; d=0.0; cur=-1
    for i in range(n):
        if day[i]!=cur: cur=day[i]; u=0.0; d=0.0
        sc=_prior_scale(arr,day,i)
        if np.isfinite(sc) and np.isfinite(arr[i]):
            z=arr[i]/sc
            u=min(h,max(0.0,u+z-CUSUM_K))
            d=min(h,max(0.0,d-z-CUSUM_K))
        up[i]=u; dn[i]=d
        if u>=h and d<h: state[i]=1
        elif d>=h and u<h: state[i]=-1
    return up,dn,state


@njit(cache=True)
def _update_hist(hist, day, metrics):
    n,m=metrics.shape
    for i in range(n):
        d=int(day[i])
        if d<0 or d>=hist.shape[0]: continue
        for j in range(m):
            v=metrics[i,j]
            if np.isfinite(v):
                a=abs(v)
                b=int(a/BIN_WIDTH)
                if b>=hist.shape[2]: b=hist.shape[2]-1
                hist[d,j,b]+=1


def _quantile_hist(counts, q=EMPIRICAL_Q):
    total=int(counts.sum())
    if total<=0: return np.nan
    target=max(1,int(math.ceil(total*q)))
    idx=int(np.searchsorted(np.cumsum(counts,dtype=np.int64),target))
    return min(BIN_MAX,(idx+0.5)*BIN_WIDTH)


def load_series(path: Path, name: str) -> pd.DataFrame:
    d=pd.read_parquet(path,columns=["timestamp","close"])
    d=d.drop_duplicates("timestamp").sort_values("timestamp")
    return d.rename(columns={"close":name})


def load_index_panel(root: Path, universe: dict):
    frames=[]
    for name, info in universe["indices"].items():
        if not info.get("available", True):
            continue
        p=root/"indices"/f"{norm(name)}.parquet"
        frames.append(load_series(p,name).set_index("timestamp"))
    panel=pd.concat(frames,axis=1).sort_index()
    panel=panel[~panel.index.duplicated(keep="last")]
    return panel


def map_sectors(root: Path, universe: dict, panel: pd.DataFrame, cal_days: list[str]):
    out=[]
    calset=set(cal_days)
    idx_names=[
        x for x, info in universe["indices"].items()
        if x!="NIFTY 50" and info.get("available", True) and x in panel.columns
    ]
    ir=np.log(panel[idx_names]/panel[idx_names].shift(1))
    ir=ir[pd.Series(ir.index.date.astype(str),index=ir.index).isin(calset)]
    for sym in sorted(universe["stocks"]):
        s=pd.read_parquet(root/"stocks"/f"{sym}.parquet",columns=["timestamp","close"])
        s=s.drop_duplicates("timestamp").set_index("timestamp").sort_index()
        # Dhan omits some no-trade cash minutes. Reindex to the NIFTY minute grid
        # and carry only the last known same-day cash price forward. This is causal
        # and makes +5/+15/+30 mean clock minutes rather than "next N prints".
        sc=s["close"].reindex(ir.index)
        daykey=pd.Series(ir.index.date.astype(str),index=ir.index)
        sc=sc.groupby(daykey).ffill()
        sr=np.log(sc/sc.groupby(daykey).shift(1))
        sr=sr[daykey.isin(calset)]
        cors={}
        for idx in idx_names:
            pair=pd.concat([sr.rename("s"),ir[idx].rename("i")],axis=1).dropna()
            cors[idx]=float(pair["s"].corr(pair["i"])) if len(pair)>=500 else np.nan
        valid={k:v for k,v in cors.items() if np.isfinite(v)}
        if not valid: raise RuntimeError(f"No sector correlation for {sym}")
        best=max(valid,key=valid.get)
        out.append({"symbol":sym,"sector_id":best,"calibration_correlation":valid[best]})
    return pd.DataFrame(out)


def prepare_symbol(root:Path,sym:str,sector:str,panel:pd.DataFrame,day_to_code:dict):
    s=pd.read_parquet(root/"stocks"/f"{sym}.parquet",
                      columns=["timestamp","open","high","low","close"])
    s=s.drop_duplicates("timestamp").sort_values("timestamp").set_index("timestamp")

    ctx=panel[["NIFTY 50",sector]].copy()
    ctx=ctx[pd.Series(ctx.index.date.astype(str),index=ctx.index).isin(day_to_code)]
    x=ctx.join(s[["open","high","low","close"]],how="left")
    x["trade_date"]=x.index.date.astype(str)
    daykey=pd.Series(x["trade_date"].to_numpy(),index=x.index)

    # Complete no-trade cash minutes using only the last known same-day price.
    x["close"]=x["close"].groupby(daykey).ffill()
    missing=x["open"].isna() & x["close"].notna()
    for col in ("open","high","low"):
        x.loc[missing,col]=x.loc[missing,"close"]
    x=x.dropna(subset=["close","NIFTY 50",sector]).copy()
    x=x.reset_index().rename(columns={"index":"timestamp"})
    x["day_code"]=x["trade_date"].map(day_to_code).astype(int)

    same=x["day_code"].eq(x["day_code"].shift())
    x["r_stock"]=np.where(same,np.log(x["close"]/x["close"].shift()),np.nan)
    x["r_market"]=np.where(same,np.log(x["NIFTY 50"]/x["NIFTY 50"].shift()),np.nan)
    x["r_sector"]=np.where(same,np.log(x[sector]/x[sector].shift()),np.nan)
    y=x["r_stock"].to_numpy(float); m=x["r_market"].to_numpy(float)
    sec=x["r_sector"].to_numpy(float); day=x["day_code"].to_numpy(np.int64)
    mkt,resid,bm,bs=_factor_residuals(y,m,sec,day)
    z,scale,coh=_z_matrix(resid,day,HORIZONS)
    raw5=_z_h(y,day,5); mkt5=_z_h(mkt,day,5)
    cup,cdn,cstate=_cusum(y,day,CUSUM_H)
    rup,rdn,rstate=_cusum(resid,day,RESID_CUSUM_H)
    absz=np.abs(z); good=np.any(np.isfinite(absz),axis=1)
    tmp=np.where(np.isfinite(absz),absz,-np.inf)
    ai=np.argmax(tmp,axis=1)
    selected=np.full(len(x),np.nan); hsel=np.full(len(x),np.nan); mx=np.full(len(x),np.nan)
    rows=np.arange(len(x))[good]
    selected[good]=z[rows,ai[good]]
    hsel[good]=HORIZONS[ai[good]]
    mx[good]=tmp[rows,ai[good]]
    direction=np.where(selected>=0,1.0,-1.0)

    # Same-day rolling realized-volatility ratio; no overnight leakage.
    sq=pd.Series(y*y,index=x.index)
    vol5=sq.groupby(x["day_code"]).rolling(5,min_periods=3).mean().reset_index(level=0,drop=True).pow(0.5)
    vol15=sq.groupby(x["day_code"]).rolling(15,min_periods=8).mean().reset_index(level=0,drop=True).pow(0.5)
    vr=(vol5/vol15).to_numpy(float)
    minute=(x["timestamp"].dt.hour*60+x["timestamp"].dt.minute-555).to_numpy(float)

    feat=pd.DataFrame({
        "timestamp":x["timestamp"].to_numpy(),"trade_date":x["trade_date"].to_numpy(),
        "day_code":day,"symbol":sym,"sector_id":sector,
        "open":x["open"].to_numpy(float),"high":x["high"].to_numpy(float),
        "low":x["low"].to_numpy(float),"close":x["close"].to_numpy(float),
        "r_stock":y,"residual":resid,"robust_sigma":scale,
        "residual_coherence":coh,"selected_horizon_min":hsel,
        "selected_z":selected,"max_abs_z":mx,"direction":direction,
        "raw_z_5m":raw5,"mkt_z_5m":mkt5,
        "cusum_up":cup,"cusum_down":cdn,"cusum_state":cstate,
        "resid_cusum_up":rup,"resid_cusum_down":rdn,"resid_cusum_state":rstate,
        "minute_of_day":minute,"recent_vol_ratio":vr,
    })
    for j,name in enumerate(H_NAMES): feat[f"z_{name}"]=z[:,j]
    return feat


def threshold_table(hist,days):
    rows=[]
    qmat=np.full((len(days),len(METRICS)),np.nan)
    for d in range(CAL_MIN_DAYS,len(days)):
        lo=max(0,d-CAL_MAX_DAYS)
        for m,name in enumerate(METRICS):
            q=_quantile_hist(hist[lo:d,m,:].sum(axis=0))
            qmat[d,m]=q
            rows.append({"trade_date":days[d],"metric":name,"q99":q,
                         "train_start":days[lo],"train_end":days[d-1],"train_days":d-lo})
    return qmat,pd.DataFrame(rows)


def episode_starts(mask,direction,day):
    prev=np.r_[False,mask[:-1]]
    prev_dir=np.r_[np.nan,direction[:-1]]
    prev_day=np.r_[-1,day[:-1]]
    return mask & ((~prev)|(prev_dir!=direction)|(prev_day!=day))


def future_metrics(feat,i,direction):
    row={}
    close=feat["close"].to_numpy(float); high=feat["high"].to_numpy(float); low=feat["low"].to_numpy(float)
    day=feat["day_code"].to_numpy(int); p0=close[i]
    for h in FORWARD_H:
        j=i+h
        row[f"signed_fwd_{h}m"]=direction*(close[j]/p0-1.0) if j<len(feat) and day[j]==day[i] else np.nan
    end=min(len(feat),i+31); idx=np.arange(i+1,end); idx=idx[day[idx]==day[i]]
    if len(idx):
        if direction>0:
            fav=np.max(high[idx]/p0-1.0); adv=np.min(low[idx]/p0-1.0)
        else:
            fav=np.max(1.0-low[idx]/p0); adv=np.min(1.0-high[idx]/p0)
        row["mfe_30m_bps"]=10000*max(0.0,fav)
        row["mae_30m_bps"]=10000*min(0.0,adv)
        for b in COST_HURDLES:
            result="NONE"
            for j in idx:
                if direction>0:
                    hit_t=(high[j]/p0-1.0)*10000>=b; hit_s=(low[j]/p0-1.0)*10000<=-b
                else:
                    hit_t=(1.0-low[j]/p0)*10000>=b; hit_s=(1.0-high[j]/p0)*10000<=-b
                if hit_t or hit_s:
                    result="STOP" if hit_s else "TARGET"; break
            row[f"fp_{int(b)}bps"]=result
    else:
        row["mfe_30m_bps"]=np.nan; row["mae_30m_bps"]=np.nan
        for b in COST_HURDLES: row[f"fp_{int(b)}bps"]="NONE"
    return row


def random_placebo(feat,i,direction,rng):
    day=int(feat["day_code"].iat[i])
    idx=np.where(feat["day_code"].to_numpy(int)==day)[0]
    idx=idx[idx+30 < len(feat)]
    idx=idx[feat["day_code"].to_numpy(int)[np.minimum(idx+30,len(feat)-1)]==day]
    if len(idx)==0: return {}
    j=int(idx[int(rng.integers(0,len(idx)))])
    p0=float(feat["close"].iat[j]); out={}
    for h in FORWARD_H:
        k=j+h
        out[f"placebo_signed_fwd_{h}m"]=direction*(float(feat["close"].iat[k])/p0-1.0) if k<len(feat) and int(feat["day_code"].iat[k])==day else np.nan
    return out


def add_events(store,variant,feat,mask,direction,rng,oos_min_day):
    day=feat["day_code"].to_numpy(int)
    mask=mask & (day>=oos_min_day) & np.isfinite(direction)
    starts=episode_starts(mask,direction,day)
    for i in np.where(starts)[0]:
        r={
            "variant":variant,"timestamp":feat["timestamp"].iat[i],
            "trade_date":feat["trade_date"].iat[i],"day_code":int(day[i]),
            "symbol":feat["symbol"].iat[i],"sector_id":feat["sector_id"].iat[i],
            "direction":int(direction[i]),"entry_close":float(feat["close"].iat[i]),
        }
        for col in FEATURE_COLS:
            r[col]=float(feat[col].iat[i]) if np.isfinite(feat[col].iat[i]) else np.nan
        r.update(future_metrics(feat,i,int(direction[i])))
        r.update(random_placebo(feat,i,int(direction[i]),rng))
        store.append(r)


def day_boot_ci(df,col,reps=2000):
    z=df[["trade_date",col]].dropna()
    if z.empty: return (np.nan,np.nan,np.nan)
    point=float(z[col].mean())
    dm=z.groupby("trade_date")[col].mean().to_numpy(float)
    if len(dm)<2: return point,np.nan,np.nan
    rng=np.random.default_rng(RNG_SEED)
    vals=np.empty(reps)
    for i in range(reps): vals[i]=np.mean(dm[rng.integers(0,len(dm),len(dm))])
    return point,float(np.quantile(vals,.025)),float(np.quantile(vals,.975))


def summarize_variant(df,minute_hits,minute_den):
    out={"events":int(len(df)),"minute_trigger_rate":float(minute_hits/minute_den) if minute_den else None}
    if df.empty: return out
    out["days"]=int(df["trade_date"].nunique())
    for h in FORWARD_H:
        col=f"signed_fwd_{h}m"; p,lo,hi=day_boot_ci(df,col)
        pv=float(df[f"placebo_signed_fwd_{h}m"].mean())
        out[f"mean_{h}m_bps"]=p*10000
        out[f"ci95_{h}m_bps"]=[lo*10000,hi*10000]
        out[f"placebo_{h}m_bps"]=pv*10000
        out[f"edge_vs_placebo_{h}m_bps"]=(p-pv)*10000
        out[f"hit_{h}m"]=float((df[col]>0).mean())
        for b in COST_HURDLES:
            out[f"p_gt_{int(b)}bps_{h}m"]=float((df[col]*10000>b).mean())
            out[f"mean_after_{int(b)}bps_{h}m"]=p*10000-b
    out["mfe30_bps"]=float(df["mfe_30m_bps"].mean())
    out["mae30_bps"]=float(df["mae_30m_bps"].mean())
    for b in COST_HURDLES:
        x=df[f"fp_{int(b)}bps"]
        resolved=x.isin(["TARGET","STOP"])
        out[f"target_first_{int(b)}bps"]=float((x[resolved]=="TARGET").mean()) if resolved.any() else None
    periods={
        "Mar-May":("2026-03-01","2026-05-31"),
        "Jun-Jul":("2026-06-01","2026-07-31"),
        "Aug-Sep":("2026-08-01","2026-09-30"),
    }
    out["subperiod_15m_bps"]={}
    for name,(a,b) in periods.items():
        q=df[(df["trade_date"]>=a)&(df["trade_date"]<=b)]["signed_fwd_15m"].dropna()
        out["subperiod_15m_bps"][name]=float(q.mean()*10000) if len(q) else None
    return out


def conditional_models(events,days):
    base=events[events["variant"]=="empirical_multiscale"].copy()
    if base.empty: return {}
    results={}
    for h in FORWARD_H:
        scored=[]
        for d in sorted(base["day_code"].unique()):
            prior=base[(base["day_code"]<d)&(base["day_code"]>=max(CAL_MIN_DAYS,d-CAL_MAX_DAYS))]
            test=base[base["day_code"]==d].copy()
            if len(prior)<500 or prior["trade_date"].nunique()<10: continue
            X=prior[FEATURE_COLS].replace([np.inf,-np.inf],np.nan)
            good=X.notna().all(axis=1)
            y=(prior.loc[good,f"signed_fwd_{h}m"]>0).astype(int)
            if good.sum()<500 or y.nunique()<2: continue
            model=LogisticRegression(C=1.0,max_iter=500,class_weight=None)
            model.fit(X.loc[good],y)
            Xt=test[FEATURE_COLS].replace([np.inf,-np.inf],np.nan)
            gt=Xt.notna().all(axis=1)
            if gt.any():
                test.loc[gt,"pred_p"]=model.predict_proba(Xt.loc[gt])[:,1]
                scored.append(test.loc[gt])
        if not scored:
            results[str(h)]={"status":"insufficient_data"}; continue
        z=pd.concat(scored,ignore_index=True).dropna(subset=["pred_p",f"signed_fwd_{h}m"])
        y=(z[f"signed_fwd_{h}m"]>0).astype(int)
        auc=float(roc_auc_score(y,z["pred_p"])) if y.nunique()>1 else np.nan
        brier=float(brier_score_loss(y,z["pred_p"]))
        z["decile"]=pd.qcut(z["pred_p"],10,labels=False,duplicates="drop")
        dec=z.groupby("decile").agg(pred_p=("pred_p","mean"),
              realized_win=(f"signed_fwd_{h}m",lambda x: float((x>0).mean())),
              mean_bps=(f"signed_fwd_{h}m",lambda x: float(x.mean()*10000)),
              n=("pred_p","size")).reset_index().to_dict("records")
        results[str(h)]={"rows":int(len(z)),"auc":auc,"brier":brier,"deciles":dec}
    return results


def main():
    ap=argparse.ArgumentParser()
    ap.add_argument("--data-root",required=True)
    ap.add_argument("--out",required=True)
    args=ap.parse_args()
    root=Path(args.data_root); out=Path(args.out); out.mkdir(parents=True,exist_ok=True)
    universe=json.loads((root/"universe.json").read_text())
    panel=load_index_panel(root,universe)
    days=sorted({str(d) for d in panel.index.date})
    days=[d for d in days if "2026-03-02"<=d<="2026-09-25"]
    if len(days)<100: raise RuntimeError(f"Too few index trading days: {len(days)}")
    day_to_code={d:i for i,d in enumerate(days)}
    sector_map=map_sectors(root,universe,panel,days[:CAL_MIN_DAYS])
    sector_map.to_csv(out/"sector_mapping.csv",index=False)
    smap=dict(zip(sector_map["symbol"],sector_map["sector_id"]))

    hist=np.zeros((len(days),len(METRICS),N_BINS),dtype=np.int32)
    row_counts=np.zeros(len(days),dtype=np.int64)

    # Pass 1: build empirical null distributions only.
    for n,sym in enumerate(sorted(universe["stocks"]),1):
        f=prepare_symbol(root,sym,smap[sym],panel,day_to_code)
        metric=np.column_stack([
            f["max_abs_z"].to_numpy(float),
            *[f[f"z_{h}"].to_numpy(float) for h in H_NAMES],
            f["raw_z_5m"].to_numpy(float),f["mkt_z_5m"].to_numpy(float)
        ])
        _update_hist(hist,f["day_code"].to_numpy(np.int64),metric)
        valid=np.isfinite(f["max_abs_z"].to_numpy(float))
        np.add.at(row_counts,f.loc[valid,"day_code"].to_numpy(int),1)
        if n%25==0: print(f"PASS1 {n}/210",flush=True)

    qmat,qt=threshold_table(hist,days)
    qt.to_csv(out/"thresholds.csv",index=False)
    np.savez_compressed(out/"null_histograms.npz",hist=hist,row_counts=row_counts)

    store=[]; rng=np.random.default_rng(RNG_SEED)
    minute_hits=defaultdict(int); minute_den=defaultdict(int)

    # Pass 2: causal OOS event extraction.
    for n,sym in enumerate(sorted(universe["stocks"]),1):
        f=prepare_symbol(root,sym,smap[sym],panel,day_to_code)
        d=f["day_code"].to_numpy(int)
        mz=f["max_abs_z"].to_numpy(float); sel=f["selected_z"].to_numpy(float)
        direction=np.sign(sel)
        oos=d>=CAL_MIN_DAYS
        finite=np.isfinite(mz)&oos

        fixed=finite&(mz>=3.188815)
        minute_hits["fixed_3188_multiscale"]+=int(fixed.sum()); minute_den["fixed_3188_multiscale"]+=int(finite.sum())
        add_events(store,"fixed_3188_multiscale",f,fixed,direction,rng,CAL_MIN_DAYS)

        q=np.array([qmat[x,MIDX["max_abs_z"]] if 0<=x<len(days) else np.nan for x in d])
        emp=finite&np.isfinite(q)&(mz>=q)
        minute_hits["empirical_multiscale"]+=int(emp.sum()); minute_den["empirical_multiscale"]+=int(finite.sum())
        add_events(store,"empirical_multiscale",f,emp,direction,rng,CAL_MIN_DAYS)

        cstate=f["cusum_state"].to_numpy(float)
        rstate=f["resid_cusum_state"].to_numpy(float)
        ec=emp&(cstate==direction)
        minute_hits["empirical_plus_stock_cusum"]+=int(ec.sum()); minute_den["empirical_plus_stock_cusum"]+=int(finite.sum())
        add_events(store,"empirical_plus_stock_cusum",f,ec,direction,rng,CAL_MIN_DAYS)
        eb=emp&(cstate==direction)&(rstate==direction)
        minute_hits["empirical_plus_both_cusum"]+=int(eb.sum()); minute_den["empirical_plus_both_cusum"]+=int(finite.sum())
        add_events(store,"empirical_plus_both_cusum",f,eb,direction,rng,CAL_MIN_DAYS)

        for metric,variant in [
            ("raw_z_5m","raw_5m_empirical"),
            ("mkt_z_5m","market_resid_5m_empirical"),
            ("z_5m","market_sector_resid_5m_empirical"),
        ]:
            vals=f[metric].to_numpy(float); dr=np.sign(vals)
            qq=np.array([qmat[x,MIDX[metric]] if 0<=x<len(days) else np.nan for x in d])
            fin=np.isfinite(vals)&oos
            mask=fin&np.isfinite(qq)&(np.abs(vals)>=qq)
            minute_hits[variant]+=int(mask.sum()); minute_den[variant]+=int(fin.sum())
            add_events(store,variant,f,mask,dr,rng,CAL_MIN_DAYS)

        for h in H_NAMES:
            metric=f"z_{h}"; variant=f"fixed_{h}_empirical"
            vals=f[metric].to_numpy(float); dr=np.sign(vals)
            qq=np.array([qmat[x,MIDX[metric]] if 0<=x<len(days) else np.nan for x in d])
            fin=np.isfinite(vals)&oos
            mask=fin&np.isfinite(qq)&(np.abs(vals)>=qq)
            minute_hits[variant]+=int(mask.sum()); minute_den[variant]+=int(fin.sum())
            add_events(store,variant,f,mask,dr,rng,CAL_MIN_DAYS)
        if n%25==0: print(f"PASS2 {n}/210 events={len(store)}",flush=True)

    events=pd.DataFrame(store)
    events.to_parquet(out/"events.parquet",index=False,compression="zstd")
    summary={
        "research_version":"V1.1-1m-EMPIRICAL-NULL",
        "production_changed":False,
        "stocks":len(universe["stocks"]),
        "context_indices_available":sum(1 for x in universe["indices"].values() if x.get("available", True)),
        "context_indices_total":len(universe["indices"]),
        "trading_days":len(days),"calibration_days":CAL_MIN_DAYS,
        "oos_days":len(days)-CAL_MIN_DAYS,
        "threshold_q99_multiscale":{
            "mean":float(qt[qt.metric=="max_abs_z"]["q99"].mean()),
            "median":float(qt[qt.metric=="max_abs_z"]["q99"].median()),
            "min":float(qt[qt.metric=="max_abs_z"]["q99"].min()),
            "max":float(qt[qt.metric=="max_abs_z"]["q99"].max()),
        },
        "variants":{},
    }
    for variant,g in events.groupby("variant"):
        summary["variants"][variant]=summarize_variant(
            g,minute_hits[variant],minute_den[variant]
        )
    summary["conditional_models"]=conditional_models(events,days)

    (out/"summary.json").write_text(json.dumps(summary,indent=2,default=str))
    lines=["# V1.1 Dhan 1-minute empirical-null replay","","**RESEARCH ONLY — production unchanged.**","",
           f"- Stocks: {summary['stocks']}",
           f"- Trading days: {summary['trading_days']} ({summary['oos_days']} OOS after {CAL_MIN_DAYS} calibration days)",
           f"- Median rolling empirical max-|Z| q99: {summary['threshold_q99_multiscale']['median']:.3f}","",
           "## Variant results","+"]

    table=["| Variant | Events | Minute trigger % | +15m bps | 95% CI | Placebo bps | +30m bps |",
           "|---|---:|---:|---:|---:|---:|---:|"]
    for v,x in sorted(summary["variants"].items()):
        ci=x.get("ci95_15m_bps",[None,None])
        table.append(f"| {v} | {x.get('events',0)} | {100*(x.get('minute_trigger_rate') or 0):.2f} | "
                     f"{x.get('mean_15m_bps',float('nan')):.2f} | "
                     f"[{ci[0]:.2f}, {ci[1]:.2f}] | "
                     f"{x.get('placebo_15m_bps',float('nan')):.2f} | "
                     f"{x.get('mean_30m_bps',float('nan')):.2f} |")
    lines.extend(table)
    (out/"report.md").write_text("\n".join(lines))
    print(json.dumps(summary,indent=2,default=str))


if __name__=="__main__":
    main()
