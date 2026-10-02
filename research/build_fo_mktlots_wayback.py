#!/usr/bin/env python3
import requests,pandas as pd,io,json
from pathlib import Path
from datetime import datetime
OUT=Path("/tmp/fo_lots"); OUT.mkdir(exist_ok=True); (OUT/"snapshots").mkdir(exist_ok=True)
S=requests.Session(); H={"User-Agent":"Mozilla/5.0"}
patterns=[
 "nseindia.com/content/fo/fo_mktlots.csv",
 "www.nseindia.com/content/fo/fo_mktlots.csv",
 "www1.nseindia.com/content/fo/fo_mktlots.csv",
]
caps=[]
for pat in patterns:
 params=[("url",pat),("from","2011"),("to","2023"),("output","json"),("filter","statuscode:200"),("fl","timestamp,original,digest"),("collapse","digest")]
 try:
  r=S.get("https://web.archive.org/cdx/search/cdx",params=params,headers=H,timeout=45);r.raise_for_status();a=r.json()
  for row in a[1:]:caps.append({"timestamp":row[0],"original":row[1],"digest":row[2]})
 except Exception as e:print("ERR",pat,repr(e))
caps=pd.DataFrame(caps).drop_duplicates(["timestamp","original"]).sort_values("timestamp") if caps else pd.DataFrame(columns=["timestamp","original","digest"])
caps.to_csv(OUT/"candidates.csv",index=False);print("candidates",len(caps))
seen=set();man=[]
for row in caps.itertuples(index=False):
 dt=datetime.strptime(str(row.timestamp)[:8],"%Y%m%d");key=(dt.year,dt.month)
 if key in seen:continue
 for u in [f"https://web.archive.org/web/{row.timestamp}id_/{row.original}",f"https://web.archive.org/web/{row.timestamp}/{row.original}"]:
  try:
   rr=S.get(u,headers=H,timeout=45);rr.raise_for_status();txt=rr.content.decode("utf-8-sig",errors="ignore")
   lines=txt.splitlines()
   # file often has a date/header preamble before CSV header. locate SYMBOL row
   start=0
   for i,l in enumerate(lines[:20]):
    if "symbol" in l.lower(): start=i;break
   d=pd.read_csv(io.StringIO("\n".join(lines[start:])))
   d.columns=[str(c).strip() for c in d.columns]
   sc=next((c for c in d.columns if "symbol" in c.lower()),None)
   if sc is None:continue
   sy=d[sc].astype(str).str.strip().str.upper();sy=sy[sy.str.match(r"^[A-Z0-9&\-]+$",na=False)]
   sy=sorted(set(sy)-{"NIFTY","BANKNIFTY","FINNIFTY","MIDCPNIFTY","NIFTYNXT50"})
   if len(sy)>=50:
    pd.DataFrame({"symbol":sy}).to_csv(OUT/"snapshots"/f"fo_{dt.date()}.csv",index=False)
    man.append({"date":str(dt.date()),"n":len(sy),"timestamp":row.timestamp,"source":row.original});seen.add(key);print("snap",dt.date(),len(sy));break
  except Exception as e:pass
pd.DataFrame(man).to_csv(OUT/"manifest.csv",index=False)
open(OUT/"meta.json","w").write(json.dumps({"candidates":len(caps),"usable_snapshots":len(man)},indent=2))
print(pd.DataFrame(man).to_string(index=False))
