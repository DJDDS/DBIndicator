#!/usr/bin/env python3
from __future__ import annotations
import json, math, os
from pathlib import Path
import numpy as np
import pandas as pd
from sklearn.cluster import MiniBatchKMeans
from sklearn.preprocessing import StandardScaler
from sklearn.ensemble import HistGradientBoostingClassifier
from sklearn.metrics import silhouette_score, roc_auc_score, log_loss, brier_score_loss
from sklearn.inspection import permutation_importance

ROOT=Path("/tmp/panels"); OUT=Path("/tmp/final"); OUT.mkdir(parents=True,exist_ok=True)
H=[1,2,3,5,10,15,20,30,45,60,90,120]
FUT=[f"fnorm{h}" for h in H]
STATE=[
 "r1","r2","r3","r5","r10","r15","r30","r60",
 "rv5","rv10","rv20","rv60","range5","range10","range20","range60",
 "pos5","pos10","pos20","pos60","eff5","eff10","eff20","eff60",
 "accel_1_3","accel_3_5","accel_5_15","from_open","gap",
 "body","upper_wick","lower_wick","logvol","vol1_20","vol5_20","vol_tod_ratio",
 "mkt_r1","mkt_r3","mkt_r5","mkt_r15","mkt_r30","mkt_r60","mkt_rv20",
 "resid_r1","resid_r3","resid_r5","resid_r15","resid_r30","resid_r60","rel_rv20",
 "tod_sin","tod_cos"
]
RNG=np.random.default_rng(20261002)

files=list(ROOT.glob("**/minute_state_panel.parquet"))
if not files: raise SystemExit("no panels")
df=pd.concat([pd.read_parquet(p) for p in files],ignore_index=True)
df["ts"]=pd.to_datetime(df.ts); df["date"]=pd.to_datetime(df.date)
df=df.replace([np.inf,-np.inf],np.nan)
print("merged rows",len(df),"symbols",df.symbol.nunique(),flush=True)

def cap(q,n,seed):
    if len(q)<=n:return q.copy()
    return q.sample(n,random_state=seed).copy()

def auc_safe(y,p):
    y=np.asarray(y)
    if len(np.unique(y))<2:return np.nan
    return roc_auc_score(y,p)

def day_boot_auc(q,score_col,target_col,n=200,seed=123):
    # Resample whole trading days using sample weights; avoids rebuilding giant frames.
    day=q.date.dt.normalize().to_numpy()
    uniq,inv=np.unique(day,return_inverse=True)
    y=q[target_col].to_numpy(); p=q[score_col].to_numpy()
    rng=np.random.default_rng(seed); vals=[]
    for _ in range(n):
        counts=np.bincount(rng.integers(0,len(uniq),len(uniq)),minlength=len(uniq))
        w=counts[inv].astype(float)
        if w.sum()==0: continue
        try: vals.append(roc_auc_score(y,p,sample_weight=w))
        except Exception: pass
    return [float(np.quantile(vals,.025)),float(np.quantile(vals,.975))] if vals else [None,None]

def label_perm_p(y,p,n=200,seed=1):
    # Permutation null on a deterministic <=30k subset. Observed AUC remains full-sample.
    y=np.asarray(y,dtype=int); p=np.asarray(p,float)
    obs=auc_safe(y,p)
    if len(y)>30000:
        rng0=np.random.default_rng(seed+991)
        ix=np.sort(rng0.choice(len(y),30000,replace=False))
        yy=y[ix]; pp=p[ix]
    else:
        yy=y.copy(); pp=p.copy()
    # rank-sum AUC allows label permutations without re-sorting scores.
    order=np.argsort(pp,kind="mergesort")
    ranks=np.empty(len(pp),dtype=float); ranks[order]=np.arange(1,len(pp)+1,dtype=float)
    n1=int(yy.sum()); n0=len(yy)-n1
    rng=np.random.default_rng(seed); vals=[]
    for _ in range(n):
        yp=rng.permutation(yy)
        rs=float(ranks[yp==1].sum())
        auc=(rs-n1*(n1+1)/2)/(n1*n0) if n1 and n0 else 0.5
        vals.append(auc)
    vals=np.asarray(vals)
    return obs,float((1+(vals>=obs).sum())/(n+1)),float(vals.mean()),float(np.quantile(vals,.99))

def run_group(name,q):
    gd=q[q.split=="discovery"].copy()
    gv=q[q.split=="validation"].copy()
    gt=q[q.split=="test"].copy()
    print(name,"sizes",len(gd),len(gv),len(gt),flush=True)
    # clustering sample only; assignments later cover all.
    cdis=cap(gd.dropna(subset=FUT),220000,11)
    cval=cap(gv.dropna(subset=FUT),80000,12)
    scaler=StandardScaler().fit(cdis[FUT])
    Xd=scaler.transform(cdis[FUT]); Xv=scaler.transform(cval[FUT])
    krows=[]; models={}
    for k in range(3,8):
        km=MiniBatchKMeans(n_clusters=k,batch_size=8192,n_init=10,random_state=20261002,max_iter=200)
        km.fit(Xd)
        lv=km.predict(Xv)
        sil=silhouette_score(Xv,lv,sample_size=min(20000,len(Xv)),random_state=20261002)
        # occupancy penalty only to avoid degenerate tiny clusters
        occ=np.bincount(lv,minlength=k)/len(lv)
        score=float(sil - max(0,0.02-occ.min())*2)
        krows.append({"group":name,"k":k,"validation_silhouette":float(sil),"min_cluster_pct":float(occ.min()*100),"selection_score":score})
        models[k]=km
        print(name,"k",k,"sil",sil,"minocc",occ.min(),flush=True)
    ks=pd.DataFrame(krows)
    bestk=int(ks.sort_values("selection_score",ascending=False).iloc[0].k)
    km=models[bestk]
    # assign all splits
    def assign(d):
        good=d[FUT].notna().all(axis=1)
        z=d.loc[good].copy()
        z["cluster"]=km.predict(scaler.transform(z[FUT]))
        return z
    gd=assign(gd); gv=assign(gv); gt=assign(gt)
    # cluster behavior discovered only from discovery
    chars=[]
    surv={}
    for c in range(bestk):
        z=gd[gd.cluster==c]
        mean_path=np.array([z[f"fwd{h}_bps"].mean() for h in H])
        integral=float(np.trapezoid(mean_path,np.array(H)))
        direction="UP" if integral>0 else "DOWN"
        run=z.pos_run if direction=="UP" else z.neg_run
        rec={"group":name,"cluster":c,"n":len(z),"direction":direction,
             "integral_bps_min":integral,"mean_fwd30_bps":float(z.fwd30_bps.mean()),
             "mean_fwd60_bps":float(z.fwd60_bps.mean()),"mean_fwd120_bps":float(z.fwd120_bps.mean()),
             "mean_direction_run_min":float(run.mean()),
             "median_direction_run_min":float(run.median()),
             "mean_pos_run":float(z.pos_run.mean()),"mean_neg_run":float(z.neg_run.mean())}
        for h in [3,5,10,15,30,60,120]:
            rec[f"survive_{h}m"]=float((run>=h).mean())
        chars.append(rec)
    chars=pd.DataFrame(chars)

    # classifier hyperparameter selection on validation; train capped for runtime.
    tr=cap(gd,360000,21); va=cap(gv,160000,22)
    med=tr[STATE].median(numeric_only=True)
    Xtr=tr[STATE].fillna(med).fillna(0.0); Xva=va[STATE].fillna(med).fillna(0.0)
    ytr=tr.cluster.astype(int); yva=va.cluster.astype(int)
    grid=[]
    for lr in [0.035,0.06]:
      for leaves in [15,31]:
       for leafn in [120,300]:
        model=HistGradientBoostingClassifier(
          learning_rate=lr,max_iter=180,max_leaf_nodes=leaves,min_samples_leaf=leafn,
          l2_regularization=3.0,random_state=20261002).fit(Xtr,ytr)
        pv=model.predict_proba(Xva)
        ll=log_loss(yva,pv,labels=list(range(bestk)))
        aucs=[]
        for c in range(bestk):
            aucs.append(auc_safe((yva==c).astype(int),pv[:,c]))
        grid.append({"lr":lr,"leaves":leaves,"leafn":leafn,"val_logloss":float(ll),"val_macro_auc":float(np.nanmean(aucs))})
        print(name,"grid",grid[-1],flush=True)
    gdf=pd.DataFrame(grid).sort_values(["val_logloss","val_macro_auc"],ascending=[True,False])
    bp=gdf.iloc[0]
    # refit on discovery+validation
    train=pd.concat([gd,gv],ignore_index=True)
    train=cap(train,500000,23)
    med=train[STATE].median(numeric_only=True)
    X=train[STATE].fillna(med).fillna(0.0); y=train.cluster.astype(int)
    model=HistGradientBoostingClassifier(
      learning_rate=float(bp.lr),max_iter=220,max_leaf_nodes=int(bp.leaves),min_samples_leaf=int(bp.leafn),
      l2_regularization=3.0,random_state=20261002).fit(X,y)
    p=model.predict_proba(gt[STATE].fillna(med).fillna(0.0))
    test=gt[["ts","date","symbol","cluster","pos_run","neg_run"]].copy()
    for c in range(bestk): test[f"p_cluster_{c}"]=p[:,c]

    # cluster survival curves estimated from discovery+validation after hyperparameters are locked.
    train_for_surv=pd.concat([gd,gv],ignore_index=True)
    for direction,runcol in [("up","pos_run"),("down","neg_run")]:
        for h in [3,5,10,15,30,60,120]:
            rates=np.array([float((train_for_surv[train_for_surv.cluster==c][runcol]>=h).mean()) for c in range(bestk)])
            test[f"p_{direction}_{h}"]=p@rates
            test[f"y_{direction}_{h}"]=(gt[runcol].to_numpy()>=h).astype(int)
        means=np.array([float(train_for_surv[train_for_surv.cluster==c][runcol].mean()) for c in range(bestk)])
        test[f"expected_{direction}_duration"]=p@means
        test[f"actual_{direction}_duration"]=gt[runcol].to_numpy()

    metrics=[]
    # cluster prediction
    for c in range(bestk):
        ytrue=(gt.cluster.to_numpy()==c).astype(int); score=p[:,c]
        order=np.argsort(score); k10=max(1,len(score)//10)
        base=ytrue.mean(); top=ytrue[order[-k10:]].mean()
        obs,pperm,nm,n99=label_perm_p(ytrue,score,n=500,seed=100+c)
        metrics.append({"group":name,"target":f"cluster_{c}","horizon":None,"auc":obs,
                        "base_rate":float(base),"top10_rate":float(top),"top10_lift":float(top/base) if base else None,
                        "perm_p":pperm,"perm_auc_mean":nm,"perm_auc_99pct":n99})
    # survival probabilities
    for direction in ["up","down"]:
        for h in [3,5,10,15,30,60,120]:
            yc=f"y_{direction}_{h}"; pc=f"p_{direction}_{h}"
            ytrue=test[yc].to_numpy(); score=test[pc].to_numpy()
            obs,pperm,nm,n99=label_perm_p(ytrue,score,n=500,seed=1000+h+(0 if direction=="up" else 5000))
            order=np.argsort(score); k10=max(1,len(score)//10); k5=max(1,len(score)//20)
            base=ytrue.mean(); top=ytrue[order[-k10:]].mean(); top5=ytrue[order[-k5:]].mean()
            ci=day_boot_auc(test.assign(_y=ytrue,_p=score)," _p".strip()," _y".strip(),n=200,seed=h)
            metrics.append({"group":name,"target":direction,"horizon":h,"auc":obs,"auc_ci_lo":ci[0],"auc_ci_hi":ci[1],
                            "base_rate":float(base),"top10_rate":float(top),"top10_lift":float(top/base) if base else None,
                            "top5_rate":float(top5),"top5_lift":float(top5/base) if base else None,
                            "brier":float(brier_score_loss(ytrue,score)),"perm_p":pperm,"perm_auc_mean":nm,"perm_auc_99pct":n99})
    metrics=pd.DataFrame(metrics)

    # permutation feature importance on test subset using multiclass logloss.
    samp=cap(gt,50000,31)
    Xs=samp[STATE].fillna(med).fillna(0.0); ys=samp.cluster.astype(int)
    base_ll=log_loss(ys,model.predict_proba(Xs),labels=list(range(bestk)))
    rng=np.random.default_rng(20261002)
    imps=[]
    for f in STATE:
        Xp=Xs.copy(); Xp[f]=rng.permutation(Xp[f].to_numpy())
        ll=log_loss(ys,model.predict_proba(Xp),labels=list(range(bestk)))
        imps.append({"group":name,"feature":f,"logloss_increase":float(ll-base_ll)})
    imp=pd.DataFrame(imps).sort_values("logloss_increase",ascending=False)

    # save
    chars.to_csv(OUT/f"{name}_cluster_characteristics.csv",index=False)
    ks.to_csv(OUT/f"{name}_k_selection.csv",index=False)
    gdf.to_csv(OUT/f"{name}_model_grid.csv",index=False)
    metrics.to_csv(OUT/f"{name}_metrics.csv",index=False)
    imp.to_csv(OUT/f"{name}_feature_importance.csv",index=False)
    test.to_parquet(OUT/f"{name}_test_predictions.parquet",index=False)
    return {"group":name,"best_k":bestk,"rows":{"discovery":len(gd),"validation":len(gv),"test":len(gt)},
            "best_model":bp.to_dict(),
            "clusters":chars.to_dict("records"),
            "headline":metrics[(metrics.target.isin(["up","down"]))&(metrics.horizon.isin([15,30,60,120]))].to_dict("records")}

summ=[]
wanted=os.environ.get("GROUP_ONLY")
groups=[("fno",df.is_fno.astype(bool)),("nonfno",~df.is_fno.astype(bool))]
if wanted:
    groups=[g for g in groups if g[0]==wanted]
for name,mask in groups:
    q=df[mask].copy()
    summ.append(run_group(name,q))
(OUT/"summary.json").write_text(json.dumps(summ,indent=2,default=str))
pd.DataFrame([{"group":x["group"],"best_k":x["best_k"],**x["rows"]} for x in summ]).to_csv(OUT/"population_sizes.csv",index=False)
print(json.dumps(summ,indent=2,default=str))
