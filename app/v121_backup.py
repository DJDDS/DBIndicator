"""V12.1 completed-day compression and optional S3-compatible off-box backup."""
from __future__ import annotations

import datetime as dt
import gzip
import json
import shutil
from pathlib import Path


def _load(path):
    try:
        data=json.loads(Path(path).read_text(encoding='utf-8'))
        return data if isinstance(data,dict) else {}
    except (OSError,ValueError,TypeError):
        return {}


def _save(path,data):
    path=Path(path); path.parent.mkdir(parents=True,exist_ok=True)
    tmp=path.with_suffix(path.suffix+'.tmp')
    tmp.write_text(json.dumps(data,sort_keys=True,separators=(',',':'),default=str),encoding='utf-8')
    tmp.replace(path)


def compress_completed_day(root, day):
    root=Path(root); day=day if isinstance(day,dt.date) else dt.date.fromisoformat(str(day)[:10])
    out=[]
    for suffix in ('micro','depth'):
        src=root/f'{day.isoformat()}_{suffix}.jsonl'
        dst=Path(str(src)+'.gz')
        if not src.exists():
            if dst.exists(): out.append(dst)
            continue
        tmp=Path(str(dst)+'.tmp')
        with src.open('rb') as fin, gzip.open(tmp,'wb') as fout:
            shutil.copyfileobj(fin,fout)
        tmp.replace(dst)
        src.unlink()
        out.append(dst)
    return out


def _default_client_factory(*,endpoint_url,region):
    import boto3
    kwargs={'region_name':region}
    if endpoint_url: kwargs['endpoint_url']=endpoint_url
    return boto3.client('s3',**kwargs)


def backup_files(paths,*,bucket,prefix,endpoint_url,region,client_factory=None):
    if not bucket:
        return {'status':'OFF-BOX BACKUP NOT CONFIGURED','uploaded':0,'error':None}
    factory=client_factory or _default_client_factory
    try:
        client=factory(endpoint_url=endpoint_url,region=region)
        uploaded=0
        prefix=str(prefix or '').strip('/')
        for p in paths:
            p=Path(p)
            key=f'{prefix}/{p.name}' if prefix else p.name
            client.upload_file(str(p),bucket,key)
            uploaded+=1
        return {'status':'OK','uploaded':uploaded,'error':None}
    except Exception as exc:  # backup must never break live recording
        return {'status':'ERROR','uploaded':0,'error':str(exc)}


def run_daily_backup_cycle(root,state_file,*,now,bucket,prefix,endpoint_url,region,client_factory=None):
    root=Path(root); state=_load(state_file); day=now.date()
    # This cycle is called only after the recorder's session boundary. Guard it
    # anyway so an accidental daytime call cannot rotate the active files.
    if now.hour < 16:
        return {**state,'status':state.get('status') or 'WAITING_FOR_CLOSE'}
    if state.get('last_cycle_day')==day.isoformat():
        return state
    if state.get('status') == 'ERROR' and state.get('last_attempt_day') == day.isoformat() and state.get('last_cycle_at'):
        try:
            last = dt.datetime.fromisoformat(str(state.get('last_cycle_at')))
            if (now - last).total_seconds() < 15 * 60:
                return state
        except (TypeError, ValueError):
            pass
    archives=compress_completed_day(root,day)
    result=backup_files(archives,bucket=bucket,prefix=prefix,endpoint_url=endpoint_url,region=region,client_factory=client_factory)
    state.update({
        'last_attempt_day':day.isoformat(), 'last_cycle_at':now.isoformat(timespec='seconds'),
        'status':result['status'], 'last_error':result.get('error'), 'uploaded':result.get('uploaded',0),
        'archives':[str(p) for p in archives],
    })
    if result['status'] in ('OK','OFF-BOX BACKUP NOT CONFIGURED'):
        state['last_cycle_day']=day.isoformat()
    else:
        state.pop('last_cycle_day', None)
    if result['status']=='OK': state['last_successful_backup_at']=now.isoformat(timespec='seconds')
    _save(state_file,state)
    return state


def backup_status(path):
    state=_load(path)
    if not state:
        return {'status':'OFF-BOX BACKUP NOT CONFIGURED','last_successful_backup_at':None,'last_error':None,'uploaded':0}
    state.setdefault('status','OFF-BOX BACKUP NOT CONFIGURED')
    return state
