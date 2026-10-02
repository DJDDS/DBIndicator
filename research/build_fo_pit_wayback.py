#!/usr/bin/env python3
import requests, pandas as pd, io, os, json, time
from pathlib import Path
from datetime import datetime
OUT=Path("/tmp/fo_pit"); OUT.mkdir(exist_ok=True); (OUT/"snapshots").mkdir(exist_ok=True)
S=requests.Session(); H={"User-Agent":"Mozilla/5.0"}
patterns=[
 "nseindia.com/content/fo/sos_scheme.xls",
 "www.nseindia.com/content/fo/sos_scheme.xls",
 "www1.nseindia.com/content/fo/sos_scheme.xls",
]
caps=[]
for pat in patterns:
    params=[("url",pat),("from","2011"),("to","2023"),("output","json"),("filter","statuscode:200"),("fl","timestamp,original,digest"),("collapse","digest")]
    try:
        r=S.get("https://web.archive.org/cdx/search/cdx",params=params,headers=H,timeout=45); r.raise_for_status(); arr=r.json()
        for row in arr[1:]: caps.append({"timestamp":row[0],"original":row[1],"digest":row[2]})
    except Exception as e: print("CDXERR",pat,repr(e))
caps=pd.DataFrame(caps).drop_duplicates(["timestamp","original"]).sort_values("timestamp") if caps else pd.DataFrame(columns=["timestamp","original","digest"])
caps.to_csv(OUT/"candidates.csv",index=False)
print("candidates",len(caps))
manifest=[]; seen_month=set()
for row in caps.itertuples(index=False):
    dt=datetime.strptime(str(row.timestamp)[:8],"%Y%m%d"); key=(dt.year,dt.month)
    if key in seen_month: continue
    for u in [f"https://web.archive.org/web/{row.timestamp}id_/{row.original}",f"https://web.archive.org/web/{row.timestamp}/{row.original}"]:
        try:
            rr=S.get(u,headers=H,timeout=45); rr.raise_for_status()
            content=rr.content
            # parse old XLS; header can vary
            xls=pd.ExcelFile(io.BytesIO(content))
            best=None
            for sh in xls.sheet_names:
                for hdr in [0,1,2,3,4]:
                    try:
                        d=pd.read_excel(io.BytesIO(content),sheet_name=sh,header=hdr)
                        cols=[str(c).strip().lower() for c in d.columns]
                        for i,c in enumerate(cols):
                            if "symbol" in c:
                                sy=d.iloc[:,i].astype(str).str.strip().str.upper()
                                sy=sy[sy.str.match(r"^[A-Z0-9&\-]+$",na=False)]
                                if len(set(sy))>=20:
                                    best=sorted(set(sy)); break
                        if best: break
                    except Exception: pass
                if best: break
            if best:
                pd.DataFrame({"symbol":best}).to_csv(OUT/"snapshots"/f"fo_{dt.date()}.csv",index=False)
                manifest.append({"date":str(dt.date()),"n":len(best),"timestamp":row.timestamp,"source":row.original})
                seen_month.add(key); print("snap",dt.date(),len(best)); break
        except Exception as e:
            last=e
pd.DataFrame(manifest).to_csv(OUT/"manifest.csv",index=False)
open(OUT/"meta.json","w").write(json.dumps({"candidates":len(caps),"usable_snapshots":len(manifest)},indent=2))
print(pd.DataFrame(manifest).to_string(index=False))
