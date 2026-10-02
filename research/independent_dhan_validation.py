#!/usr/bin/env python3
from __future__ import annotations
import glob, json, math
from itertools import combinations
from pathlib import Path
import numpy as np
import pandas as pd
from sklearn.ensemble import HistGradientBoostingClassifier
from sklearn.metrics import roc_auc_score, log_loss
from sklearn.inspection import permutation_importance

ROOT=Path("/tmp/v11"); OUT=Path("/tmp/independent"); OUT.mkdir(parents=True,exist_ok=True)
p=glob.glob(str(ROOT/"**/events.parquet"),recursive=True)[0]
df=pd.read_parquet(p)
df["trade_date"]=pd.to_datetime(df["trade_date"])

VARIANTS=["empirical_multiscale","raw_5m_empirical"]
FEATURES=[
 "direction","z_1m","z_2m","z_3m","z_5m","z_10m","z_15m",
 "selected_horizon_min","selected_z","max_abs_z","cusum_up","cusum_down",
 "resid_cusum_up","resid_cusum_down","robust_sigma","residual_coherence",
 "minute_of_day","recent_vol_ratio"
]
TARGETS={
 "cont15":lambda z: ((z.signed_fwd_5m>0)&(z.signed_fwd_15m>0)).astype(int),
 "cont30":lambda z: ((z.signed_fwd_5m>0)&(z.signed_fwd_15m>0)&(z.signed_fwd_30m>0)).astype(int),
 "fp10":lambda z: (z.fp_10bps=="TARGET").astype(int),
}

def wilson(k,n,z=1.96):
    if n==0:return (None,None)
    p=k/n; den=1+z*z/n
    c=(p+z*z/(2*n))/den
    h=z*math.sqrt(p*(1-p)/n+z*z/(4*n*n))/den
    return float(c-h),float(c+h)

def split_dates(q):
    dates=np.array(sorted(q.trade_date.dt.normalize().unique()))
    n=len(dates)
    # 101-ish dates: 35 / 30 / remainder
    a=min(35,max(20,int(n*.35)))
    b=min(30,max(20,int(n*.30)))
    if a+b>=n-10:
        a=int(n*.4); b=int(n*.3)
    return dates[:a],dates[a:a+b],dates[a+b:]

def cleanX(q,med=None):
    X=q[FEATURES].replace([np.inf,-np.inf],np.nan).copy()
    if med is None: med=X.median(numeric_only=True)
    return X.fillna(med).fillna(0.0),med

def choose_gate(pv,yv,va):
    rows=[]
    for q in np.linspace(.50,.99,50):
        th=float(np.quantile(pv,q))
        s=pv>=th; n=int(s.sum())
        if n<120: continue
        days=int(va.loc[s,"trade_date"].nunique())
        if days<18: continue
        wr=float(yv[s].mean())
        rows.append((wr,n,days,q,th))
    inrange=[r for r in rows if .60<=r[0]<=.70]
    if inrange:
        # maximise support first, then closeness to 65%
        inrange.sort(key=lambda r:(-r[1],abs(r[0]-.65)))
        return "VAL_60_70",inrange[0],rows
    rows.sort(key=lambda r:(r[0],r[1]),reverse=True)
    return "NO_VAL_60_70",rows[0] if rows else (np.nan,0,0,np.nan,np.inf),rows

def fit_edges(s,bins=5):
    x=pd.to_numeric(s,errors="coerce").replace([np.inf,-np.inf],np.nan).dropna().to_numpy()
    if len(x)<100:return None
    e=np.unique(np.quantile(x,np.linspace(0,1,bins+1)))
    if len(e)<3:return None
    e=e.astype(float); e[0]=-np.inf; e[-1]=np.inf
    return e

def binned(q,features,edge_map):
    out={}
    for f in features:
        e=edge_map[f]
        x=pd.to_numeric(q[f],errors="coerce").replace([np.inf,-np.inf],np.nan).to_numpy()
        if e is None:
            b=np.zeros(len(x),dtype=np.int16); b[~np.isfinite(x)]=-1
        else:
            b=np.digitize(x,e[1:-1],right=True).astype(np.int16); b[~np.isfinite(x)]=-1
        out[f]=b
    return out

def codes(B,feats):
    return [tuple(int(B[f][i]) for f in feats) for i in range(len(next(iter(B.values()))))]

def prob_table(B,y,feats,alpha=12):
    prior=float(y.mean()); cs=codes(B,feats); n={}; s={}
    for c,yy in zip(cs,y):
        if -1 in c:continue
        n[c]=n.get(c,0)+1;s[c]=s.get(c,0)+int(yy)
    p={c:(s[c]+alpha*prior)/(n[c]+alpha) for c in n}
    return prior,p

def score(B,feats,prior,p):
    return np.array([p.get(c,prior) if -1 not in c else prior for c in codes(B,feats)])

summary=[]; details=[]
rng=np.random.default_rng(20261002)
for variant in VARIANTS:
    q=df[df.variant==variant].copy().sort_values(["trade_date","timestamp","symbol"])
    dates_tr,dates_va,dates_te=split_dates(q)
    tr=q[q.trade_date.dt.normalize().isin(dates_tr)].copy()
    va=q[q.trade_date.dt.normalize().isin(dates_va)].copy()
    te=q[q.trade_date.dt.normalize().isin(dates_te)].copy()
    for target,fn in TARGETS.items():
        for x in [tr,va,te]: x["_y"]=fn(x)
        Xtr,med=cleanX(tr); Xv,_=cleanX(va,med); Xt,_=cleanX(te,med)
        ytr=tr._y.to_numpy(); yv=va._y.to_numpy(); yt=te._y.to_numpy()

        # fixed nonlinear probability model
        model=HistGradientBoostingClassifier(learning_rate=.045,max_iter=160,max_leaf_nodes=23,
             min_samples_leaf=180,l2_regularization=5.0,random_state=20261002)
        model.fit(Xtr,ytr)
        pv=model.predict_proba(Xv)[:,1]; pt=model.predict_proba(Xt)[:,1]
        status,g,allg=choose_gate(pv,yv,va)
        wrv,nv,dv,qq,th=g
        st=pt>=th; nt=int(st.sum()); wt=float(yt[st].mean()) if nt else np.nan
        ci=wilson(int(yt[st].sum()),nt) if nt else (None,None)
        aucv=float(roc_auc_score(yv,pv)); auct=float(roc_auc_score(yt,pt))

        # validation permutation importance -> top 8 features
        sub=va.sample(min(30000,len(va)),random_state=20261002)
        Xs,_=cleanX(sub,med); ys=sub._y.to_numpy()
        pi=permutation_importance(model,Xs,ys,scoring="roc_auc",n_repeats=3,random_state=20261002)
        rank=[FEATURES[i] for i in np.argsort(pi.importances_mean)[::-1]]
        top8=rank[:8]

        # exhaustive 1/2/3-state probability tables among top8, selected only on validation.
        emap={f:fit_edges(tr[f],5) for f in top8}
        Btr=binned(tr,top8,emap); Bva=binned(va,top8,emap); Bte=binned(te,top8,emap)
        candidates=[]
        for r in [1,2,3]:
            for feats in combinations(top8,r):
                prior,tab=prob_table(Btr,ytr,feats)
                sv=score(Bva,feats,prior,tab)
                status2,g2,_=choose_gate(sv,yv,va)
                wr2,n2,d2,q2,th2=g2
                candidates.append((status2,wr2,n2,d2,th2,feats,prior,tab))
        preferred=[c for c in candidates if c[0]=="VAL_60_70"]
        if preferred:
            preferred.sort(key=lambda c:(-c[2],abs(c[1]-.65)))
            cc=preferred[0]
        else:
            candidates.sort(key=lambda c:(c[1],c[2]),reverse=True); cc=candidates[0]
        st2,wr2,n2,d2,th2,feats2,prior2,tab2=cc
        pte=score(Bte,feats2,prior2,tab2); sel2=pte>=th2
        nt2=int(sel2.sum()); wt2=float(yt[sel2].mean()) if nt2 else np.nan
        ci2=wilson(int(yt[sel2].sum()),nt2) if nt2 else (None,None)

        # locked combo shuffled-label null on final test AUC (100 train shuffles).
        obs_auc=float(roc_auc_score(yt,pte))
        null=[]
        for k in range(100):
            yp=rng.permutation(ytr)
            pr,tb=prob_table(Btr,yp,feats2)
            ps=score(Bte,feats2,pr,tb)
            null.append(float(roc_auc_score(yt,ps)))
        perm_p=float((1+sum(x>=obs_auc for x in null))/(len(null)+1))

        rec={
          "variant":variant,"target":target,
          "train_days":len(dates_tr),"validation_days":len(dates_va),"test_days":len(dates_te),
          "train_n":len(tr),"validation_n":len(va),"test_n":len(te),
          "base_test":float(yt.mean()),
          "hgb_val_auc":aucv,"hgb_test_auc":auct,"hgb_gate_status":status,
          "hgb_val_win":float(wrv),"hgb_val_gate_n":int(nv),
          "hgb_test_win":wt,"hgb_test_gate_n":nt,"hgb_test_ci_lo":ci[0],"hgb_test_ci_hi":ci[1],
          "combo_features":"|".join(feats2),"combo_gate_status":st2,
          "combo_val_win":float(wr2),"combo_val_gate_n":int(n2),
          "combo_test_win":wt2,"combo_test_gate_n":nt2,"combo_test_ci_lo":ci2[0],"combo_test_ci_hi":ci2[1],
          "combo_test_auc":obs_auc,"combo_perm_p":perm_p,
          "top_features":"|".join(top8)
        }
        summary.append(rec)
        print(json.dumps(rec),flush=True)

pd.DataFrame(summary).to_csv(OUT/"dhan_independent_validation.csv",index=False)
(OUT/"summary.json").write_text(json.dumps(summary,indent=2))
print(pd.DataFrame(summary).to_string(index=False))
