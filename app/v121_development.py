"""V12.1 development-only runner for remaining-session NIFTY variance."""
from __future__ import annotations

import datetime as dt
import json
import threading
from pathlib import Path

import pandas as pd

from . import scanner, v121_rv_lab

RESEARCH_LABEL = "DEVELOPMENT ONLY — NOT VALIDATED"


def _save(path, data):
    path=Path(path); path.parent.mkdir(parents=True,exist_ok=True)
    tmp=path.with_suffix(path.suffix+'.tmp')
    tmp.write_text(json.dumps(data,sort_keys=True,separators=(',',':'),default=str),encoding='utf-8')
    tmp.replace(path)


def development_status(path):
    try:
        data=json.loads(Path(path).read_text(encoding='utf-8'))
        if isinstance(data,dict):
            data.setdefault('trial25_locked',True)
            data.setdefault('promote_trial25',False)
            data.setdefault('research_label',RESEARCH_LABEL)
            return data
    except (OSError,ValueError,TypeError):
        pass
    return {'status':'NOT_RUN','trial25_locked':True,'promote_trial25':False,'research_label':RESEARCH_LABEL}


def _load_historical_inputs(kite,start,end):
    nifty_token=scanner._load_index_token(kite,'NIFTY 50')
    vix_token=scanner._load_index_token(kite,'INDIA VIX')
    if not nifty_token or not vix_token:
        raise RuntimeError('NIFTY 50 / INDIA VIX historical token unavailable')
    start_dt=dt.datetime.combine(start,dt.time(0,0)) if isinstance(start,dt.date) and not isinstance(start,dt.datetime) else start
    end_dt=dt.datetime.combine(end,dt.time(23,59)) if isinstance(end,dt.date) and not isinstance(end,dt.datetime) else end
    nifty_rows=scanner._fetch_historical_chunked(kite,nifty_token,start_dt,end_dt,'5minute')
    vix_rows=scanner._fetch_historical_chunked(kite,vix_token,start_dt-dt.timedelta(days=10),end_dt,'day')
    nifty=pd.DataFrame(nifty_rows)
    vix=pd.DataFrame(vix_rows)
    if nifty.empty or vix.empty:
        raise RuntimeError('historical NIFTY/VIX input unavailable')
    if 'date' in nifty.columns:
        nifty=nifty.rename(columns={'date':'timestamp'})
    if 'date' not in vix.columns:
        raise RuntimeError('VIX history missing date')
    if 'close' not in vix.columns:
        raise RuntimeError('VIX history missing close')
    vix=vix[['date','close']].rename(columns={'close':'vix'})
    return nifty,vix


def run_development_lab(kite,state_file,*,start,end,min_train=120):
    started=dt.datetime.now(dt.timezone.utc)
    try:
        nifty,vix=_load_historical_inputs(kite,start,end)
        ds=v121_rv_lab.build_remaining_session_dataset(nifty,vix)
        curve=v121_rv_lab.intraday_variance_curve(nifty)
        if len(ds) <= max(3,int(min_train)):
            out={
                'status':'INSUFFICIENT_DEVELOPMENT_SAMPLE','research_label':RESEARCH_LABEL,
                'trial25_locked':True,'promote_trial25':False,'dataset_rows':int(len(ds)),
                'u_curve_points':int(len(curve)),'oos_metrics':{'n':0,'mse':None,'qlike':None,'baseline_mse':None,'baseline_qlike':None},
            }
        else:
            pred=v121_rv_lab.rolling_oos_forecast(ds,min_train=min_train)
            metrics=v121_rv_lab.forecast_metrics(pred)
            out={
                'status':'COMPLETE_DEVELOPMENT_ONLY','research_label':RESEARCH_LABEL,
                'trial25_locked':True,'promote_trial25':False,'dataset_rows':int(len(ds)),
                'oos_metrics':metrics,'u_curve_points':int(len(curve)),
                'feature_end':'10:30','target_end':'15:10',
                'model':'log remaining variance ~ log morning variance + log prior-day VIX variance',
            }
        out['started_at']=started.isoformat(timespec='seconds')
        out['completed_at']=dt.datetime.now(dt.timezone.utc).isoformat(timespec='seconds')
        _save(state_file,out)
        return out
    except Exception as exc:
        out={'status':'ERROR','research_label':RESEARCH_LABEL,'trial25_locked':True,'promote_trial25':False,'error':str(exc),'started_at':started.isoformat(timespec='seconds')}
        _save(state_file,out)
        return out


_worker_thread = None
_worker_guard = threading.Lock()

def start_development_lab(kite,state_file,*,start,end,min_train=120):
    global _worker_thread
    with _worker_guard:
        if _worker_thread is not None and _worker_thread.is_alive():
            return {'status':'ALREADY_RUNNING','trial25_locked':True,'research_label':RESEARCH_LABEL}
        _save(state_file, {'status':'RUNNING','research_label':RESEARCH_LABEL,'trial25_locked':True,'promote_trial25':False,'start':str(start),'end':str(end)})
        def worker():
            run_development_lab(kite,state_file,start=start,end=end,min_train=min_train)
        _worker_thread=threading.Thread(target=worker,daemon=True,name='v121-rv-development')
        _worker_thread.start()
        return {'status':'STARTED','trial25_locked':True,'research_label':RESEARCH_LABEL}
