#!/usr/bin/env python3
from __future__ import annotations
import io, zipfile, time, calendar, json, os
from pathlib import Path
from datetime import date, timedelta
import pandas as pd
import requests

OUT=Path("/tmp/fno_history"); OUT.mkdir(parents=True,exist_ok=True)
S=requests.Session()
S.headers.update({"User-Agent":"Mozilla/5.0"})

def fetch(d):
    mon=calendar.month_abbr[d.month].upper()
    dd=d.strftime("%d%b%Y").upper()
    url=f"https://archives.nseindia.com/content/historical/DERIVATIVES/{d.year}/{mon}/fo{dd}bhav.csv.zip"
    for k in range(4):
        try:
            r=S.get(url,timeout=30)
            if r.status_code==404: return None
            r.raise_for_status()
            z=zipfile.ZipFile(io.BytesIO(r.content))
            names=z.namelist()
            if not names: return None
            raw=z.read(names[0])
            df=pd.read_csv(io.BytesIO(raw))
            return df
        except Exception:
            if k==3: return None
            time.sleep(1+k)

start=date(2018,1,1)
end=date(2026,10,2)
rows=[]; days=0; misses=0
d=start
while d<=end:
    if d.weekday()<5:
        df=fetch(d)
        if df is not None and not df.empty:
            cols={c.upper().strip():c for c in df.columns}
            symc=cols.get("SYMBOL")
            instrc=cols.get("INSTRUMENT")
            if symc:
                if instrc:
                    q=df[df[instrc].astype(str).str.upper().str.startswith(("FUTSTK","OPTSTK"))]
                else:
                    q=df
                syms=sorted(set(q[symc].dropna().astype(str).str.strip().str.upper()))
                for s in syms:
                    rows.append((str(d),s))
                days+=1
        else:
            misses+=1
    d += timedelta(days=1)
    if (d-start).days % 120 == 0:
        print("progress",d,"days",days,"rows",len(rows),flush=True)

out=pd.DataFrame(rows,columns=["date","symbol"])
out.to_csv(OUT/f"fno_membership_{year}.csv.gz",index=False,compression="gzip")
meta={"start":str(start),"end":str(end),"trading_days_found":days,"misses_or_holidays":misses,
      "rows":len(out),"symbols":int(out.symbol.nunique()) if len(out) else 0}
(OUT/"meta.json").write_text(json.dumps(meta,indent=2))
print(json.dumps(meta,indent=2))
