"""Research-only direction formula discovery on V12.4 order-flow minutes.

Reads v124_orderflow_minutes.jsonl.  Does not touch production state.

Primary target is frozen to the recorder's pre-registered horizon:
features at minute t -> enter at close(t+1) -> exit at close(t+5).
Discovery uses leave-one-session-out (LOSO) so a 3-4 day sample cannot win by
memorising one session.  The output is an explicit sparse logistic equation
plus hand-built quant challengers.  Any winner must be frozen before testing
on another dataset.
"""
from __future__ import annotations
import argparse, json, math
from pathlib import Path
import numpy as np
import pandas as pd
from sklearn.linear_model import LogisticRegression
from sklearn.preprocessing import RobustScaler
from sklearn.metrics import accuracy_score, balanced_accuracy_score, roc_auc_score

CORE = [
    "ofi_norm","l1_imb","l5_imb","l5_imb_avg","tot_imb","delta_ratio",
    "micro_bps","spread_bps","volume","ticks",
]

def _safe_z(s: pd.Series, win=30):
    med=s.rolling(win,min_periods=10).median()
    mad=(s-med).abs().rolling(win,min_periods=10).median()
    return (s-med)/(1.4826*mad.replace(0,np.nan))

def load(path: str) -> pd.DataFrame:
    rows=[json.loads(x) for x in open(path,encoding="utf-8") if x.strip()]
    d=pd.DataFrame(rows)
    d["minute"]=pd.to_datetime(d["minute"])
    d["day"]=d["minute"].dt.date.astype(str)
    d=d.sort_values(["symbol","minute"]).reset_index(drop=True)
    for c in CORE+["open","high","low","close","buy_vol","sell_vol","unk_vol","ofi"]:
        if c in d: d[c]=pd.to_numeric(d[c],errors="coerce")
    return d

def engineer(d: pd.DataFrame) -> pd.DataFrame:
    out=[]
    for (_,day),g in d.groupby(["symbol","day"],sort=False):
        g=g.sort_values("minute").copy()
        close=g["close"]
        r1=1e4*close.pct_change()
        sigma=r1.abs().rolling(30,min_periods=10).median()

        # Exact recorder-native witnesses and causal transforms.
        for c in ["ofi_norm","l1_imb","l5_imb","l5_imb_avg","tot_imb","delta_ratio","micro_bps"]:
            if c in g:
                g[c+"_z"]=_safe_z(g[c])
                g[c+"_m3"]=g[c].rolling(3,min_periods=2).mean()
                g[c+"_m5"]=g[c].rolling(5,min_periods=3).mean()
                g[c+"_d3"]=g[c]-g[c].shift(3)

        g["ret1_bps"]=r1
        g["ret3_bps"]=1e4*(close/close.shift(3)-1)
        g["flow_accel"]=g.get("ofi_norm",np.nan)-g.get("ofi_norm",pd.Series(index=g.index,dtype=float)).shift(3)
        g["book_trade_agree"]=g.get("delta_ratio",0)*g.get("l5_imb_avg",0)
        g["micro_book_agree"]=g.get("micro_bps",0)*g.get("l1_imb",0)
        g["absorption"]=g.get("delta_ratio",np.nan)-r1/sigma.replace(0,np.nan)
        g["book_vs_trade"]=g.get("delta_ratio",np.nan)-g.get("l5_imb_avg",np.nan)

        spread_med=g.get("spread_bps",pd.Series(index=g.index,dtype=float)).rolling(30,min_periods=10).median()
        g["spread_stress"]=g.get("spread_bps",np.nan)/spread_med.replace(0,np.nan)
        if "volume" in g:
            vm=g["volume"].rolling(30,min_periods=10).median()
            g["vol_burst"]=g["volume"]/vm.replace(0,np.nan)
        else:
            g["vol_burst"]=np.nan

        # Directional pressure: flow should agree across independent witnesses.
        zcols=[x for x in ["ofi_norm_z","delta_ratio_z","l5_imb_avg_z","micro_bps_z","tot_imb_z"] if x in g]
        if zcols:
            g["pressure_mean"]=g[zcols].mean(axis=1)
            signs=np.sign(g[zcols])
            g["pressure_agreement"]=signs.mean(axis=1)
        else:
            g["pressure_mean"]=np.nan; g["pressure_agreement"]=np.nan
        g["liq_pressure"]=g["pressure_mean"]/(1+g["spread_stress"].clip(lower=0).fillna(1))

        # Frozen no-leakage target inherited from orderflow_week_eval.py.
        full=g.set_index("minute").reindex(pd.date_range(g.minute.min(),g.minute.max(),freq="1min"))
        c=full["close"]
        raw=1e4*(c.shift(-5)/c.shift(-1)-1)
        g["raw_fwd_bps"]=raw.reindex(g["minute"]).to_numpy()

        out.append(g)
    x=pd.concat(out,ignore_index=True)
    # market-neutral target, but keep raw sign diagnostics too.
    x["mkt_fwd_bps"]=x.groupby("minute")["raw_fwd_bps"].transform("mean")
    x["fwd_bps"]=x["raw_fwd_bps"]-x["mkt_fwd_bps"]
    x["y"]=(x["fwd_bps"]>0).astype(int)
    x["raw_y"]=(x["raw_fwd_bps"]>0).astype(int)
    return x

FEATURES=[
    "ofi_norm","l1_imb","l5_imb","l5_imb_avg","tot_imb","delta_ratio","micro_bps","spread_bps",
    "ofi_norm_m3","ofi_norm_m5","ofi_norm_d3","delta_ratio_m3","delta_ratio_d3",
    "l5_imb_avg_m3","l5_imb_avg_d3","micro_bps_m3","micro_bps_d3",
    "ret1_bps","ret3_bps","flow_accel","book_trade_agree","micro_book_agree",
    "absorption","book_vs_trade","spread_stress","vol_burst","pressure_mean",
    "pressure_agreement","liq_pressure",
]

def metric(y,p):
    pred=(p>=.5).astype(int)
    acc=accuracy_score(y,pred)
    bal=balanced_accuracy_score(y,pred)
    auc=roc_auc_score(y,p) if len(np.unique(y))==2 else np.nan
    return dict(n=int(len(y)),acc=float(acc),bal_acc=float(bal),auc=float(auc) if np.isfinite(auc) else None)

def fit_sparse_loso(d: pd.DataFrame):
    feats=[f for f in FEATURES if f in d.columns]
    work=d.dropna(subset=["fwd_bps"]).copy()
    X=work[feats].replace([np.inf,-np.inf],np.nan)
    med=X.median(); X=X.fillna(med).fillna(0)
    y=work["y"].to_numpy()
    days=sorted(work["day"].unique())
    results=[]
    for C in [0.01,0.02,0.04,0.07,0.1,0.2,0.4]:
        folds=[]
        for hold in days:
            tr=(work.day!=hold).to_numpy(); te=(work.day==hold).to_numpy()
            if tr.sum()<100 or te.sum()<20 or len(np.unique(y[tr]))<2: continue
            sc=RobustScaler().fit(X.loc[tr])
            Xt=sc.transform(X.loc[tr]); Xv=sc.transform(X.loc[te])
            m=LogisticRegression(penalty="l1",solver="liblinear",C=C,class_weight="balanced",max_iter=3000,random_state=124)
            m.fit(Xt,y[tr]); p=m.predict_proba(Xv)[:,1]
            mm=metric(y[te],p); mm["day"]=hold; folds.append(mm)
        if not folds: continue
        minacc=min(f["acc"] for f in folds)
        meanacc=float(np.mean([f["acc"] for f in folds]))
        meanbal=float(np.mean([f["bal_acc"] for f in folds]))
        results.append((minacc,meanbal,meanacc,C,folds))
    results.sort(reverse=True,key=lambda t:(t[0],t[1],t[2]))
    best=results[0]

    # Refit winner on all discovery sessions and expose exact formula.
    sc=RobustScaler().fit(X)
    Z=sc.transform(X)
    m=LogisticRegression(penalty="l1",solver="liblinear",C=best[3],class_weight="balanced",max_iter=3000,random_state=124)
    m.fit(Z,y)
    nz=np.flatnonzero(np.abs(m.coef_[0])>1e-10)
    terms=[]
    for j in nz:
        terms.append({
            "feature":feats[j],
            "weight_on_robust_scaled_feature":float(m.coef_[0,j]),
            "center":float(sc.center_[j]),
            "scale":float(sc.scale_[j]) if sc.scale_[j]!=0 else 1.0,
        })
    return {
        "method":"LOSO sparse logistic quant equation",
        "primary_target":"market-neutral sign, close(t+1)->close(t+5)",
        "sessions":days,
        "C":best[3],
        "loso_folds":best[4],
        "loso_min_accuracy":best[0],
        "loso_mean_balanced_accuracy":best[1],
        "loso_mean_accuracy":best[2],
        "intercept":float(m.intercept_[0]),
        "terms":terms,
    }

def hand_formula_tournament(d: pd.DataFrame):
    # Economically interpretable challengers; no future variables.
    formulas={
      "Q1_pressure":"pressure_mean",
      "Q2_liq_pressure":"liq_pressure",
      "Q3_ofi_trade_book":"0.45*ofi_norm_z + 0.30*delta_ratio_z + 0.25*l5_imb_avg_z",
      "Q4_micro_flow":"0.40*micro_bps_z + 0.35*ofi_norm_z + 0.25*delta_ratio_z",
      "Q5_persistence":"0.35*ofi_norm_m3 + 0.25*delta_ratio_m3 + 0.20*l5_imb_avg_m3 + 0.20*micro_bps_m3",
      "Q6_acceleration":"0.35*ofi_norm_d3 + 0.25*delta_ratio_d3 + 0.20*l5_imb_avg_d3 + 0.20*micro_bps_d3",
    }
    rows=[]
    for name,expr in formulas.items():
        try: s=d.eval(expr)
        except Exception: continue
        q=d[["day","y"]].copy();q["s"]=s
        q=q.replace([np.inf,-np.inf],np.nan).dropna()
        folds=[]
        for day,g in q.groupby("day"):
            if len(g)<20: continue
            pred=(g.s>=0).astype(int)
            folds.append({"day":day,"n":len(g),"acc":float((pred==g.y).mean())})
        if folds:
            rows.append({
              "name":name,"formula":expr,
              "loso_min_accuracy":min(x["acc"] for x in folds),
              "loso_mean_accuracy":float(np.mean([x["acc"] for x in folds])),
              "folds":folds,
            })
    rows.sort(key=lambda x:(x["loso_min_accuracy"],x["loso_mean_accuracy"]),reverse=True)
    return rows

def main():
    ap=argparse.ArgumentParser()
    ap.add_argument("jsonl")
    ap.add_argument("--out",default="orderflow_direction_discovery")
    args=ap.parse_args()
    out=Path(args.out);out.mkdir(parents=True,exist_ok=True)
    d=engineer(load(args.jsonl))
    sparse=fit_sparse_loso(d)
    hand=hand_formula_tournament(d)
    report={
      "status":"DISCOVERY_ONLY_DO_NOT_DEPLOY",
      "rows":int(len(d)),"sessions":sorted(d.day.unique().tolist()),
      "symbols":int(d.symbol.nunique()),
      "sparse_winner":sparse,
      "hand_formula_tournament":hand[:10],
      "freeze_rule":"Freeze winner before evaluating any later/independent dataset. No retuning on validation data.",
    }
    (out/"orderflow_direction_formula.json").write_text(json.dumps(report,indent=2),encoding="utf-8")
    d.to_csv(out/"orderflow_direction_features.csv.gz",index=False,compression="gzip")
    print(json.dumps(report,indent=2))

if __name__=="__main__":
    main()
