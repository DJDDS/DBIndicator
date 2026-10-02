#!/usr/bin/env python3
"""Clean-slate combinatorial persistence search.

Uses already-created underlying-only event data. No option/OI/production features.
Training: 2018-2022; validation: 2023-2024; final untouched test: 2025.
Search:
  * all single-feature Bayesian probability tables
  * all pairwise combinations
  * all 3-way combinations among validation-selected diverse features
  * max-statistic shuffled-label controls
Probabilities use beta-binomial smoothing; bins are learned on train only.
"""
from pathlib import Path
from itertools import combinations
import json, math
import numpy as np
import pandas as pd
from sklearn.metrics import roc_auc_score, brier_score_loss, log_loss
from sklearn.feature_selection import mutual_info_classif

ROOT=Path("/tmp/shards")
OUT=Path("/tmp/final"); OUT.mkdir(parents=True,exist_ok=True)
TARGETS=["persist15","persist30","persist60","persistent_regime","persistent10bps30"]
EXCLUDE={
 "year","direction","persist15","persist30","persist60","persistent_regime","persistent10bps30",
 "fwd15_bps","fwd30_bps","fwd60_bps","resid_fwd15_bps","resid_fwd30_bps","resid_fwd60_bps"
}
N_BINS=5
ALPHA=8.0
PERMUTATIONS=80
TOP_FOR_TRIPLES=12
MIN_CELL=40

files=list(ROOT.glob("**/events.parquet"))
if not files: raise SystemExit("No event shards found")
df=pd.concat([pd.read_parquet(p) for p in files],ignore_index=True)
df["date"]=pd.to_datetime(df["date"])
df=df.sort_values(["date","symbol","ts"]).reset_index(drop=True)

# Candidate numeric features are discovered from the dataset itself.
features=[]
for c in df.columns:
    if c in EXCLUDE or c in {"symbol","ts","date"}: continue
    if pd.api.types.is_numeric_dtype(df[c]) and df[c].nunique(dropna=True)>3:
        features.append(c)

# Full-session chronological split + one whole trading session embargo.
dates=pd.DatetimeIndex(sorted(df.date.dt.normalize().unique()))
train_dates=dates[dates.year<=2022][:-1]
val_dates=dates[(dates.year>=2023)&(dates.year<=2024)][1:-1]
test_dates=dates[dates.year==2025][1:]
train=df[df.date.dt.normalize().isin(train_dates)].copy()
val=df[df.date.dt.normalize().isin(val_dates)].copy()
test=df[df.date.dt.normalize().isin(test_dates)].copy()

# Deterministic training cap only for runtime/memory. Validation/test remain complete.
MAX_TRAIN=650_000
if len(train)>MAX_TRAIN:
    rng=np.random.default_rng(20261002)
    train=train.iloc[np.sort(rng.choice(len(train),MAX_TRAIN,replace=False))].copy()

def fit_edges(series, n_bins=N_BINS):
    x=pd.to_numeric(series,errors="coerce").replace([np.inf,-np.inf],np.nan).dropna().to_numpy()
    if len(x)<100:return None
    qs=np.linspace(0,1,n_bins+1)
    e=np.quantile(x,qs)
    e[0]=-np.inf; e[-1]=np.inf
    e=np.unique(e)
    if len(e)<3:return None
    return e

edges={f:fit_edges(train[f]) for f in features}
features=[f for f in features if edges.get(f) is not None]

def bin_col(s,e):
    x=pd.to_numeric(s,errors="coerce").replace([np.inf,-np.inf],np.nan).to_numpy()
    b=np.digitize(x,e[1:-1],right=True).astype(np.int16)
    b[~np.isfinite(x)]=-1
    return b

B={}
for split_name,d in [("train",train),("val",val),("test",test)]:
    B[split_name]={f:bin_col(d[f],edges[f]) for f in features}

def code_state(bin_dict, feats, n_bins=N_BINS):
    n=len(next(iter(bin_dict.values())))
    code=np.zeros(n,dtype=np.int32)
    valid=np.ones(n,dtype=bool)
    mult=1
    for f in feats:
        b=bin_dict[f]
        valid &= b>=0
        code += np.where(b>=0,b,0)*mult
        mult*=n_bins
    return code,valid,mult

def fit_prob_table(bin_dict,y,feats,prior):
    code,valid,size=code_state(bin_dict,feats)
    c=code[valid]; yy=np.asarray(y)[valid]
    n=np.bincount(c,minlength=size).astype(float)
    s=np.bincount(c,weights=yy,minlength=size).astype(float)
    p=(s+ALPHA*prior)/(n+ALPHA)
    p[n<MIN_CELL]=prior
    return p

def score_table(table,bin_dict,feats,prior):
    code,valid,_=code_state(bin_dict,feats)
    p=np.full(len(code),prior,dtype=float)
    p[valid]=table[code[valid]]
    return p

def met(y,p):
    y=np.asarray(y,dtype=int); p=np.clip(np.asarray(p),1e-6,1-1e-6)
    auc=roc_auc_score(y,p) if len(np.unique(y))>1 else np.nan
    base=y.mean(); order=np.argsort(p)
    k=max(1,len(y)//10); k5=max(1,len(y)//20)
    return dict(
      n=int(len(y)),base_rate=float(base),auc=float(auc),
      brier=float(brier_score_loss(y,p)),logloss=float(log_loss(y,p,labels=[0,1])),
      top10_rate=float(y[order[-k:]].mean()),bottom10_rate=float(y[order[:k]].mean()),
      top10_lift=float(y[order[-k:]].mean()/base) if base else None,
      top5_rate=float(y[order[-k5:]].mean()),top5_lift=float(y[order[-k5:]].mean()/base) if base else None
    )

def search_combos(target):
    ytr=train[target].astype(int).to_numpy()
    yv=val[target].astype(int).to_numpy()
    yt=test[target].astype(int).to_numpy()
    prior=float(ytr.mean())
    rows=[]
    # singles and all pairs
    combo_list=[(f,) for f in features] + list(combinations(features,2))
    for feats in combo_list:
        tab=fit_prob_table(B["train"],ytr,feats,prior)
        pv=score_table(tab,B["val"],feats,prior)
        m=met(yv,pv)
        rows.append({"target":target,"order":len(feats),"features":"|".join(feats),**m})
    sr=pd.DataFrame(rows).sort_values(["auc","top10_lift"],ascending=False).reset_index(drop=True)

    # Choose diverse features for triples from validation only.
    selected=[]
    for fs in sr.features:
        for f in fs.split("|"):
            if f not in selected:selected.append(f)
        if len(selected)>=TOP_FOR_TRIPLES:break
    triples=list(combinations(selected[:TOP_FOR_TRIPLES],3))
    tri=[]
    for feats in triples:
        tab=fit_prob_table(B["train"],ytr,feats,prior)
        pv=score_table(tab,B["val"],feats,prior)
        m=met(yv,pv)
        tri.append({"target":target,"order":3,"features":"|".join(feats),**m})
    allres=pd.concat([sr,pd.DataFrame(tri)],ignore_index=True).sort_values(["auc","top10_lift"],ascending=False).reset_index(drop=True)

    # Lock top candidates based on validation. Evaluate only these on 2025.
    locked=allres.head(25).copy()
    tests=[]
    for _,r in locked.iterrows():
        feats=tuple(r.features.split("|"))
        tab=fit_prob_table(B["train"],ytr,feats,prior)
        pt=score_table(tab,B["test"],feats,prior)
        tests.append({"target":target,"order":len(feats),"features":"|".join(feats),**met(yt,pt)})
    tdf=pd.DataFrame(tests).sort_values(["auc","top10_lift"],ascending=False).reset_index(drop=True)

    # Simple ensemble of top five locked probability tables; no test tuning.
    plist=[]
    for _,r in locked.head(5).iterrows():
        feats=tuple(r.features.split("|"))
        tab=fit_prob_table(B["train"],ytr,feats,prior)
        plist.append(score_table(tab,B["test"],feats,prior))
    pens=np.mean(np.vstack(plist),axis=0)
    ens={"target":target,"order":0,"features":"ENSEMBLE_TOP5_VALIDATION",**met(yt,pens)}

    return allres,tdf,ens,selected[:TOP_FOR_TRIPLES]

# Permutation max-statistic uses a representative set of all singles+pairs on a
# deterministic subset to estimate how large the best validation AUC can appear by chance.
def permutation_null(target):
    rng=np.random.default_rng(20261002+sum(map(ord,target)))
    ytr=train[target].astype(int).to_numpy()
    yv=val[target].astype(int).to_numpy()
    prior=float(ytr.mean())
    combo_list=[(f,) for f in features]+list(combinations(features,2))
    # To control runtime, evaluate all combos but on full validation; tables are cheap.
    maxima=[]
    for k in range(PERMUTATIONS):
        yp=ytr.copy(); rng.shuffle(yp)
        best=0.5
        for feats in combo_list:
            tab=fit_prob_table(B["train"],yp,feats,prior)
            pv=score_table(tab,B["val"],feats,prior)
            try:a=roc_auc_score(yv,pv)
            except Exception:a=0.5
            if a>best:best=a
        maxima.append(best)
    return np.asarray(maxima)

summary=[]; val_tables=[]; test_tables=[]; null_rows=[]; selected_map={}
for target in TARGETS:
    valres,testres,ens,sel=search_combos(target)
    val_tables.append(valres.assign(split="validation"))
    test_tables.append(testres.assign(split="test_2025"))
    test_tables.append(pd.DataFrame([ens]).assign(split="test_2025"))
    selected_map[target]=sel
    null=permutation_null(target)
    null_rows.append(pd.DataFrame({"target":target,"perm_max_auc":null}))
    best_val=float(valres.iloc[0].auc)
    p_corr=(1+int(np.sum(null>=best_val)))/(1+len(null))
    best_test=testres.iloc[0]
    summary.append({
      "target":target,"features_n":len(features),"validation_search_n":len(valres),
      "best_validation_features":valres.iloc[0].features,
      "best_validation_auc":best_val,
      "permutation_max_auc_mean":float(null.mean()),
      "permutation_max_auc_95pct":float(np.quantile(null,.95)),
      "familywise_perm_p":float(p_corr),
      "best_2025_locked_features":best_test.features,
      "best_2025_locked_auc":float(best_test.auc),
      "best_2025_top10_rate":float(best_test.top10_rate),
      "best_2025_top10_lift":float(best_test.top10_lift),
      "base_rate_2025":float(best_test.base_rate)
    })

pd.concat(val_tables,ignore_index=True).to_csv(OUT/"validation_combinatorial_search.csv",index=False)
pd.concat(test_tables,ignore_index=True).to_csv(OUT/"locked_test_results.csv",index=False)
pd.concat(null_rows,ignore_index=True).to_csv(OUT/"permutation_null.csv",index=False)
pd.DataFrame(summary).to_csv(OUT/"summary.csv",index=False)

# Mutual-information ranking is supplemental and computed only on training.
mi_rows=[]
for target in TARGETS:
    X=np.column_stack([B["train"][f] for f in features]).astype(float)
    X[X<0]=N_BINS
    y=train[target].astype(int).to_numpy()
    mi=mutual_info_classif(X,y,discrete_features=True,random_state=20261002)
    for f,v in zip(features,mi):mi_rows.append({"target":target,"feature":f,"mutual_info":float(v)})
pd.DataFrame(mi_rows).sort_values(["target","mutual_info"],ascending=[True,False]).to_csv(OUT/"mutual_information.csv",index=False)

meta={
 "events_total":int(len(df)),"train_n":int(len(train)),"validation_n":int(len(val)),"test_n":int(len(test)),
 "train_period":[str(train.date.min().date()),str(train.date.max().date())],
 "validation_period":[str(val.date.min().date()),str(val.date.max().date())],
 "test_period":[str(test.date.min().date()),str(test.date.max().date())],
 "features":features,"feature_count":len(features),"bins":N_BINS,
 "pairwise_combinations":int(len(list(combinations(features,2)))),
 "triple_combinations_per_target":int(math.comb(TOP_FOR_TRIPLES,3)),
 "permutations":PERMUTATIONS,
 "method":"beta-binomial smoothed empirical conditional probabilities; train-only quantile bins; exhaustive singles+pairs; triples among validation-selected diverse features; max-statistic shuffled-label familywise control",
 "warning":"historical source universe is not point-in-time NIFTY500 membership; survivorship bias remains"
}
(OUT/"meta.json").write_text(json.dumps(meta,indent=2))
print(json.dumps(meta,indent=2))
print("\nSUMMARY")
print(pd.DataFrame(summary).to_string(index=False))
