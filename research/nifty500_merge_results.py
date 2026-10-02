#!/usr/bin/env python3
from pathlib import Path
import json
import pandas as pd
import numpy as np
from research.nifty500_persistence_replay import summarize

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
pd.DataFrame(summarize(events.to_dict("records"))).to_csv(out/"summary.csv",index=False)

byyear=[]
if not events.empty:
    for (thr,yr),q in events.groupby(["threshold","year"]):
        rec={"threshold":thr,"year":int(yr),"events":len(q),"symbols":q.symbol.nunique(),"days":q.date.nunique()}
        for h in [5,15,30,60,120]:
            v=q[f"r{h}_bps"].dropna()
            rec[f"mean_{h}m_bps"]=float(v.mean())
            rec[f"median_{h}m_bps"]=float(v.median())
            rec[f"positive_{h}m_pct"]=float((v>0).mean()*100)
        byyear.append(rec)
pd.DataFrame(byyear).to_csv(out/"by_year.csv",index=False)

meta={
 "files_ok":int(len(coverage)),
 "failures":int(len(failures)),
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
