#!/usr/bin/env python3
"""Research-only feature replay for underlying persistence.

Candidate gate is intentionally broad (|z5| >= 2.5).  The gate only says
"something moved"; the lab learns which pre/during-event states distinguish
mean-reverting bursts from persistent directional regimes.

No options, OI, CALL V1 or production-state inputs are used.
"""
from __future__ import annotations
import argparse, json, math
from pathlib import Path
import numpy as np
import pandas as pd

CANDIDATE_Z = 2.5
SESSION_MINUTES = 375
BASELINE_MIN_OBS = 20 * SESSION_MINUTES
BASELINE_SPAN = 20 * SESSION_MINUTES
COOLDOWN_MIN = 30
HORIZONS = [5, 15, 30, 60, 120]

def pick(cols, names):
    m={str(c).lower().replace(" ","").replace("_",""):c for c in cols}
    for n in names:
        k=n.lower().replace(" ","").replace("_","")
        if k in m: return m[k]
    return None

def load_file(path):
    raw=pd.read_parquet(path)
    if raw.empty: return None
    tcol=pick(raw.columns,["datetime","timestamp","date","time"])
    if tcol is None and isinstance(raw.index,pd.DatetimeIndex):
        raw=raw.reset_index(); tcol=raw.columns[0]
    ccol=pick(raw.columns,["close","closingprice","c"])
    ocol=pick(raw.columns,["open","o"])
    hcol=pick(raw.columns,["high","h"])
    lcol=pick(raw.columns,["low","l"])
    vcol=pick(raw.columns,["volume","vol","v"])
    if tcol is None or ccol is None: return None
    d={"ts":pd.to_datetime(raw[tcol],errors="coerce"),
       "close":pd.to_numeric(raw[ccol],errors="coerce")}
    if ocol is not None: d["open"]=pd.to_numeric(raw[ocol],errors="coerce")
    if hcol is not None: d["high"]=pd.to_numeric(raw[hcol],errors="coerce")
    if lcol is not None: d["low"]=pd.to_numeric(raw[lcol],errors="coerce")
    if vcol is not None: d["volume"]=pd.to_numeric(raw[vcol],errors="coerce")
    out=pd.DataFrame(d).dropna(subset=["ts","close"]).sort_values("ts").drop_duplicates("ts")
    try:
        if out["ts"].dt.tz is not None:
            out["ts"]=out["ts"].dt.tz_convert("Asia/Kolkata").dt.tz_localize(None)
    except Exception: pass
    out=out[(out.ts.dt.time>=pd.Timestamp("09:15").time())&(out.ts.dt.time<=pd.Timestamp("15:30").time())]
    out=out[out.close>0].reset_index(drop=True)
    return out

def efficiency(logp, n):
    net=(logp-logp.shift(n)).abs()
    path=logp.diff().abs().rolling(n,min_periods=n).sum()
    return net/path.replace(0,np.nan)

def feature_events(df, symbol, nifty):
    if len(df)<BASELINE_MIN_OBS+300:return []
    x=df.copy()
    x["date"]=x.ts.dt.date
    x["logp"]=np.log(x.close)
    x["r1"]=x.logp.diff(1)
    x["r3"]=x.logp.diff(3)
    x["r5"]=x.logp.diff(5)
    x["r10"]=x.logp.diff(10)
    x["r20"]=x.logp.diff(20)

    # Strictly lagged scaling.
    x["sig5"]=x.r5.shift(1).ewm(span=BASELINE_SPAN,min_periods=BASELINE_MIN_OBS,adjust=False).std()
    x["z5"]=x.r5/x.sig5.replace(0,np.nan)

    # Pre/during-event structure.
    x["eff5"]=efficiency(x.logp,5)
    x["eff10"]=efficiency(x.logp,10)
    x["eff20"]=efficiency(x.logp,20)
    x["rv5"]=x.r1.rolling(5,min_periods=5).std()
    x["rv20"]=x.r1.rolling(20,min_periods=20).std()
    x["rv60"]=x.r1.rolling(60,min_periods=60).std()
    x["compression20_60"]=x.rv20/x.rv60.replace(0,np.nan)
    x["accel_1_3"]=(x.r1 - x.r3/3.0)/x.sig5.replace(0,np.nan)
    x["accel_3_5"]=(x.r3/3.0 - x.r5/5.0)/x.sig5.replace(0,np.nan)
    abs1=x.r1.abs()
    x["impulse_concentration5"]=abs1.rolling(5,min_periods=5).max()/abs1.rolling(5,min_periods=5).sum().replace(0,np.nan)

    # Price location within recent ranges, if OHLC available use actual highs/lows.
    hi=x["high"] if "high" in x else x.close
    lo=x["low"] if "low" in x else x.close
    rhi=hi.rolling(20,min_periods=20).max()
    rlo=lo.rolling(20,min_periods=20).min()
    x["range_pos20"]=(x.close-rlo)/(rhi-rlo).replace(0,np.nan)
    rhi60=hi.rolling(60,min_periods=60).max()
    rlo60=lo.rolling(60,min_periods=60).min()
    x["range_pos60"]=(x.close-rlo60)/(rhi60-rlo60).replace(0,np.nan)

    # Session state.
    x["session_min"]=(x.ts.dt.hour*60+x.ts.dt.minute)-(9*60+15)
    x["from_open"]=x.groupby("date").logp.transform(lambda s:s-s.iloc[0])
    x["tod_sin"]=np.sin(2*np.pi*x.session_min/SESSION_MINUTES)
    x["tod_cos"]=np.cos(2*np.pi*x.session_min/SESSION_MINUTES)

    # Volume participation using only past/current stock volume.
    if "volume" in x:
        v=x.volume.clip(lower=0)
        x["vol5"]=v.rolling(5,min_periods=5).sum()
        # lagged 20-session-equivalent EWM moments
        vm=x.vol5.shift(1).ewm(span=BASELINE_SPAN,min_periods=BASELINE_MIN_OBS,adjust=False).mean()
        vs=x.vol5.shift(1).ewm(span=BASELINE_SPAN,min_periods=BASELINE_MIN_OBS,adjust=False).std()
        x["vol_z5"]=(x.vol5-vm)/vs.replace(0,np.nan)
        x["vol_accel"]=v.rolling(3,min_periods=3).mean()/v.shift(3).rolling(3,min_periods=3).mean().replace(0,np.nan)
    else:
        x["vol_z5"]=np.nan; x["vol_accel"]=np.nan

    # Market-relative contemporaneous state and future residual labels.
    nclose=None
    if nifty is not None:
        ns=nifty.reindex(x.ts).ffill(limit=2)
        nlog=np.log(ns)
        x["nifty_r5"]=nlog.diff(5).to_numpy()
        x["resid_r5"]=x.r5-x.nifty_r5
        nclose=ns.reset_index(drop=True)
    else:
        x["nifty_r5"]=np.nan; x["resid_r5"]=np.nan

    # Event candidates must have full 120m labels.
    cand=x[(x.session_min>=20)&(x.session_min<=SESSION_MINUTES-120)&(x.z5.abs()>=CANDIDATE_Z)]
    events=[]; last_by_day={}
    for idx in cand.index:
        ts=x.at[idx,"ts"]; day=ts.date()
        prior=last_by_day.get(day)
        if prior is not None and (ts-prior).total_seconds()<COOLDOWN_MIN*60:
            continue
        direction=1 if x.at[idx,"z5"]>0 else -1
        base=float(x.at[idx,"close"])
        rec={
          "symbol":symbol,"ts":ts.isoformat(),"date":str(day),"year":int(ts.year),
          "direction":direction,"z5_abs":abs(float(x.at[idx,"z5"])),
          "z5_signed":float(x.at[idx,"z5"]),
          "r1_dir_bps":direction*float(x.at[idx,"r1"])*10000,
          "r3_dir_bps":direction*float(x.at[idx,"r3"])*10000,
          "r5_dir_bps":direction*float(x.at[idx,"r5"])*10000,
          "r10_dir_bps":direction*float(x.at[idx,"r10"])*10000,
          "r20_dir_bps":direction*float(x.at[idx,"r20"])*10000,
          "eff5":float(x.at[idx,"eff5"]) if pd.notna(x.at[idx,"eff5"]) else np.nan,
          "eff10":float(x.at[idx,"eff10"]) if pd.notna(x.at[idx,"eff10"]) else np.nan,
          "eff20":float(x.at[idx,"eff20"]) if pd.notna(x.at[idx,"eff20"]) else np.nan,
          "compression20_60":float(x.at[idx,"compression20_60"]) if pd.notna(x.at[idx,"compression20_60"]) else np.nan,
          "accel_1_3":direction*float(x.at[idx,"accel_1_3"]) if pd.notna(x.at[idx,"accel_1_3"]) else np.nan,
          "accel_3_5":direction*float(x.at[idx,"accel_3_5"]) if pd.notna(x.at[idx,"accel_3_5"]) else np.nan,
          "impulse_concentration5":float(x.at[idx,"impulse_concentration5"]) if pd.notna(x.at[idx,"impulse_concentration5"]) else np.nan,
          "range_pos20_dir":float(x.at[idx,"range_pos20"]) if direction>0 else 1-float(x.at[idx,"range_pos20"]),
          "range_pos60_dir":float(x.at[idx,"range_pos60"]) if direction>0 else 1-float(x.at[idx,"range_pos60"]),
          "from_open_dir_bps":direction*float(x.at[idx,"from_open"])*10000,
          "session_min":int(x.at[idx,"session_min"]),
          "tod_sin":float(x.at[idx,"tod_sin"]),"tod_cos":float(x.at[idx,"tod_cos"]),
          "vol_z5":float(x.at[idx,"vol_z5"]) if pd.notna(x.at[idx,"vol_z5"]) else np.nan,
          "vol_accel":float(x.at[idx,"vol_accel"]) if pd.notna(x.at[idx,"vol_accel"]) else np.nan,
          "resid_r5_dir_bps":direction*float(x.at[idx,"resid_r5"])*10000 if pd.notna(x.at[idx,"resid_r5"]) else np.nan,
        }
        valid=True
        for h in HORIZONS:
            j=idx+h
            if j>=len(x) or x.at[j,"ts"].date()!=day:
                valid=False; break
            ret=direction*math.log(float(x.at[j,"close"])/base)*10000
            rec[f"r{h}_bps"]=ret
            if nclose is not None and j<len(nclose):
                a=nclose.iloc[idx]; b=nclose.iloc[j]
                if pd.notna(a) and pd.notna(b) and a>0 and b>0:
                    rec[f"resid{h}_bps"]=ret-direction*math.log(float(b)/float(a))*10000
        if not valid: continue
        # Pure price-behaviour targets, independent of production rules.
        rec["persist15"]=int(rec["r15_bps"]>0)
        rec["persist30"]=int(rec["r30_bps"]>0)
        rec["persist60"]=int(rec["r60_bps"]>0)
        rec["persist30_10bps"]=int(rec["r30_bps"]>=10)
        rec["persistent_regime"]=int((rec["r15_bps"]>0) and (rec["r30_bps"]>0) and (rec["r60_bps"]>0))
        events.append(rec); last_by_day[day]=ts
    return events

def main():
    ap=argparse.ArgumentParser()
    ap.add_argument("--data",required=True)
    ap.add_argument("--out",required=True)
    a=ap.parse_args()
    root=Path(a.data); out=Path(a.out); out.mkdir(parents=True,exist_ok=True)
    nifty_path=root/"NIFTY50-INDEX.parquet"
    nifty_df=load_file(nifty_path) if nifty_path.exists() else None
    nifty=nifty_df.set_index("ts")["close"].sort_index() if nifty_df is not None else None
    files=sorted(p for p in root.glob("*.parquet") if "INDEX" not in p.name.upper())
    rows=[]; coverage=[]; failures=[]
    for k,p in enumerate(files,1):
        try:
            df=load_file(p)
            if df is None or df.empty:
                failures.append({"symbol":p.stem,"reason":"empty"}); continue
            coverage.append({"symbol":p.stem,"rows":len(df),"start":str(df.ts.min()),"end":str(df.ts.max())})
            rows.extend(feature_events(df,p.stem,nifty))
        except Exception as e:
            failures.append({"symbol":p.stem,"reason":repr(e)})
        if k%10==0: print(f"processed {k}/{len(files)} rows={len(rows)}",flush=True)
    pd.DataFrame(rows).to_parquet(out/"feature_events.parquet",index=False)
    pd.DataFrame(coverage).to_csv(out/"coverage.csv",index=False)
    pd.DataFrame(failures).to_csv(out/"failures.csv",index=False)
    meta={"files_seen":len(files),"files_ok":len(coverage),"failures":len(failures),"events":len(rows),
          "candidate_gate":"abs(z5)>=2.5","cooldown_min":COOLDOWN_MIN,
          "note":"underlying-only features and price-behaviour targets; no option/OI/production-state inputs"}
    (out/"meta.json").write_text(json.dumps(meta,indent=2))
    print(json.dumps(meta,indent=2))
if __name__=="__main__": main()
