#!/usr/bin/env python3
from pathlib import Path
import json
import pandas as pd
import numpy as np

HORIZONS=[3,5,10,15,30,45,60,90,120]
THRESHOLDS=[2.5,3.0,3.5]

def summarize_df(e):
    rows=[]
    for thr in THRESHOLDS:
        q=e[e.threshold==thr]
        for h in HORIZONS:
            col=f"r{h}_bps"; rcol=f"resid{h}_bps"
            vals=q[col].dropna()
            if vals.empty: continue
            daily=q.groupby("date")[col].mean().dropna()
            rng=np.random.default_rng(12345+int(thr*10)+h)
            if len(daily)>=20:
                arr=daily.to_numpy()
                boots=np.array([rng.choice(arr,len(arr),replace=True).mean() for _ in range(1000)])
                lo,hi=np.percentile(boots,[2.5,97.5])
            else:
                lo=hi=np.nan
            rv=q[rcol].dropna() if rcol in q else pd.Series(dtype=float)
            rows.append({
              "threshold":thr,"horizon_min":h,"events":int(len(vals)),"days":int(q.date.nunique()),
              "mean_bps":float(vals.mean()),"median_bps":float(vals.median()),
              "positive_pct":float((vals>0).mean()*100),
              "gt5bps_pct":float((vals>5).mean()*100),
              "gt10bps_pct":float((vals>10).mean()*100),
              "daily_cluster_ci95_lo_bps":float(lo) if np.isfinite(lo) else None,
              "daily_cluster_ci95_hi_bps":float(hi) if np.isfinite(hi) else None,
              "market_adjusted_mean_bps":float(rv.mean()) if len(rv) else None
            })
    return rows

root=Path("/tmp/shards")
out=Path("/tmp/merged"); out.mkdir(parents=True,exist_ok=True)
event_files=list(root.glob("**/events.parquet"))
coverage_files=list(root.glob("**/coverage.csv"))
failure_files=list(root.glob("**/failures.csv"))
events=pd.concat([pd.read_parquet(p) for p in event_files],ignore_index=True) if event_files else pd.DataFrame()
coverage=pd.concat([pd.read_csv(p) for p in coverage_files if p.stat().st_size>0],ignore_index=True) if coverage_files else pd.DataFrame()
fails=[]
for p in failure_files:
    if p.stat().st_size>0:
        try: fails.append(pd.read_csv(p))
        except Exception: pass
failures=pd.concat(fails,ignore_index=True) if fails else pd.DataFrame(columns=["symbol","reason"])

events.to_parquet(out/"events.parquet",index=False)
coverage.to_csv(out/"coverage.csv",index=False)
failures.to_csv(out/"failures.csv",index=False)
pd.DataFrame(summarize_df(events)).to_csv(out/"summary.csv",index=False)

byyear=[]
if not events.empty:
    for (thr,yr),q in events.groupby(["threshold","year"]):
        rec={"threshold":float(thr),"year":int(yr),"events":int(len(q)),
             "symbols":int(q.symbol.nunique()),"days":int(q.date.nunique())}
        for h in [5,15,30,60,120]:
            v=q[f"r{h}_bps"].dropna()
            rec[f"mean_{h}m_bps"]=float(v.mean())
            rec[f"median_{h}m_bps"]=float(v.median())
            rec[f"positive_{h}m_pct"]=float((v>0).mean()*100)
        byyear.append(rec)
pd.DataFrame(byyear).to_csv(out/"by_year.csv",index=False)

meta={
 "files_ok":int(len(coverage)),"failures":int(len(failures)),
 "events":int(len(events)),
 "symbols":int(events.symbol.nunique()) if not events.empty else 0,
 "days":int(events.date.nunique()) if not events.empty else 0,
 "date_start":str(coverage["start"].min()) if not coverage.empty else None,
 "date_end":str(coverage["end"].max()) if not coverage.empty else None,
 "method":"5m signed return / strictly-lagged 20-session EWM volatility; |z| 2.5/3.0/3.5; 30m same-stock cooldown; forward labels 3-120m; no options/OI",
 "universe_limit":"Source repository stock set, not point-in-time historical NIFTY-500 membership; survivorship bias remains."
}
(out/"meta.json").write_text(json.dumps(meta,indent=2))
print(json.dumps(meta,indent=2))
print(pd.read_csv(out/"summary.csv").to_string(index=False))
