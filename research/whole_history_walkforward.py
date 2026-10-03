from __future__ import annotations
import os, io, re, csv, json, math, time, zipfile, hashlib, bisect, warnings
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
import numpy as np
import pandas as pd
import requests
from sklearn.cluster import KMeans
from sklearn.metrics import roc_auc_score
from lightgbm import LGBMClassifier

warnings.filterwarnings('ignore')
OUT=Path(os.getenv('OUT_DIR','whole_history_out')); OUT.mkdir(parents=True,exist_ok=True)
SAMPLE_MOD=int(os.getenv('SAMPLE_MOD','20'))
H_PATH=[1,2,3,5,10,15,20,30,45,60,90,120]
H_EVAL=[5,15,30,60]
TARGET_PREC=[0.60,0.65,0.70]
K_BY_GROUP={'fno':4,'nonfno':3}
FEATURES=[
'rv60','mkt_rv20','range20','from_open','gap','upper_wick','mkt_r1','lower_wick','range5','range60','range10','body','resid_r1','r60','tod_sin','pos60','rv20','vol_tod_ratio','vol1_20','resid_r15','mkt_r3','mkt_r15','eff20','pos10','rv10','accel_1_3','tod_cos','eff60','r1','rel_rv20','resid_r30','rv5','accel_5_15','eff5','resid_r5','accel_3_5','logvol','pos20','r2','r15','r30','resid_r3','r5','eff10','r10','resid_r60','mkt_r30','vol5_20','pos5','mkt_r5','mkt_r60','r3']
HEADERS={'User-Agent':'Mozilla/5.0 research/1.0','Accept':'*/*'}
S=requests.Session(); S.headers.update(HEADERS)

def get_bytes(url, tries=5, timeout=180):
    last=None
    for i in range(tries):
        try:
            r=S.get(url,timeout=timeout)
            if r.status_code==200 and len(r.content)>100: return r.content
            last=RuntimeError(f'{url} status={r.status_code} bytes={len(r.content)}')
        except Exception as e: last=e
        time.sleep(min(10,1.5*(i+1)))
    raise last

def get_json(url, tries=5):
    return json.loads(get_bytes(url,tries=tries).decode('utf-8'))

def load_minute(url):
    df=pd.read_parquet(io.BytesIO(get_bytes(url))).rename(columns=lambda c:c.lower())
    if 'date' not in df: raise ValueError('no Date')
    df['date']=pd.to_datetime(df['date'])
    df=df.rename(columns={'date':'ts'})[['ts','open','high','low','close','volume']].copy()
    for c in ['open','high','low','close','volume']: df[c]=pd.to_numeric(df[c],errors='coerce')
    return df.sort_values('ts').drop_duplicates('ts',keep='last')

def fetch_tree(repo):
    return get_json(f'https://api.github.com/repos/{repo}/git/trees/main?recursive=1')['tree']

def parse_constituent_csv(content):
    if content[:2]==b'\x1f\x8b':
        import gzip; content=gzip.decompress(content)
    lines=content.decode('utf-8-sig','ignore').splitlines()
    start=0
    for i,l in enumerate(lines):
        if l.strip().lower().startswith('company name'): start=i; break
    out=set()
    for r in csv.DictReader(lines[start:]):
        sym=(r.get('Symbol') or r.get('SYMBOL') or '').strip().upper()
        if sym: out.add(sym)
    return out

def build_pit_snapshots():
    """Load verified historical NIFTY500 Wayback snapshots from a committed public mirror.

    The upstream raw file contains actual archived ind_nifty500list.csv captures
    and extends through Jan-2026, allowing the original conservative bracketing
    rule without a live Wayback dependency.
    """
    cache=OUT/'pit_snapshots.json'
    if cache.exists():
        return [(pd.Timestamp(d),set(v)) for d,v in json.load(open(cache))]
    url='https://raw.githubusercontent.com/srees16/centurion_core/main/data/nifty500_wayback_raw.json'
    raw=json.loads(get_bytes(url,tries=5,timeout=120).decode('utf-8'))
    legacy={
        '2018-10':'2018-10-04','2019-02':'2019-02-01','2020-07':'2020-07-25',
        '2022-05':'2022-05-04','2022-10':'2022-10-09','2023-04':'2023-04-04',
        '2024-02':'2024-02-07','2024-02b':'2024-02-26','2025-06':'2025-06-16',
        '2025-08':'2025-08-21'
    }
    snaps=[]
    for label,vals in raw.items():
        if str(label).startswith('_') or not isinstance(vals,list) or not vals:
            continue
        d=legacy.get(label,label)[:10]
        try: dt=pd.Timestamp(d)
        except Exception: continue
        syms={str(x).strip().upper() for x in vals if str(x).strip()}
        if 350<=len(syms)<=650:
            snaps.append((dt,syms))
    snaps.sort(key=lambda x:x[0])
    ded=[];prev=None
    for d,s in snaps:
        if prev is None or s!=prev:
            ded.append((d,s)); prev=s
    snaps=ded
    if len(snaps)<4:
        raise RuntimeError(f'insufficient PIT snapshots from committed archive: {len(snaps)}')
    json.dump([(d.date().isoformat(),sorted(s)) for d,s in snaps],open(cache,'w'))
    print('PIT snapshots',len(snaps),[(str(d.date()),len(s)) for d,s in snaps])
    return snaps

def pit_sets_for_dates(dates,snaps):
    sd=[d for d,_ in snaps]; out={}
    for d in sorted(set(pd.Timestamp(x).normalize() for x in dates)):
        i=bisect.bisect_right(sd,d)-1; j=bisect.bisect_left(sd,d)
        out[d.date().isoformat()]=snaps[i][1]&snaps[j][1] if i>=0 and j<len(sd) else set()
    return out

def parse_fno_zip(b):
    z=zipfile.ZipFile(io.BytesIO(b)); names=[n for n in z.namelist() if n.lower().endswith('.csv')]
    if not names:return set()
    rd=csv.DictReader(io.TextIOWrapper(z.open(names[0]),encoding='utf-8-sig',errors='ignore',newline=''))
    fields=set(rd.fieldnames or []); out=set()
    if 'INSTRUMENT' in fields:
        for r in rd:
            if (r.get('INSTRUMENT') or '').strip() in {'FUTSTK','OPTSTK'}:
                x=(r.get('SYMBOL') or '').strip().upper()
                if x: out.add(x)
    elif 'FinInstrmTp' in fields:
        for r in rd:
            if (r.get('FinInstrmTp') or '').strip() in {'STF','STO','FUTSTK','OPTSTK'}:
                x=(r.get('TckrSymb') or '').strip().upper()
                if x: out.add(x)
    return out

def fno_url(d):
    mon=d.strftime('%b').upper()
    fn=(f"fo{d.strftime('%d')}{mon}{d.strftime('%Y')}bhav.csv.zip" if d<pd.Timestamp('2024-07-08')
        else f"BhavCopy_NSE_FO_0_0_0_{d.strftime('%Y%m%d')}_F_0000.csv.zip")
    if d>=pd.Timestamp('2020-04-13'):
        return f"https://raw.githubusercontent.com/SantoshSrinivas79/NSE-FNO-Data-bank/main/data/{d.year}/{d.strftime('%m')}/{fn}"
    return f"https://archives.nseindia.com/content/historical/DERIVATIVES/{d.year}/{mon}/{fn}"

def build_fno_map(dates):
    cache=OUT/'fno_map.json'
    if cache.exists():return {k:set(v) for k,v in json.load(open(cache)).items()}
    ds=[pd.Timestamp(x).normalize() for x in sorted(set(dates))]
    def one(d):
        try:return d.date().isoformat(),parse_fno_zip(get_bytes(fno_url(d),tries=3,timeout=90))
        except Exception:
            mon=d.strftime('%b').upper()
            u=(f"https://nsearchives.nseindia.com/content/historical/DERIVATIVES/{d.year}/{mon}/fo{d.strftime('%d')}{mon}{d.strftime('%Y')}bhav.csv.zip"
               if d<pd.Timestamp('2024-07-08') else f"https://nsearchives.nseindia.com/content/fo/BhavCopy_NSE_FO_0_0_0_{d.strftime('%Y%m%d')}_F_0000.csv.zip")
            try:return d.date().isoformat(),parse_fno_zip(get_bytes(u,tries=2,timeout=90))
            except Exception:return d.date().isoformat(),set()
    out={}
    with ThreadPoolExecutor(max_workers=12) as ex:
        futs=[ex.submit(one,d) for d in ds]
        for n,f in enumerate(as_completed(futs),1):
            k,v=f.result();out[k]=v
            if n%100==0:print('FNO dates',n,'/',len(ds))
    good=sum(bool(v) for v in out.values());print('FNO map good',good,'/',len(ds))
    json.dump({k:sorted(v) for k,v in out.items()},open(cache,'w'))
    return out

def market_features(mkt):
    x=mkt.copy();x['day']=x.ts.dt.normalize()
    def f(g):
        g=g.copy();c=g.close;rb=c.pct_change()*10000
        for n in [1,3,5,15,30,60]:g[f'mkt_r{n}']=c.pct_change(n)*10000
        g['mkt_rv20']=rb.rolling(20,min_periods=20).std(ddof=0);return g
    x=x.groupby('day',group_keys=False).apply(f)
    return x[['ts','mkt_r1','mkt_r3','mkt_r5','mkt_r15','mkt_r30','mkt_r60','mkt_rv20']].set_index('ts')

def stock_state_frame(df,mktfeat):
    df=df.copy()
    df=df[(df.ts.dt.time>=pd.Timestamp('09:15').time())&(df.ts.dt.time<=pd.Timestamp('15:29').time())]
    df['day']=df.ts.dt.normalize();df['slot']=df.ts.dt.hour*60+df.ts.dt.minute
    df['slot_vol_base']=df.groupby('slot')['volume'].transform(lambda s:s.shift(1).rolling(20,min_periods=5).mean())
    def f(g):
        g=g.copy();c=g.close;o=g.open;h=g.high;l=g.low;v=g.volume;r1=c.pct_change()*10000
        for n in [1,2,3,5,10,15,30,60]:g[f'r{n}']=c.pct_change(n)*10000
        for n in [5,10,20,60]:
            g[f'rv{n}']=r1.rolling(n,min_periods=n).std(ddof=0)
            hi=h.rolling(n,min_periods=n).max();lo=l.rolling(n,min_periods=n).min();span=hi-lo
            g[f'range{n}']=span/c*10000;g[f'pos{n}']=(c-lo)/span.replace(0,np.nan)
            g[f'eff{n}']=(c-c.shift(n)).abs()/c.diff().abs().rolling(n,min_periods=n).sum().replace(0,np.nan)
        g['accel_1_3']=g['r1']-(g['r3']-g['r1'])/2
        g['accel_3_5']=g['r3']/3-(g['r5']-g['r3'])/2
        g['accel_5_15']=g['r5']/5-(g['r15']-g['r5'])/10
        g['body']=(c-o)/o*10000;g['upper_wick']=(h-np.maximum(o,c))/o*10000;g['lower_wick']=(np.minimum(o,c)-l)/o*10000
        g['from_open']=(c/g['open'].iloc[0]-1)*10000;g['logvol']=np.log1p(v.clip(lower=0))
        mean20=v.rolling(20,min_periods=20).mean();g['vol1_20']=v/mean20.replace(0,np.nan)
        g['vol5_20']=v.rolling(5,min_periods=5).sum()/(5*mean20.replace(0,np.nan));return g
    df=df.groupby('day',group_keys=False).apply(f)
    daily=df.groupby('day').agg(day_open=('open','first'),day_close=('close','last'));daily['prev_close']=daily.day_close.shift(1);daily['gap']=(daily.day_open/daily.prev_close-1)*10000
    df=df.merge(daily[['gap']],left_on='day',right_index=True,how='left')
    df['vol_tod_ratio']=df.volume/df.slot_vol_base.replace(0,np.nan)
    sm=(df.ts.dt.hour*60+df.ts.dt.minute)-(9*60+15);ang=2*np.pi*sm/375
    df['tod_sin']=np.sin(ang);df['tod_cos']=np.cos(ang);df=df.join(mktfeat,on='ts')
    for n in [1,3,5,15,30,60]:df[f'resid_r{n}']=df[f'r{n}']-df[f'mkt_r{n}']
    df['rel_rv20']=df.rv20/df.mkt_rv20.replace(0,np.nan);return df

def stable_keep(symbol,ts):
    return int.from_bytes(hashlib.blake2b(f'{symbol}|{ts.isoformat()}'.encode(),digest_size=8).digest(),'little')%SAMPLE_MOD==0

def make_samples(symbol,df,mktfeat,pit_map,fno_map):
    x=stock_state_frame(df,mktfeat);rows=[]
    for d,g0 in x.groupby('day'):
        ds=d.date().isoformat()
        if symbol not in pit_map.get(ds,set()):continue
        idx=pd.date_range(d+pd.Timedelta(hours=9,minutes=15),d+pd.Timedelta(hours=15,minutes=29),freq='1min')
        g=g0.set_index('ts').reindex(idx);closes=g.close.to_numpy(float)
        for i,ts in enumerate(idx):
            if ts.minute%5 or ts.time()<pd.Timestamp('10:15').time() or ts.time()>pd.Timestamp('13:25').time():continue
            if i<60 or i+120>=len(g) or not stable_keep(symbol,ts):continue
            rr=g.iloc[i]
            if any(not np.isfinite(rr.get(c,np.nan)) for c in ['close','rv60','range60','from_open','mkt_rv20']):continue
            fut=closes[i+1:i+121]
            if len(fut)<120 or not np.isfinite(fut).all():continue
            path=(fut/closes[i]-1)*10000;pos=0;neg=0
            for v in path:
                if v>0:pos+=1
                else:break
            for v in path:
                if v<0:neg+=1
                else:break
            rec={'ts':ts,'date':d,'symbol':symbol,'year':d.year,'group':'fno' if symbol in fno_map.get(ds,set()) else 'nonfno','pos_run':pos,'neg_run':neg}
            for c in FEATURES:rec[c]=rr.get(c,np.nan)
            for h in H_PATH:rec[f'fwd{h}']=path[h-1]
            for h in H_EVAL:
                p=path[:h];rec[f'mfe_up_{h}']=max(0,float(np.max(p)));rec[f'mae_up_{h}']=max(0,float(-np.min(p)))
                rec[f'mfe_down_{h}']=max(0,float(-np.min(p)));rec[f'mae_down_{h}']=max(0,float(np.max(p)))
            rows.append(rec)
    return rows

def process_archive():
    if (OUT/'SAMPLES_DONE').exists():return
    tree=fetch_tree('rahulkanadia/IndianMarkets_DataBackfill')
    paths=[x['path'] for x in tree if x['type']=='blob' and x['path'].endswith('.parquet') and '-INDEX' not in x['path']]
    mkt=load_minute('https://raw.githubusercontent.com/rahulkanadia/IndianMarkets_DataBackfill/main/NIFTY50-INDEX.parquet')
    mkt=mkt[(mkt.ts>='2018-01-01')&(mkt.ts<'2026-01-01')]
    dates=sorted(mkt.ts.dt.normalize().unique());snaps=build_pit_snapshots();pit_map=pit_sets_for_dates(dates,snaps);fno_map=build_fno_map(dates);mf=market_features(mkt)
    pit_union=set().union(*pit_map.values());paths=[p for p in paths if Path(p).stem.upper() in pit_union]
    print('stock files selected',len(paths),'pit union',len(pit_union))
    buffers={(y,g):[] for y in range(2018,2026) for g in ('fno','nonfno')}
    def flush(key):
        b=buffers[key]
        if not b:return
        y,g=key;out=OUT/f'samples_{y}_{g}.parquet';new=pd.DataFrame(b)
        if out.exists():new=pd.concat([pd.read_parquet(out),new],ignore_index=True)
        new.to_parquet(out,index=False,compression='zstd');buffers[key]=[]
    for n,p in enumerate(paths,1):
        sym=Path(p).stem.upper()
        try:
            df=load_minute('https://raw.githubusercontent.com/rahulkanadia/IndianMarkets_DataBackfill/main/'+p)
            df=df[(df.ts>='2018-01-01')&(df.ts<'2026-01-01')]
            for r in make_samples(sym,df,mf,pit_map,fno_map):
                key=(r['year'],r['group']);buffers[key].append(r)
                if len(buffers[key])>=25000:flush(key)
        except Exception as e:print('FAIL',sym,type(e).__name__,e)
        if n%20==0:print('processed',n,'/',len(paths))
    for k in list(buffers):flush(k)
    summary={f'{y}_{g}':(len(pd.read_parquet(OUT/f'samples_{y}_{g}.parquet')) if (OUT/f'samples_{y}_{g}.parquet').exists() else 0) for y,g in buffers}
    json.dump(summary,open(OUT/'sample_counts.json','w'),indent=2);print('sample counts',summary);(OUT/'SAMPLES_DONE').write_text('ok')

def fit_state_model(train,group):
    k=K_BY_GROUP[group];pathcols=[f'fwd{h}' for h in H_PATH];Z=train[pathcols].to_numpy(float);mu=np.nanmean(Z,0);sd=np.nanstd(Z,0);sd[sd<1e-9]=1;Zn=(Z-mu)/sd
    cap=min(250000,len(train));idx=np.linspace(0,len(train)-1,cap,dtype=int) if len(train)>cap else np.arange(len(train))
    km=KMeans(n_clusters=k,n_init=15,random_state=42,max_iter=300).fit(Zn[idx]);y=km.predict(Zn)
    clf=LGBMClassifier(objective='multiclass',num_class=k,learning_rate=.035,num_leaves=15,min_child_samples=120,n_estimators=100,random_state=42,n_jobs=-1,verbosity=-1)
    clf.fit(train[FEATURES],y);surv={'up':{},'down':{},'mean_up':{},'mean_down':{}}
    for c in range(k):
        m=y==c;surv['mean_up'][c]=float(train.loc[m,'pos_run'].mean());surv['mean_down'][c]=float(train.loc[m,'neg_run'].mean())
        for h in H_PATH:
            surv['up'][(c,h)]=float((train.loc[m,'pos_run']>=h).mean());surv['down'][(c,h)]=float((train.loc[m,'neg_run']>=h).mean())
    return {'mu':mu,'sd':sd,'km':km,'clf':clf,'surv':surv}

def score(model,df,direction,h):
    probs=model['clf'].predict_proba(df[FEATURES]);return probs@np.array([model['surv'][direction][(c,h)] for c in range(probs.shape[1])])

def select_thr(prob,y,target,min_n=100):
    p=np.asarray(prob);yy=np.asarray(y,dtype=int);m=np.isfinite(p);p=p[m];yy=yy[m]
    if len(p)<min_n:return None
    o=np.argsort(-p);p=p[o];yy=yy[o];n=np.arange(1,len(p)+1);prec=np.cumsum(yy)/n;ok=np.where((n>=min_n)&(prec>=target))[0]
    if not len(ok):return None
    i=ok[-1];return float(p[i]),int(n[i]),float(prec[i])

def ci_wilson(k,n,z=1.96):
    if not n:return np.nan,np.nan
    ph=k/n;den=1+z*z/n;ctr=(ph+z*z/(2*n))/den;half=z*math.sqrt(ph*(1-ph)/n+z*z/(4*n*n))/den;return ctr-half,ctr+half

def metrics_for_signal(df,prob,direction,h,thr):
    s=df.loc[np.asarray(prob)>=thr].copy();n=len(s)
    if not n:return {'signals':0}
    days=max(1,s.date.nunique());run='pos_run' if direction=='up' else 'neg_run';y=(s[run]>=h).astype(int);wins=int(y.sum());lo,hi=ci_wilson(wins,n)
    final=(s[f'fwd{h}']>0) if direction=='up' else (s[f'fwd{h}']<0);dur=s[run];mfe=s[f'mfe_{direction}_{h}'];mae=s[f'mae_{direction}_{h}']
    consumed=np.maximum(s.from_open.to_numpy() if direction=='up' else -s.from_open.to_numpy(),0);den=consumed+mfe.to_numpy();cp=np.where(den>1e-9,100*consumed/den,np.nan)
    return {'signals':n,'wins':wins,'win_rate':wins/n,'ci_lo':lo,'ci_hi':hi,'coverage':n/len(df),'signals_per_day':n/days,'stocks_per_day':s.groupby('date').symbol.nunique().mean(),'false_alerts_per_day':(n-wins)/days,'avg_persistence_min':dur.mean(),'median_persistence_min':dur.median(),'final_direction_hit':float(final.mean()),'avg_mfe_bps':mfe.mean(),'avg_mae_bps':mae.mean(),'move_consumed_pct':float(np.nanmean(cp))}

def evaluate_split(model,val,test,group,label):
    rows=[];aucs=[]
    for direction in ('up','down'):
        run='pos_run' if direction=='up' else 'neg_run'
        for h in H_EVAL:
            pv=score(model,val,direction,h);pt=score(model,test,direction,h);yv=(val[run]>=h).astype(int).to_numpy();yt=(test[run]>=h).astype(int).to_numpy()
            try:auc=roc_auc_score(yt,pt)
            except:auc=np.nan
            q90=np.quantile(pt,.90);q95=np.quantile(pt,.95)
            aucs.append({'split':label,'group':group,'direction':direction,'horizon':h,'n':len(test),'base_rate':float(yt.mean()),'auc':auc,'top10_rate':float(yt[pt>=q90].mean()),'top5_rate':float(yt[pt>=q95].mean())})
            for target in TARGET_PREC:
                sel=select_thr(pv,yv,target)
                if sel is None:rows.append({'split':label,'group':group,'direction':direction,'horizon':h,'target_precision':target,'threshold':np.nan,'validation_signals':0,'validation_precision':np.nan,'signals':0})
                else:
                    thr,nv,pr=sel;rows.append({'split':label,'group':group,'direction':direction,'horizon':h,'target_precision':target,'threshold':thr,'validation_signals':nv,'validation_precision':pr,**metrics_for_signal(test,pt,direction,h,thr)})
    return rows,aucs

def load_years(years,group):
    xs=[]
    for y in years:
        p=OUT/f'samples_{y}_{group}.parquet'
        if p.exists():xs.append(pd.read_parquet(p))
    return pd.concat(xs,ignore_index=True) if xs else pd.DataFrame()

def run_walkforward():
    allr=[];alla=[]
    for group in ('fno','nonfno'):
        for testy in range(2021,2026):
            train=load_years(range(2018,testy-1),group);val=load_years([testy-1],group);test=load_years([testy],group)
            if min(len(train),len(val),len(test))<1000:print('skip fold',group,testy,len(train),len(val),len(test));continue
            print('FIT',group,testy,len(train),len(val),len(test));model=fit_state_model(train,group);r,a=evaluate_split(model,val,test,group,f'walk_{testy}');allr+=r;alla+=a
        train=load_years(range(2018,2023),group);val=load_years([2023,2024],group);test=load_years([2025],group)
        if min(len(train),len(val),len(test))>=1000:
            model=fit_state_model(train,group);r,a=evaluate_split(model,val,test,group,'checksum_2025');allr+=r;alla+=a
        train=load_years(range(2018,2024),group);val=load_years([2024],group);model=fit_state_model(train,group)
        import joblib;joblib.dump(model,OUT/f'final_model_{group}.joblib',compress=3)
        th=[]
        for direction in ('up','down'):
            run='pos_run' if direction=='up' else 'neg_run'
            for h in H_EVAL:
                pv=score(model,val,direction,h);yv=(val[run]>=h).astype(int).to_numpy()
                for target in TARGET_PREC:
                    sel=select_thr(pv,yv,target);th.append({'group':group,'direction':direction,'horizon':h,'target_precision':target,'threshold':None if sel is None else sel[0],'validation_signals':0 if sel is None else sel[1],'validation_precision':None if sel is None else sel[2]})
        pd.DataFrame(th).to_csv(OUT/f'final_thresholds_{group}.csv',index=False)
    pd.DataFrame(allr).to_csv(OUT/'walkforward_threshold_results.csv',index=False);pd.DataFrame(alla).to_csv(OUT/'walkforward_auc_results.csv',index=False)
    a=pd.DataFrame(alla);w=pd.DataFrame(allr)
    if len(a):a[a.split.str.startswith('walk_')].groupby(['group','direction','horizon']).agg(years=('split','nunique'),mean_auc=('auc','mean'),min_auc=('auc','min'),max_auc=('auc','max'),mean_top5=('top5_rate','mean'),mean_base=('base_rate','mean')).reset_index().to_csv(OUT/'walkforward_auc_summary.csv',index=False)
    if len(w):w[w.split.str.startswith('walk_')].groupby(['group','direction','horizon','target_precision']).agg(years=('split','nunique'),years_with_signal=('signals',lambda x:int((x>0).sum())),signals=('signals','sum'),weighted_wins=('wins','sum'),mean_signals_per_day=('signals_per_day','mean'),mean_false_alerts_day=('false_alerts_per_day','mean')).reset_index().to_csv(OUT/'walkforward_threshold_summary.csv',index=False)

def main():
    process_archive();run_walkforward();print('DONE',OUT)
if __name__=='__main__':main()
