#!/usr/bin/env python3
from __future__ import annotations
import argparse, hashlib, json, math
from pathlib import Path
import numpy as np
import pandas as pd

HORIZONS=[1,2,3,5,10,15,20,30,45,60,90,120]
CAPS={"discovery":1800,"validation":900,"test":900}
FEATURES=[]

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

def load_snapshots(pit_root):
    files=sorted(Path(pit_root).glob("snapshots/nifty500_*.csv"))
    snaps=[]
    for p in files:
        ds=p.stem.replace("nifty500_","")
        d=pd.Timestamp(ds).normalize()
        s=set(pd.read_csv(p)["symbol"].astype(str).str.strip().str.upper())
        snaps.append((d,s))
    return snaps

def strict_member_mask(dates,symbol,snaps):
    d=pd.to_datetime(dates).dt.normalize()
    mask=np.zeros(len(d),dtype=bool)
    for (d0,s0),(d1,s1) in zip(snaps[:-1],snaps[1:]):
        if symbol in s0 and symbol in s1:
            mask |= ((d>=d0)&(d<d1)).to_numpy()
    return mask

def roll(g,col,w,fn="std",minp=None):
    if minp is None:minp=w
    r=g[col].rolling(w,min_periods=minp)
    z=getattr(r,fn)()
    return z.reset_index(level=0,drop=True)

def add_features(x,market):
    x=x.copy()
    x["date"]=x.ts.dt.normalize()
    x["session_min"]=(x.ts.dt.hour*60+x.ts.dt.minute)-(9*60+15)
    x["logp"]=np.log(x.close)
    g=x.groupby("date",sort=False)
    for h in [1,2,3,5,10,15,30,60]:
        x[f"r{h}"]=g.logp.diff(h)
    # generic path/vol/range state
    for w in [5,10,20,60]:
        x[f"rv{w}"]=roll(g,"r1",w,"std")
        net=g.logp.diff(w).abs()
        path=roll(g,"r1",w,"sum").abs()  # placeholder overwritten below
        ab=x["r1"].abs()
        tmp=x.assign(_ab=ab).groupby("date",sort=False)
        den=roll(tmp,"_ab",w,"sum")
        x[f"eff{w}"]=net/den.replace(0,np.nan)
        hi_col="high" if "high" in x else "close"
        lo_col="low" if "low" in x else "close"
        hi=roll(g,hi_col,w,"max")
        lo=roll(g,lo_col,w,"min")
        x[f"range{w}"]=(hi-lo)/x.close
        x[f"pos{w}"]=(x.close-lo)/(hi-lo).replace(0,np.nan)
    x["accel_1_3"]=x.r1-x.r3/3.0
    x["accel_3_5"]=x.r3/3.0-x.r5/5.0
    x["accel_5_15"]=x.r5/5.0-x.r15/15.0
    open_log=g.logp.transform("first")
    x["from_open"]=x.logp-open_log
    prev_close=g.close.last().shift(1)
    day_gap=x.date.map(prev_close)
    x["gap"]=np.log(x.groupby("date").close.transform("first")/day_gap)
    if all(c in x for c in ["open","high","low"]):
        mx=np.maximum(x.open,x.close); mn=np.minimum(x.open,x.close)
        x["body"]=(x.close-x.open)/x.close
        x["upper_wick"]=(x.high-mx)/x.close
        x["lower_wick"]=(mn-x.low)/x.close
    else:
        x["body"]=x["upper_wick"]=x["lower_wick"]=np.nan
    if "volume" in x:
        v=x.volume.clip(lower=0)
        x["logvol"]=np.log1p(v)
        x["vol1_20"]=v/roll(g,"volume",20,"mean",5).replace(0,np.nan)
        v5=roll(g,"volume",5,"sum",3); v20=roll(g,"volume",20,"sum",5)
        x["vol5_20"]=v5/v20.replace(0,np.nan)
        tod_med=x.groupby("session_min",sort=False)["volume"].transform(
            lambda s:s.shift(1).rolling(20,min_periods=5).median())
        x["vol_tod_ratio"]=v/tod_med.replace(0,np.nan)
    else:
        x["logvol"]=x["vol1_20"]=x["vol5_20"]=x["vol_tod_ratio"]=np.nan

    # market features already indexed by timestamp
    m=market.reindex(pd.DatetimeIndex(x.ts)).reset_index(drop=True)
    for h in [1,3,5,15,30,60]:
        x[f"mkt_r{h}"]=m[f"r{h}"].to_numpy()
        x[f"resid_r{h}"]=x[f"r{h}"]-x[f"mkt_r{h}"]
    x["mkt_rv20"]=m["rv20"].to_numpy()
    x["rel_rv20"]=x.rv20/x.mkt_rv20.replace(0,np.nan)
    x["tod_sin"]=np.sin(2*np.pi*x.session_min/375)
    x["tod_cos"]=np.cos(2*np.pi*x.session_min/375)
    return x

def build_market(path):
    m=load_file(path)
    if m is None:raise RuntimeError("missing NIFTY index")
    m["date"]=m.ts.dt.normalize(); m["logp"]=np.log(m.close)
    g=m.groupby("date",sort=False)
    for h in [1,3,5,15,30,60]:
        m[f"r{h}"]=g.logp.diff(h)
    m["r1tmp"]=g.logp.diff(1)
    m["rv20"]=m.groupby("date",sort=False)["r1tmp"].rolling(20,min_periods=20).std().reset_index(level=0,drop=True)
    return m.set_index("ts")[[f"r{h}" for h in [1,3,5,15,30,60]]+["rv20"]].sort_index()

def sample_indices(x,symbol):
    # ordinary states, no ignition/event gate. Use a 5-min research grid to reduce overlap,
    # then deterministic uniform caps by chronological split.
    eligible=(x.session_min>=5)&(x.session_min<=255)&((x.session_min%5)==0)
    base=np.flatnonzero(eligible.to_numpy())
    rng=np.random.default_rng(int(hashlib.sha256(symbol.encode()).hexdigest()[:16],16)%2**32)
    out=[]
    years=x.ts.dt.year.to_numpy()
    for name,mask in [
        ("discovery",years<=2022),
        ("validation",(years>=2023)&(years<=2024)),
        ("test",years==2025),
    ]:
        idx=base[mask[base]]
        cap=CAPS[name]
        if len(idx)>cap:
            idx=np.sort(rng.choice(idx,cap,replace=False))
        out.append((name,idx))
    return out

def make_rows(x,symbol,snaps,fno_set):
    member=strict_member_mask(x.date,symbol,snaps)
    # require stable NIFTY500 membership before sampling
    x=x[member].copy()
    if len(x)<500:return pd.DataFrame()
    x=x.reset_index(drop=True)

    pieces=[]
    close=x.close.to_numpy(float)
    ts=x.ts.to_numpy(dtype="datetime64[ns]")
    day=x.date.to_numpy(dtype="datetime64[D]")
    rv=x.rv60.to_numpy(float)
    for split,idx0 in sample_indices(x,symbol):
        if len(idx0)==0:continue
        # full 120-minute path must exist continuously in same session
        good=idx0+120<len(x)
        idx=idx0[good]
        if len(idx)==0:continue
        good=(day[idx+120]==day[idx]) & (((ts[idx+120]-ts[idx]).astype("timedelta64[m]").astype(int))==120)
        idx=idx[good]
        scale=rv[idx]
        good=np.isfinite(scale)&(scale>1e-7)
        idx=idx[good]; scale=scale[good]
        if len(idx)==0:continue

        fut_ix=idx[:,None]+np.arange(1,121)[None,:]
        path=np.log(close[fut_ix]/close[idx,None])*10000.0
        pos_bad=path<=0; neg_bad=path>=0
        pos_run=np.where(pos_bad.any(1),np.argmax(pos_bad,axis=1),120)
        neg_run=np.where(neg_bad.any(1),np.argmax(neg_bad,axis=1),120)
        pos_occ=(path>0).mean(1); neg_occ=(path<0).mean(1)

        take=x.iloc[idx].copy()
        take["symbol"]=symbol; take["split"]=split
        dates=take.date.dt.strftime("%Y-%m-%d").to_numpy()
        take["is_fno"]=[(d,symbol) in fno_set for d in dates]
        take["pos_run"]=pos_run; take["neg_run"]=neg_run
        take["pos_occ120"]=pos_occ; take["neg_occ120"]=neg_occ
        take["mfe120_bps"]=np.max(path,axis=1); take["mae120_bps"]=np.min(path,axis=1)
        for h in HORIZONS:
            raw=path[:,h-1]
            take[f"fwd{h}_bps"]=raw
            take[f"fnorm{h}"]=raw/10000.0/(scale*np.sqrt(h))
        pieces.append(take)
    if not pieces:return pd.DataFrame()
    out=pd.concat(pieces,ignore_index=True)
    keep=["ts","date","symbol","split","is_fno","session_min",
          "pos_run","neg_run","pos_occ120","neg_occ120","mfe120_bps","mae120_bps"]
    state=[
      "r1","r2","r3","r5","r10","r15","r30","r60",
      "rv5","rv10","rv20","rv60","range5","range10","range20","range60",
      "pos5","pos10","pos20","pos60","eff5","eff10","eff20","eff60",
      "accel_1_3","accel_3_5","accel_5_15","from_open","gap",
      "body","upper_wick","lower_wick","logvol","vol1_20","vol5_20","vol_tod_ratio",
      "mkt_r1","mkt_r3","mkt_r5","mkt_r15","mkt_r30","mkt_r60","mkt_rv20",
      "resid_r1","resid_r3","resid_r5","resid_r15","resid_r30","resid_r60","rel_rv20",
      "tod_sin","tod_cos"
    ]
    future=[f"fwd{h}_bps" for h in HORIZONS]+[f"fnorm{h}" for h in HORIZONS]
    return out[keep+state+future]

def main():
    ap=argparse.ArgumentParser()
    ap.add_argument("--data",required=True); ap.add_argument("--pit",required=True)
    ap.add_argument("--fno",required=True); ap.add_argument("--out",required=True)
    a=ap.parse_args()
    root=Path(a.data); out=Path(a.out); out.mkdir(parents=True,exist_ok=True)
    snaps=load_snapshots(a.pit)
    fno=pd.read_csv(a.fno,usecols=["date","symbol"],dtype=str)
    fno_set=set(zip(fno.date,fno.symbol.str.upper()))
    market=build_market(root/"NIFTY50-INDEX.parquet")
    files=sorted(p for p in root.glob("*.parquet") if "INDEX" not in p.name.upper())
    panels=[]; cov=[]; failures=[]
    for n,p in enumerate(files,1):
        try:
            x=load_file(p)
            if x is None or x.empty:continue
            x=add_features(x,market)
            z=make_rows(x,p.stem.upper(),snaps,fno_set)
            if len(z):
                panels.append(z)
                cov.append({"symbol":p.stem.upper(),"rows":len(z),"start":str(z.ts.min()),"end":str(z.ts.max()),
                            "fno_rows":int(z.is_fno.sum()),"nonfno_rows":int((~z.is_fno).sum())})
        except Exception as e:
            failures.append({"symbol":p.stem,"error":repr(e)})
        if n%10==0:print("processed",n,"/",len(files),"panel_rows",sum(len(q) for q in panels),flush=True)
    panel=pd.concat(panels,ignore_index=True) if panels else pd.DataFrame()
    panel.to_parquet(out/"minute_state_panel.parquet",index=False)
    pd.DataFrame(cov).to_csv(out/"coverage.csv",index=False)
    pd.DataFrame(failures).to_csv(out/"failures.csv",index=False)
    meta={"files_seen":len(files),"symbols_ok":len(cov),"rows":len(panel),"failures":len(failures),
          "strict_pit":"symbol must appear in both bracketing archived NIFTY500 snapshots",
          "state_sampling":"ordinary states on 5-minute research grid; deterministic per-stock caps; no event/ignition gate",
          "caps":CAPS,"horizons":HORIZONS}
    (out/"meta.json").write_text(json.dumps(meta,indent=2))
    print(json.dumps(meta,indent=2))
if __name__=="__main__":main()
