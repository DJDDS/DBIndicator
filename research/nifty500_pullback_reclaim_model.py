#!/usr/bin/env python3
from pathlib import Path
import json
import numpy as np
import pandas as pd
from sklearn.ensemble import HistGradientBoostingClassifier
from sklearn.metrics import roc_auc_score, brier_score_loss, log_loss

ROOT=Path("/tmp/shards"); OUT=Path("/tmp/final"); OUT.mkdir(parents=True,exist_ok=True)
TARGETS=["persist15","persist30","persist60","persistent_regime","persistent10bps30"]
IGNITE=[
 "z5_abs","impulse_bps","r1_at_ignite_dir_bps","r3_at_ignite_dir_bps",
 "r10_at_ignite_dir_bps","r20_at_ignite_dir_bps","from_open_dir_bps",
 "vol_z5_at_ignite","resid_r5_at_ignite_dir_bps","session_min"
]
MICRO=[
 "post1_bps","post2_bps","post3_bps","post3_maxfav_bps","post3_minfav_bps",
 "post3_surrender_bps","post3_pullback_ratio","post3_adverse_ratio",
 "post3_efficiency","post3_reclaim70","post3_holds_impulse","post3_extends_impulse",
 "post3_volume_ratio"
]
FULL=IGNITE+MICRO

files=list(ROOT.glob("**/events.parquet"))
df=pd.concat([pd.read_parquet(p) for p in files],ignore_index=True)
df["date"]=pd.to_datetime(df["date"]); df=df.sort_values(["date","symbol","ts"]).reset_index(drop=True)
dts=pd.DatetimeIndex(sorted(df.date.dt.normalize().unique()))
train_dates=dts[dts.year<=2022][:-1]
val_dates=dts[(dts.year>=2023)&(dts.year<=2024)][1:-1]
test_dates=dts[dts.year==2025][1:]
train=df[df.date.dt.normalize().isin(train_dates)].copy()
val=df[df.date.dt.normalize().isin(val_dates)].copy()
test=df[df.date.dt.normalize().isin(test_dates)].copy()
MAX_TRAIN=700000
if len(train)>MAX_TRAIN:
    rng=np.random.default_rng(20261002)
    train=train.iloc[np.sort(rng.choice(len(train),MAX_TRAIN,replace=False))].copy()
rng=np.random.default_rng(20261002)

def prep_fit(X,y):
    X=X.replace([np.inf,-np.inf],np.nan)
    med=X.median(numeric_only=True); X=X.fillna(med).fillna(0.0)
    m=HistGradientBoostingClassifier(
      learning_rate=.055,max_iter=180,max_leaf_nodes=23,min_samples_leaf=150,
      l2_regularization=2.0,random_state=20261002).fit(X,y)
    return m,med

def pred(m,med,X,cols):
    return m.predict_proba(X[cols].replace([np.inf,-np.inf],np.nan).fillna(med).fillna(0.0))[:,1]

def metrics(y,p):
    y=np.asarray(y); p=np.clip(np.asarray(p),1e-6,1-1e-6)
    auc=roc_auc_score(y,p) if len(np.unique(y))>1 else np.nan
    order=np.argsort(p); k=max(1,len(y)//10); k5=max(1,len(y)//20)
    base=y.mean()
    return {"n":int(len(y)),"base_rate":float(base),"auc":float(auc),
      "brier":float(brier_score_loss(y,p)),"logloss":float(log_loss(y,p,labels=[0,1])),
      "top10_rate":float(y[order[-k:]].mean()),"bottom10_rate":float(y[order[:k]].mean()),
      "top10_lift":float(y[order[-k:]].mean()/base) if base else None,
      "top5_rate":float(y[order[-k5:]].mean()),"top5_lift":float(y[order[-k5:]].mean()/base) if base else None}

rows=[]; imps=[]
for target in TARGETS:
    ytr=train[target].astype(int); yv=val[target].astype(int); yt=test[target].astype(int)
    for name,cols in [("ignite_only",IGNITE),("ignite_plus_micro",FULL)]:
        m,med=prep_fit(train[cols],ytr)
        pv=pred(m,med,val,cols); pt=pred(m,med,test,cols)
        rows.append({"target":target,"model":name,"split":"validation",**metrics(yv,pv)})
        rows.append({"target":target,"model":name,"split":"test_2025",**metrics(yt,pt)})
        if name=="ignite_plus_micro":
            samp=test if len(test)<=100000 else test.sample(100000,random_state=20261002)
            Xs=samp[cols].replace([np.inf,-np.inf],np.nan).fillna(med).fillna(0.0)
            ys=samp[target].astype(int).to_numpy(); bp=m.predict_proba(Xs)[:,1]; ba=roc_auc_score(ys,bp)
            for f in cols:
                Xp=Xs.copy(); Xp[f]=rng.permutation(Xp[f].to_numpy())
                imps.append({"target":target,"feature":f,"auc_drop":float(ba-roc_auc_score(ys,m.predict_proba(Xp)[:,1]))})
    # shuffled-label null with full feature set
    ysh=ytr.to_numpy().copy(); rng.shuffle(ysh)
    sm,smed=prep_fit(train[FULL],ysh); ps=pred(sm,smed,test,FULL)
    rows.append({"target":target,"model":"shuffled_labels","split":"test_2025",**metrics(yt,ps)})

res=pd.DataFrame(rows); imp=pd.DataFrame(imps).sort_values(["target","auc_drop"],ascending=[True,False])
res.to_csv(OUT/"model_results.csv",index=False); imp.to_csv(OUT/"permutation_importance.csv",index=False)

# Direct condition map on 2025 for interpretable microstructure states.
conds={
 "reclaim70":test.post3_reclaim70==1,
 "holds_impulse":test.post3_holds_impulse==1,
 "extends_impulse":test.post3_extends_impulse==1,
 "controlled_pullback_0_50pct":(test.post3_pullback_ratio>=0)&(test.post3_pullback_ratio<=0.5),
 "low_adverse_le_25pct":test.post3_adverse_ratio<=0.25,
 "reclaim_and_low_adverse":(test.post3_reclaim70==1)&(test.post3_adverse_ratio<=0.25),
 "holds_and_low_adverse":(test.post3_holds_impulse==1)&(test.post3_adverse_ratio<=0.25),
}
cr=[]
for nm,mask in conds.items():
    q=test[mask]
    cr.append({"condition":nm,"n":len(q),"pct_of_test":100*len(q)/len(test),
      "persist15":q.persist15.mean(),"persist30":q.persist30.mean(),"persist60":q.persist60.mean(),
      "persistent_regime":q.persistent_regime.mean(),"persistent10bps30":q.persistent10bps30.mean(),
      "mean_fwd30_bps":q.fwd30_bps.mean(),"median_fwd30_bps":q.fwd30_bps.median()})
pd.DataFrame(cr).to_csv(OUT/"condition_map.csv",index=False)

meta={"events_total":int(len(df)),"train_n":int(len(train)),"validation_n":int(len(val)),"test_n":int(len(test)),
 "train_period":[str(train.date.min().date()),str(train.date.max().date())],
 "validation_period":[str(val.date.min().date()),str(val.date.max().date())],
 "test_period":[str(test.date.min().date()),str(test.date.max().date())],
 "decision_delay_min":3,"embargo":"one whole trading session at split boundaries",
 "ignite_features":IGNITE,"micro_features":MICRO,
 "warning":"source-repository universe; not point-in-time historical NIFTY500 membership"}
(OUT/"meta.json").write_text(json.dumps(meta,indent=2))
print(json.dumps(meta,indent=2)); print(res.to_string(index=False)); print("\nCONDITIONS"); print(pd.DataFrame(cr).to_string(index=False))
print("\nTOP FEATURES"); 
for t in TARGETS:
    print("\n"+t); print(imp[imp.target==t].head(15).to_string(index=False))
