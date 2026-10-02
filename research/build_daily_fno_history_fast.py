#!/usr/bin/env python3
from __future__ import annotations
import io
import zipfile
import time
import calendar
import json
import os
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from datetime import date, timedelta
import pandas as pd
import requests

OUT=Path("/tmp/fno_history")
OUT.mkdir(parents=True,exist_ok=True)
YEAR=int(os.environ["YEAR"])

def fetch_one(d):
    switch=date(2024,7,8)
    if d < switch:
        mon=calendar.month_abbr[d.month].upper()
        dd=d.strftime("%d%b%Y").upper()
        url=f"https://archives.nseindia.com/content/historical/DERIVATIVES/{d.year}/{mon}/fo{dd}bhav.csv.zip"
    else:
        compact=d.strftime("%Y%m%d")
        url=f"https://nsearchives.nseindia.com/content/fo/BhavCopy_NSE_FO_0_0_0_{compact}_F_0000.csv.zip"
    headers={"User-Agent":"Mozilla/5.0"}
    for k in range(4):
        try:
            r=requests.get(url,headers=headers,timeout=25)
            if r.status_code==404:
                return str(d), []
            r.raise_for_status()
            z=zipfile.ZipFile(io.BytesIO(r.content))
            name=z.namelist()[0]
            df=pd.read_csv(io.BytesIO(z.read(name)))
            cols={str(c).upper().strip():c for c in df.columns}
            symc=cols.get("SYMBOL")
            instrc=cols.get("INSTRUMENT")
            if symc is None:
                return str(d), []
            if instrc is not None:
                ins=df[instrc].astype(str).str.upper()
                q=df[ins.str.startswith("FUTSTK") | ins.str.startswith("OPTSTK")]
            else:
                q=df
            syms=sorted(set(q[symc].dropna().astype(str).str.strip().str.upper()))
            return str(d), syms
        except Exception:
            if k==3:
                return str(d), []
            time.sleep(0.5*(k+1))

start=date(YEAR,1,1)
end=date(YEAR,12,31)
if YEAR==2026:
    end=date(2026,10,2)

dates=[]
d=start
while d<=end:
    if d.weekday()<5:
        dates.append(d)
    d += timedelta(days=1)

rows=[]
found=0
missing=0
with ThreadPoolExecutor(max_workers=12) as ex:
    futs=[ex.submit(fetch_one,d) for d in dates]
    for i,fut in enumerate(as_completed(futs),1):
        ds,syms=fut.result()
        if syms:
            rows.extend((ds,s) for s in syms)
            found += 1
        else:
            missing += 1
        if i%40==0:
            print("progress",YEAR,i,"/",len(dates),"found",found,"rows",len(rows),flush=True)

out=pd.DataFrame(rows,columns=["date","symbol"]).drop_duplicates().sort_values(["date","symbol"])
out.to_csv(OUT/f"fno_membership_{YEAR}.csv.gz",index=False,compression="gzip")
meta={"year":YEAR,"trading_days_found":found,"missing_or_holiday":missing,
      "rows":int(len(out)),"symbols":int(out.symbol.nunique()) if len(out) else 0,
      "start":out.date.min() if len(out) else None,"end":out.date.max() if len(out) else None}
(OUT/"meta.json").write_text(json.dumps(meta,indent=2))
print(json.dumps(meta,indent=2))
