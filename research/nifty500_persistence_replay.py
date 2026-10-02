#!/usr/bin/env python3
"""Research-only NIFTY-500 underlying persistence replay.

No option data, no OI gate, no production dependencies.
Processes public 1-minute parquet files stock-by-stock.
"""
from __future__ import annotations
import argparse, json, math, os, re
from pathlib import Path
import numpy as np
import pandas as pd

HORIZONS = [3,5,10,15,30,45,60,90,120]
THRESHOLDS = [2.5,3.0,3.5]
SESSION_MINUTES = 375
BASELINE_MIN_OBS = 20 * SESSION_MINUTES
BASELINE_SPAN = 20 * SESSION_MINUTES
COOLDOWN_MIN = 30

def _pick(cols, names):
    m={str(c).lower().replace(" ","").replace("_",""):c for c in cols}
    for n in names:
        k=n.lower().replace(" ","").replace("_","")
        if k in m: return m[k]
    return None

def load_file(path):
    df=pd.read_parquet(path)
    if df.empty: return None
    tcol=_pick(df.columns,["datetime","timestamp","date","time"])
    ccol=_pick(df.columns,["close","closingprice","c"])
    if tcol is None:
        if isinstance(df.index,pd.DatetimeIndex):
            df=df.reset_index()
            tcol=df.columns[0]
        else: return None
    if ccol is None: return None
    out=pd.DataFrame({"ts":pd.to_datetime(df[tcol],errors="coerce"),"close":pd.to_numeric(df[ccol],errors="coerce")})
    out=out.dropna().sort_values("ts").drop_duplicates("ts")
    if out.empty:return None
    try:
        if out["ts"].dt.tz is not None:
            out["ts"]=out["ts"].dt.tz_convert("Asia/Kolkata").dt.tz_localize(None)
    except Exception: pass
    out=out[(out.ts.dt.time>=pd.Timestamp("09:15").time())&(out.ts.dt.time<=pd.Timestamp("15:30").time())]
    out=out[(out.close>0)]
    return out.reset_index(drop=True)

def load_nifty(path):
    df=load_file(path)
    if df is None:return None
    return df.set_index("ts")["close"].sort_index()

def detect_events(df,symbol,nifty):
    if len(df)<BASELINE_MIN_OBS+200:return []
    x=df.copy()
    x["date"]=x.ts.dt.date
    x["logp"]=np.log(x.close)
    x["r5"]=x.logp.diff(5)
    # strictly lagged robust volatility proxy via EWM std; current r5 is excluded
    x["sig5"]=x.r5.shift(1).ewm(span=BASELINE_SPAN,min_periods=BASELINE_MIN_OBS,adjust=False).std()
    x["z5"]=x.r5/x.sig5.replace(0,np.nan)
    # ignore first 5m and last 120m so every event has full forward labels
    mins=(x.ts.dt.hour*60+x.ts.dt.minute)-(9*60+15)
    x=x[(mins>=5)&(mins<=SESSION_MINUTES-max(HORIZONS))]
    if x.empty:return []
    # align NIFTY close for market-adjusted labels
    nclose=None
    if nifty is not None:
        nclose=nifty.reindex(df.ts).ffill(limit=2)
    events=[]
    for thr in THRESHOLDS:
        cand=x.index[x.z5.abs()>=thr].tolist()
        last_by_day={}
        for idx in cand:
            ts=df.at[idx,"ts"]; day=ts.date()
            prior=last_by_day.get(day)
            if prior is not None and (ts-prior).total_seconds()<COOLDOWN_MIN*60:
                continue
            direction=1 if x.at[idx,"z5"]>0 else -1
            base=float(df.at[idx,"close"])
            row={"symbol":symbol,"ts":ts.isoformat(),"date":str(day),"year":ts.year,
                 "threshold":thr,"z5":float(x.at[idx,"z5"]),"direction":direction,"base":base}
            valid=True
            mfe=-1e9; mae=1e9
            for h in HORIZONS:
                j=idx+h
                if j>=len(df) or df.at[j,"ts"].date()!=day:
                    valid=False; break
                sr=direction*math.log(float(df.at[j,"close"])/base)
                row[f"r{h}_bps"]=sr*10000
                if nclose is not None and idx < len(nclose) and j < len(nclose):
                    a=nclose.iloc[idx]; b=nclose.iloc[j]
                    if pd.notna(a) and pd.notna(b) and a>0 and b>0:
                        nr=direction*math.log(float(b)/float(a))*10000
                        row[f"resid{h}_bps"]=sr*10000-nr
                seg=df.close.iloc[idx+1:j+1].to_numpy(dtype=float)
                if len(seg):
                    paths=direction*np.log(seg/base)*10000
                    mfe=max(mfe,float(np.nanmax(paths)))
                    mae=min(mae,float(np.nanmin(paths)))
            if not valid: continue
            row["mfe120_bps"]=mfe if mfe>-1e8 else None
            row["mae120_bps"]=mae if mae<1e8 else None
            events.append(row)
            last_by_day[day]=ts
    return events

def summarize(events):
    if not events:return []
    e=pd.DataFrame(events)
    rows=[]
    for thr in THRESHOLDS:
        q=e[e.threshold==thr]
        for h in HORIZONS:
            col=f"r{h}_bps"; rcol=f"resid{h}_bps"
            vals=q[col].dropna()
            if vals.empty:continue
            # cluster by trading day: CI over daily mean outcomes
            daily=q.groupby("date")[col].mean().dropna()
            rng=np.random.default_rng(12345+int(thr*10)+h)
            if len(daily)>=20:
                arr=daily.to_numpy()
                boots=np.array([rng.choice(arr,len(arr),replace=True).mean() for _ in range(1000)])
                lo,hi=np.percentile(boots,[2.5,97.5])
            else: lo=hi=np.nan
            rv=q[rcol].dropna() if rcol in q else pd.Series(dtype=float)
            rows.append({
                "threshold":thr,"horizon_min":h,"events":int(len(vals)),"days":int(q.date.nunique()),
                "mean_bps":float(vals.mean()),"median_bps":float(vals.median()),
                "positive_pct":float((vals>0).mean()*100),"gt5bps_pct":float((vals>5).mean()*100),
                "gt10bps_pct":float((vals>10).mean()*100),
                "daily_cluster_ci95_lo_bps":float(lo) if np.isfinite(lo) else None,
                "daily_cluster_ci95_hi_bps":float(hi) if np.isfinite(hi) else None,
                "market_adjusted_mean_bps":float(rv.mean()) if len(rv) else None
            })
    return rows

def main():
    ap=argparse.ArgumentParser()
    ap.add_argument("--data",required=True)
    ap.add_argument("--out",required=True)
    a=ap.parse_args()
    root=Path(a.data); out=Path(a.out); out.mkdir(parents=True,exist_ok=True)
    nifty_path=root/"NIFTY50-INDEX.parquet"
    nifty=load_nifty(nifty_path) if nifty_path.exists() else None
    files=sorted(p for p in root.glob("*.parquet") if "INDEX" not in p.name.upper())
    all_events=[]; coverage=[]; failures=[]
    for k,p in enumerate(files,1):
        sym=p.stem
        try:
            df=load_file(p)
            if df is None or df.empty:
                failures.append({"symbol":sym,"reason":"unreadable_or_empty"}); continue
            coverage.append({"symbol":sym,"rows":len(df),"start":str(df.ts.min()),"end":str(df.ts.max()),
                             "days":int(df.ts.dt.date.nunique())})
            all_events.extend(detect_events(df,sym,nifty))
        except Exception as exc:
            failures.append({"symbol":sym,"reason":repr(exc)})
        if k%25==0: print(f"processed {k}/{len(files)} files; events={len(all_events)}",flush=True)
    ev=pd.DataFrame(all_events)
    ev.to_parquet(out/"events.parquet",index=False)
    pd.DataFrame(coverage).to_csv(out/"coverage.csv",index=False)
    pd.DataFrame(failures).to_csv(out/"failures.csv",index=False)
    summary=summarize(all_events)
    pd.DataFrame(summary).to_csv(out/"summary.csv",index=False)
    byyear=[]
    if not ev.empty:
        for (thr,yr),q in ev.groupby(["threshold","year"]):
            rec={"threshold":thr,"year":yr,"events":len(q),"symbols":q.symbol.nunique(),"days":q.date.nunique()}
            for h in [5,15,30,60,120]:
                v=q[f"r{h}_bps"].dropna()
                rec[f"mean_{h}m_bps"]=v.mean()
                rec[f"positive_{h}m_pct"]=(v>0).mean()*100
            byyear.append(rec)
    pd.DataFrame(byyear).to_csv(out/"by_year.csv",index=False)
    meta={"files_seen":len(files),"files_ok":len(coverage),"failures":len(failures),"events":len(all_events),
          "date_start":min((x["start"] for x in coverage),default=None),
          "date_end":max((x["end"] for x in coverage),default=None),
          "method":"5m signed return / strictly-lagged 20-session EWM volatility; |z| thresholds 2.5/3/3.5; 30m same-stock cooldown; forward labels 3-120m; no option/OI filters",
          "warning":"Universe is the source repository's NIFTY-500-style stock set, not point-in-time historical membership. Treat survivorship as a limitation."}
    (out/"meta.json").write_text(json.dumps(meta,indent=2),encoding="utf-8")
    print(json.dumps(meta,indent=2))
if __name__=="__main__": main()
