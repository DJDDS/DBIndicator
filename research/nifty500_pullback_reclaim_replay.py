#!/usr/bin/env python3
"""Underlying-only ignition -> pullback/reclaim replay.

Decision is intentionally delayed 3 minutes after an ignition event so that
the model can observe the immediate post-ignition path without leaking any
future information beyond the decision timestamp.
"""
from __future__ import annotations
import argparse, json, math
from pathlib import Path
import numpy as np
import pandas as pd

CANDIDATE_Z=2.5
SESSION_MINUTES=375
BASELINE_MIN_OBS=20*SESSION_MINUTES
BASELINE_SPAN=20*SESSION_MINUTES
COOLDOWN_MIN=30
DECISION_DELAY=3
HORIZONS=[15,30,60]

def pick(cols,names):
    m={str(c).lower().replace(" ","").replace("_",""):c for c in cols}
    for n in names:
        k=n.lower().replace(" ","").replace("_","")
        if k in m:return m[k]
    return None

def load_file(path):
    raw=pd.read_parquet(path)
    if raw.empty:return None
    t=pick(raw.columns,["datetime","timestamp","date","time"])
    if t is None and isinstance(raw.index,pd.DatetimeIndex):
        raw=raw.reset_index(); t=raw.columns[0]
    c=pick(raw.columns,["close","closingprice","c"])
    o=pick(raw.columns,["open","o"]); h=pick(raw.columns,["high","h"])
    l=pick(raw.columns,["low","l"]); v=pick(raw.columns,["volume","vol","v"])
    if t is None or c is None:return None
    d={"ts":pd.to_datetime(raw[t],errors="coerce"),
       "close":pd.to_numeric(raw[c],errors="coerce")}
    if o is not None:d["open"]=pd.to_numeric(raw[o],errors="coerce")
    if h is not None:d["high"]=pd.to_numeric(raw[h],errors="coerce")
    if l is not None:d["low"]=pd.to_numeric(raw[l],errors="coerce")
    if v is not None:d["volume"]=pd.to_numeric(raw[v],errors="coerce")
    x=pd.DataFrame(d).dropna(subset=["ts","close"]).sort_values("ts").drop_duplicates("ts")
    try:
        if x.ts.dt.tz is not None:
            x["ts"]=x.ts.dt.tz_convert("Asia/Kolkata").dt.tz_localize(None)
    except Exception:pass
    x=x[(x.ts.dt.time>=pd.Timestamp("09:15").time())&(x.ts.dt.time<=pd.Timestamp("15:30").time())]
    x=x[x.close>0].reset_index(drop=True)
    return x

def eff(arr):
    if len(arr)<2:return np.nan
    net=abs(arr[-1]-arr[0]); path=np.abs(np.diff(arr)).sum()
    return float(net/path) if path>0 else np.nan

def build(df,symbol,nifty):
    if len(df)<BASELINE_MIN_OBS+300:return []
    x=df.copy(); x["date"]=x.ts.dt.date; x["logp"]=np.log(x.close)
    x["r1"]=x.logp.diff(); x["r3"]=x.logp.diff(3); x["r5"]=x.logp.diff(5)
    x["r10"]=x.logp.diff(10); x["r20"]=x.logp.diff(20)
    x["sig5"]=x.r5.shift(1).ewm(span=BASELINE_SPAN,min_periods=BASELINE_MIN_OBS,adjust=False).std()
    x["z5"]=x.r5/x.sig5.replace(0,np.nan)
    x["session_min"]=(x.ts.dt.hour*60+x.ts.dt.minute)-(9*60+15)
    x["from_open"]=x.groupby("date").logp.transform(lambda s:s-s.iloc[0])
    if "volume" in x:
        v=x.volume.clip(lower=0)
        x["vol5"]=v.rolling(5,min_periods=5).sum()
        vm=x.vol5.shift(1).ewm(span=BASELINE_SPAN,min_periods=BASELINE_MIN_OBS,adjust=False).mean()
        vs=x.vol5.shift(1).ewm(span=BASELINE_SPAN,min_periods=BASELINE_MIN_OBS,adjust=False).std()
        x["vol_z5"]=(x.vol5-vm)/vs.replace(0,np.nan)
    else:x["vol_z5"]=np.nan

    ns=None
    if nifty is not None:
        ns=nifty.reindex(x.ts).ffill(limit=2).reset_index(drop=True)
        nlog=np.log(ns)
        x["nifty_r5"]=nlog.diff(5).to_numpy()
        x["resid_r5"]=x.r5-x.nifty_r5
    else:x["resid_r5"]=np.nan

    latest_allowed=SESSION_MINUTES-(DECISION_DELAY+max(HORIZONS))
    cand=x[(x.session_min>=20)&(x.session_min<=latest_allowed)&(x.z5.abs()>=CANDIDATE_Z)]
    rows=[]; last_by_day={}
    for i in cand.index:
        ts=x.at[i,"ts"]; day=ts.date()
        prior=last_by_day.get(day)
        if prior is not None and (ts-prior).total_seconds()<COOLDOWN_MIN*60:continue
        d=1 if x.at[i,"z5"]>0 else -1
        j=i+DECISION_DELAY
        if j>=len(x) or x.at[j,"ts"].date()!=day:continue
        base=float(x.at[i,"close"]); decision=float(x.at[j,"close"])
        impulse=abs(float(x.at[i,"r5"]))*10000
        # path after ignition observed only through t+3
        path=x.close.iloc[i:j+1].to_numpy(float)
        signed=d*np.log(path/base)*10000
        highs=np.maximum.accumulate(signed)
        maxfav=float(np.max(signed))
        minfav=float(np.min(signed))
        close3=float(signed[-1])
        # surrender from best favorable excursion by decision time
        surrender=maxfav-close3
        pullback_ratio=surrender/max(abs(maxfav),1e-6)
        adverse_ratio=max(0.0,-minfav)/max(impulse,1e-6)
        # reclaim: decision price recovers at least 70% of max favorable excursion,
        # after having surrendered at least 20% at an intermediate minute.
        had_pullback=False
        if len(signed)>=3:
            best=-1e18
            for z in signed[1:-1]:
                best=max(best,z)
                if best>0 and (best-z)>=0.20*max(best,1e-6): had_pullback=True
        reclaim70=int(maxfav>0 and close3>=0.70*maxfav and had_pullback)
        holds_impulse=int(close3>=0)
        extends_impulse=int(close3>0.25*impulse)

        rec={
          "symbol":symbol,"ts":ts.isoformat(),"date":str(day),"year":int(ts.year),
          "direction":d,"z5_abs":abs(float(x.at[i,"z5"])),
          "impulse_bps":impulse,
          "r1_at_ignite_dir_bps":d*float(x.at[i,"r1"])*10000,
          "r3_at_ignite_dir_bps":d*float(x.at[i,"r3"])*10000,
          "r10_at_ignite_dir_bps":d*float(x.at[i,"r10"])*10000,
          "r20_at_ignite_dir_bps":d*float(x.at[i,"r20"])*10000,
          "from_open_dir_bps":d*float(x.at[i,"from_open"])*10000,
          "vol_z5_at_ignite":float(x.at[i,"vol_z5"]) if pd.notna(x.at[i,"vol_z5"]) else np.nan,
          "resid_r5_at_ignite_dir_bps":d*float(x.at[i,"resid_r5"])*10000 if pd.notna(x.at[i,"resid_r5"]) else np.nan,
          "session_min":int(x.at[i,"session_min"]),
          "post1_bps":float(signed[1]) if len(signed)>1 else np.nan,
          "post2_bps":float(signed[2]) if len(signed)>2 else np.nan,
          "post3_bps":close3,
          "post3_maxfav_bps":maxfav,
          "post3_minfav_bps":minfav,
          "post3_surrender_bps":surrender,
          "post3_pullback_ratio":pullback_ratio,
          "post3_adverse_ratio":adverse_ratio,
          "post3_efficiency":eff(np.log(path))*1.0,
          "post3_reclaim70":reclaim70,
          "post3_holds_impulse":holds_impulse,
          "post3_extends_impulse":extends_impulse,
        }
        if "volume" in x:
            pre=float(x.volume.iloc[max(0,i-2):i+1].sum())
            post=float(x.volume.iloc[i+1:j+1].sum())
            rec["post3_volume_ratio"]=post/max(pre,1.0)
        else:rec["post3_volume_ratio"]=np.nan

        valid=True
        for h in HORIZONS:
            k=j+h
            if k>=len(x) or x.at[k,"ts"].date()!=day:
                valid=False;break
            ret=d*math.log(float(x.at[k,"close"])/decision)*10000
            rec[f"fwd{h}_bps"]=ret
            if ns is not None:
                a=ns.iloc[j]; b=ns.iloc[k]
                if pd.notna(a) and pd.notna(b) and a>0 and b>0:
                    rec[f"resid_fwd{h}_bps"]=ret-d*math.log(float(b)/float(a))*10000
        if not valid:continue
        rec["persist15"]=int(rec["fwd15_bps"]>0)
        rec["persist30"]=int(rec["fwd30_bps"]>0)
        rec["persist60"]=int(rec["fwd60_bps"]>0)
        rec["persistent_regime"]=int(rec["persist15"] and rec["persist30"] and rec["persist60"])
        rec["persistent10bps30"]=int(rec["fwd30_bps"]>=10)
        rows.append(rec); last_by_day[day]=ts
    return rows

def main():
    ap=argparse.ArgumentParser(); ap.add_argument("--data",required=True); ap.add_argument("--out",required=True)
    a=ap.parse_args(); root=Path(a.data); out=Path(a.out); out.mkdir(parents=True,exist_ok=True)
    npth=root/"NIFTY50-INDEX.parquet"
    ndf=load_file(npth) if npth.exists() else None
    nifty=ndf.set_index("ts")["close"].sort_index() if ndf is not None else None
    files=sorted(p for p in root.glob("*.parquet") if "INDEX" not in p.name.upper())
    rows=[]; cov=[]; fail=[]
    for n,p in enumerate(files,1):
        try:
            df=load_file(p)
            if df is None or df.empty:fail.append({"symbol":p.stem,"reason":"empty"});continue
            cov.append({"symbol":p.stem,"rows":len(df),"start":str(df.ts.min()),"end":str(df.ts.max())})
            rows.extend(build(df,p.stem,nifty))
        except Exception as e:fail.append({"symbol":p.stem,"reason":repr(e)})
        if n%10==0:print("processed",n,"/",len(files),"events",len(rows),flush=True)
    pd.DataFrame(rows).to_parquet(out/"events.parquet",index=False)
    pd.DataFrame(cov).to_csv(out/"coverage.csv",index=False)
    pd.DataFrame(fail).to_csv(out/"failures.csv",index=False)
    meta={"files_ok":len(cov),"failures":len(fail),"events":len(rows),"decision_delay_min":DECISION_DELAY,
          "candidate_gate":"abs(z5)>=2.5","note":"underlying-only ignition/pullback/reclaim lab"}
    (out/"meta.json").write_text(json.dumps(meta,indent=2)); print(json.dumps(meta,indent=2))
if __name__=="__main__":main()
