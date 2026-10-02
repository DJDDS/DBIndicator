#!/usr/bin/env python3
from __future__ import annotations
import io, json, re, time
from pathlib import Path
from datetime import datetime
import pandas as pd
import requests

OUT=Path("/tmp/universe"); OUT.mkdir(parents=True,exist_ok=True)
S=requests.Session()
S.headers.update({"User-Agent":"Mozilla/5.0 DBIndicator research"})

FO_URL="https://nsearchives.nseindia.com/web/mediaattachment/2026-09/FO_Stock_Introduction_and_Exclusion_Tracker_10-09-2026_20260910125755.xlsx"

def get(url, **kw):
    for i in range(5):
        try:
            r=S.get(url,timeout=60,**kw); r.raise_for_status(); return r
        except Exception:
            if i==4: raise
            time.sleep(2+i)

# 1) official F&O tracker
r=get(FO_URL)
(OUT/"fo_tracker.xlsx").write_bytes(r.content)
xls=pd.ExcelFile(io.BytesIO(r.content))
events=[]
raws=[]
for sh in xls.sheet_names:
    d=pd.read_excel(io.BytesIO(r.content),sheet_name=sh,header=1)
    d.columns=[str(x).strip() for x in d.columns]
    d["__sheet"]=sh
    raws.append(d.copy())
    symc=find_col(d.columns,["symbol"])
    namec=find_col(d.columns,["security","company","underlying"])
    datec=find_col(d.columns,["effective date","date"])
    if symc is None or datec is None:
        print("Could not normalize sheet",sh,d.columns.tolist())
        continue
    dd=d[[symc]+([namec] if namec else [])+[datec]].copy()
    dd.columns=["symbol"]+(["company_name"] if namec else [])+["effective_date"]
    if "company_name" not in dd: dd["company_name"]=""
    # NSE tracker uses grouped rows: date appears on first row and applies to the following symbols.
    dd["effective_date"]=pd.to_datetime(dd["effective_date"],errors="coerce").ffill()
    dd["symbol"]=dd["symbol"].astype(str).str.strip().str.upper()
    dd=dd[dd.symbol.str.match(r"^[A-Z0-9&\\-]+$",na=False)]
    dd["event_type"]="intro" if "intro" in sh.lower() else "exit"
    events.append(dd[["symbol","company_name","effective_date","event_type"]])
fo_raw=pd.concat(raws,ignore_index=True) if raws else pd.DataFrame()
fo_raw.to_csv(OUT/"fo_tracker_raw.csv",index=False)
fo_events=pd.concat(events,ignore_index=True) if events else pd.DataFrame(columns=["symbol","company_name","effective_date","event_type"])
fo_events=fo_events.dropna(subset=["effective_date"]).drop_duplicates().sort_values(["symbol","effective_date","event_type"])
fo_events.to_csv(OUT/"fo_events.csv",index=False)
print("F&O sheets:",xls.sheet_names)
print("F&O event rows:",len(fo_events))
print(fo_events.head(40).to_string(index=False))

# Build membership intervals from event stream. Start is left-censored if first event is an exit.
intervals=[]
for sym,g in fo_events.groupby("symbol"):
    g=g.sort_values(["effective_date","event_type"])
    open_date=None
    company=""
    for row in g.itertuples(index=False):
        company=row.company_name or company
        if row.event_type=="intro":
            if open_date is None: open_date=row.effective_date
        else:
            if open_date is None:
                intervals.append({"symbol":sym,"company_name":company,"start_date":pd.NaT,"end_date":row.effective_date,"start_confidence":"left_censored","end_confidence":"official_exit"})
            else:
                intervals.append({"symbol":sym,"company_name":company,"start_date":open_date,"end_date":row.effective_date,"start_confidence":"official_intro","end_confidence":"official_exit"})
                open_date=None
    if open_date is not None:
        intervals.append({"symbol":sym,"company_name":company,"start_date":open_date,"end_date":pd.NaT,"start_confidence":"official_intro","end_confidence":"still_active"})
fo_intervals=pd.DataFrame(intervals)
fo_intervals.to_csv(OUT/"fo_history_normalized.csv",index=False)
print("F&O intervals:",len(fo_intervals))

# 2) Wayback historical NIFTY500 snapshots
patterns=[
 "nseindia.com/content/indices/ind_cnx500list.csv",
 "nseindia.com/content/indices/ind_nifty500list.csv",
 "niftyindices.com/IndexConstituent/ind_nifty500list.csv",
]
caps=[]
for pat in patterns:
    url="https://web.archive.org/cdx/search/cdx"
    params=[("url",pat),("from","2017"),("to","2026"),("output","json"),("filter","statuscode:200"),("fl","timestamp,original,digest,mimetype"),("collapse","digest")]
    try:
        rr=get(url,params=params)
        arr=rr.json()
        for row in arr[1:]:
            caps.append({"timestamp":row[0],"original":row[1],"digest":row[2] if len(row)>2 else ""})
    except Exception as e:
        print("CDX failed",pat,repr(e))
caps=pd.DataFrame(caps).drop_duplicates(["timestamp","original"]).sort_values("timestamp")
caps.to_csv(OUT/"wayback_candidates.csv",index=False)
print("Wayback candidates:",len(caps))

def parse_snapshot(content):
    txt=content.decode("utf-8-sig",errors="ignore")
    # Find header line containing Symbol and ISIN where possible
    lines=txt.splitlines()
    start=0
    for i,l in enumerate(lines[:20]):
        if "symbol" in l.lower():
            start=i; break
    data="\n".join(lines[start:])
    try:d=pd.read_csv(io.StringIO(data))
    except Exception:return None
    d.columns=[str(c).strip() for c in d.columns]
    sc=find_col(d.columns,["symbol"])
    if not sc:return None
    syms=d[sc].astype(str).str.strip().str.upper()
    syms=syms[syms.str.match(r"^[A-Z0-9&\-]+$",na=False)]
    if len(syms)<400:return None
    return sorted(set(syms))

snap_meta=[]
snap_dir=OUT/"snapshots"; snap_dir.mkdir(exist_ok=True)
# Keep at most one good capture per calendar quarter to avoid redundant snapshots.
seen_q=set()
for row in caps.itertuples(index=False):
    dt=datetime.strptime(str(row.timestamp)[:8],"%Y%m%d")
    q=(dt.year,(dt.month-1)//3)
    if q in seen_q: continue
    urls=[
      f"https://web.archive.org/web/{row.timestamp}id_/{row.original}",
      f"https://web.archive.org/web/{row.timestamp}/{row.original}",
    ]
    ok=False
    for u in urls:
        try:
            rr=get(u)
            syms=parse_snapshot(rr.content)
            if syms:
                fn=snap_dir/f"nifty500_{dt.date()}.csv"
                pd.DataFrame({"symbol":syms}).to_csv(fn,index=False)
                snap_meta.append({"date":str(dt.date()),"n":len(syms),"source":row.original,"timestamp":row.timestamp})
                seen_q.add(q); ok=True; break
        except Exception as e:
            last=e
    if ok: print("snapshot",dt.date(),len(syms))

pd.DataFrame(snap_meta).to_csv(OUT/"snapshot_manifest.csv",index=False)
print("Usable snapshots:",len(snap_meta))
print(pd.DataFrame(snap_meta).to_string(index=False))
(OUT/"meta.json").write_text(json.dumps({
 "fo_rows":len(norm),"wayback_candidates":len(caps),"usable_snapshots":len(snap_meta),
 "note":"Research-only universe reconstruction. Historical NIFTY500 snapshots from Wayback + official NSE F&O introduction/exclusion tracker."
},indent=2))
