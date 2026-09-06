import datetime as dt
import json
import numpy as np
import pandas as pd


def _historical(days=180):
    rows=[]; vix=[]
    start=dt.date(2025,1,2)
    px=24000.0
    for i,day in enumerate(pd.bdate_range(start,periods=days).date):
        vix.append({'date':day,'vix':13+0.5*np.sin(i/9)})
        t=dt.datetime.combine(day,dt.time(9,15))
        while t.time()<=dt.time(15,10):
            shock=0.0002*np.sin((t.hour*60+t.minute+i)/17)
            px*=np.exp(shock)
            rows.append({'timestamp':t,'close':px})
            t+=dt.timedelta(minutes=5)
    return pd.DataFrame(rows),pd.DataFrame(vix)


def test_development_lab_is_development_only_and_never_promotes(tmp_path,monkeypatch):
    from app import v121_development
    bars,vix=_historical()
    monkeypatch.setattr(v121_development,'_load_historical_inputs',lambda kite,start,end:(bars,vix))
    state=tmp_path/'state.json'
    out=v121_development.run_development_lab(object(),state,start=dt.date(2025,1,1),end=dt.date(2026,1,1),min_train=60)
    assert out['research_label']=='DEVELOPMENT ONLY — NOT VALIDATED'
    assert out['trial25_locked'] is True and out['promote_trial25'] is False
    assert out['dataset_rows']>100
    assert out['oos_metrics']['n']>0
    assert out['u_curve_points']>0
    persisted=json.loads(state.read_text())
    assert persisted['status']=='COMPLETE_DEVELOPMENT_ONLY'


def test_development_status_empty_is_locked(tmp_path):
    from app.v121_development import development_status
    out=development_status(tmp_path/'missing.json')
    assert out['status']=='NOT_RUN'
    assert out['trial25_locked'] is True


def test_start_development_lab_runs_in_background_and_marks_running(tmp_path,monkeypatch):
    from app import v121_development
    calls=[]
    monkeypatch.setattr(v121_development,'run_development_lab',lambda *a,**k: calls.append(1) or {'status':'COMPLETE_DEVELOPMENT_ONLY'})
    class FakeThread:
        def __init__(self,target,daemon,name): self.target=target
        def start(self): self.target()
        def is_alive(self): return False
    monkeypatch.setattr(v121_development.threading,'Thread',FakeThread)
    v121_development._worker_thread=None
    out=v121_development.start_development_lab(object(),tmp_path/'state.json',start=dt.date(2025,1,1),end=dt.date(2025,12,31),min_train=60)
    assert out['status']=='STARTED' and calls==[1]
