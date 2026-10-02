#!/usr/bin/env python3
"""Out-of-sample persistence classifier lab with session embargo and shuffled control."""
from pathlib import Path
import json, math
import numpy as np
import pandas as pd
from sklearn.compose import ColumnTransformer
from sklearn.impute import SimpleImputer
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import StandardScaler
from sklearn.linear_model import LogisticRegression
from sklearn.ensemble import HistGradientBoostingClassifier
from sklearn.metrics import roc_auc_score, brier_score_loss, log_loss

ROOT=Path("/tmp/shards")
OUT=Path("/tmp/final"); OUT.mkdir(parents=True,exist_ok=True)
TARGETS=["persist15","persist30","persist60","persistent_regime"]
FEATURES=[
 "z5_abs","r1_dir_bps","r3_dir_bps","r5_dir_bps","r10_dir_bps","r20_dir_bps",
 "eff5","eff10","eff20","compression20_60","accel_1_3","accel_3_5",
 "impulse_concentration5","range_pos20_dir","range_pos60_dir","from_open_dir_bps",
 "session_min","tod_sin","tod_cos","vol_z5","vol_accel","resid_r5_dir_bps"
]

files=list(ROOT.glob("**/feature_events.parquet"))
if not files: raise SystemExit("no feature shard artifacts")
df=pd.concat([pd.read_parquet(p) for p in files],ignore_index=True)
df["date"]=pd.to_datetime(df["date"])
df=df.sort_values(["date","symbol","ts"]).reset_index(drop=True)

# Full-session chronological split with one full trading-session embargo on each boundary.
all_dates=np.array(sorted(df.date.dt.normalize().unique()))
train_dates=all_dates[pd.DatetimeIndex(all_dates).year<=2022]
val_dates=all_dates[(pd.DatetimeIndex(all_dates).year>=2023)&(pd.DatetimeIndex(all_dates).year<=2024)]
test_dates=all_dates[pd.DatetimeIndex(all_dates).year==2025]
if len(train_dates)>1: train_dates=train_dates[:-1]
if len(val_dates)>2: val_dates=val_dates[1:-1]
if len(test_dates)>1: test_dates=test_dates[1:]

train=df[df.date.dt.normalize().isin(train_dates)].copy()
val=df[df.date.dt.normalize().isin(val_dates)].copy()
test=df[df.date.dt.normalize().isin(test_dates)].copy()

# Cap training rows deterministically only if needed for memory; preserve chronology coverage.
MAX_TRAIN=700_000
if len(train)>MAX_TRAIN:
    rng=np.random.default_rng(20261002)
    idx=np.sort(rng.choice(len(train),MAX_TRAIN,replace=False))
    train=train.iloc[idx].copy()

def metrics(y,p):
    y=np.asarray(y); p=np.clip(np.asarray(p),1e-6,1-1e-6)
    auc=roc_auc_score(y,p) if len(np.unique(y))>1 else np.nan
    base=float(np.mean(y))
    order=np.argsort(p)
    k=max(1,len(y)//10)
    top=float(np.mean(y[order[-k:]]))
    bot=float(np.mean(y[order[:k]]))
    return {
      "n":int(len(y)),"base_rate":base,"auc":float(auc),
      "brier":float(brier_score_loss(y,p)),"logloss":float(log_loss(y,p,labels=[0,1])),
      "top_decile_rate":top,"bottom_decile_rate":bot,
      "top_decile_lift":float(top/base) if base>0 else None,
      "spread_pp":float((top-bot)*100)
    }

def fit_logit(X,y):
    return Pipeline([
      ("impute",SimpleImputer(strategy="median",add_indicator=True)),
      ("scale",StandardScaler()),
      ("model",LogisticRegression(max_iter=500,C=0.2,n_jobs=1,class_weight=None))
    ]).fit(X,y)

def fit_hgb(X,y):
    X2=X.replace([np.inf,-np.inf],np.nan)
    med=X2.median(numeric_only=True)
    X2=X2.fillna(med).fillna(0.0)
    model=HistGradientBoostingClassifier(
      learning_rate=0.055,max_iter=180,max_leaf_nodes=23,min_samples_leaf=150,
      l2_regularization=2.0,random_state=20261002
    ).fit(X2,y)
    return model,med

results=[]
feature_effects=[]
rng=np.random.default_rng(20261002)

for target in TARGETS:
    ytr=train[target].astype(int); yv=val[target].astype(int); yt=test[target].astype(int)
    # Full feature linear baseline.
    logit=fit_logit(train[FEATURES].replace([np.inf,-np.inf],np.nan),ytr)
    pv=logit.predict_proba(val[FEATURES].replace([np.inf,-np.inf],np.nan))[:,1]
    pt=logit.predict_proba(test[FEATURES].replace([np.inf,-np.inf],np.nan))[:,1]
    results.append({"target":target,"model":"logit_full","split":"validation",**metrics(yv,pv)})
    results.append({"target":target,"model":"logit_full","split":"test_2025",**metrics(yt,pt)})

    # Non-linear benchmark.
    hgb,med=fit_hgb(train[FEATURES],ytr)
    pv=hgb.predict_proba(val[FEATURES].replace([np.inf,-np.inf],np.nan).fillna(med).fillna(0.0))[:,1]
    pt=hgb.predict_proba(test[FEATURES].replace([np.inf,-np.inf],np.nan).fillna(med).fillna(0.0))[:,1]
    results.append({"target":target,"model":"hgb_full","split":"validation",**metrics(yv,pv)})
    results.append({"target":target,"model":"hgb_full","split":"test_2025",**metrics(yt,pt)})

    # z-only benchmark: can magnitude alone do it?
    zhgb,zmed=fit_hgb(train[["z5_abs"]],ytr)
    pz=zhgb.predict_proba(test[["z5_abs"]].replace([np.inf,-np.inf],np.nan).fillna(zmed).fillna(0.0))[:,1]
    results.append({"target":target,"model":"hgb_z_only","split":"test_2025",**metrics(yt,pz)})

    # Shuffled-label leakage/null control.
    ysh=ytr.to_numpy().copy(); rng.shuffle(ysh)
    sh,shmed=fit_hgb(train[FEATURES],ysh)
    ps=sh.predict_proba(test[FEATURES].replace([np.inf,-np.inf],np.nan).fillna(shmed).fillna(0.0))[:,1]
    results.append({"target":target,"model":"hgb_shuffled_labels","split":"test_2025",**metrics(yt,ps)})

    # Permutation drop in AUC on a deterministic 100k test sample.
    samp=test if len(test)<=100_000 else test.sample(100_000,random_state=20261002)
    Xs=samp[FEATURES].replace([np.inf,-np.inf],np.nan).fillna(med).fillna(0.0)
    ys=samp[target].astype(int).to_numpy()
    basep=hgb.predict_proba(Xs)[:,1]
    baseauc=roc_auc_score(ys,basep)
    for f in FEATURES:
        Xp=Xs.copy()
        Xp[f]=rng.permutation(Xp[f].to_numpy())
        pp=hgb.predict_proba(Xp)[:,1]
        feature_effects.append({"target":target,"feature":f,"auc_drop":float(baseauc-roc_auc_score(ys,pp))})

# Univariate decile map for the target most aligned to the objective.
map_target="persistent_regime"
maps=[]
for f in FEATURES:
    q=test[[f,map_target]].replace([np.inf,-np.inf],np.nan).dropna()
    if len(q)<1000: continue
    try:
        q["bucket"]=pd.qcut(q[f],10,duplicates="drop")
    except Exception:
        continue
    g=q.groupby("bucket",observed=True).agg(n=(map_target,"size"),rate=(map_target,"mean"),feature_mean=(f,"mean")).reset_index(drop=True)
    for i,row in g.iterrows():
        maps.append({"feature":f,"decile":int(i+1),"n":int(row.n),"feature_mean":float(row.feature_mean),"persistent_regime_rate":float(row.rate)})

res=pd.DataFrame(results)
imp=pd.DataFrame(feature_effects).sort_values(["target","auc_drop"],ascending=[True,False])
umap=pd.DataFrame(maps)
res.to_csv(OUT/"model_results.csv",index=False)
imp.to_csv(OUT/"permutation_importance.csv",index=False)
umap.to_csv(OUT/"univariate_deciles.csv",index=False)

meta={
  "events_total":int(len(df)),"train_n":int(len(train)),"validation_n":int(len(val)),"test_n":int(len(test)),
  "train_period":[str(train.date.min().date()),str(train.date.max().date())],
  "validation_period":[str(val.date.min().date()),str(val.date.max().date())],
  "test_period":[str(test.date.min().date()),str(test.date.max().date())],
  "embargo":"one whole trading session removed at each split boundary",
  "features":FEATURES,
  "targets":TARGETS,
  "warning":"Universe is source repository stock set, not point-in-time historical NIFTY-500 membership; survivorship bias remains."
}
(OUT/"meta.json").write_text(json.dumps(meta,indent=2))
print(json.dumps(meta,indent=2))
print("\nMODEL RESULTS")
print(res.to_string(index=False))
print("\nTOP FEATURES")
for t in TARGETS:
    print("\n",t)
    print(imp[imp.target==t].head(12).to_string(index=False))
