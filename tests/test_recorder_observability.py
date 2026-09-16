import datetime as dt
import json
from app import recorder_observability

def _health(tmp_path, now, v12=None, v121=None):
    root=tmp_path/'index'; root.mkdir(exist_ok=True); vs=tmp_path/'v12.json'; ns=tmp_path/'v121.json'; bs=tmp_path/'backup.json'; snap=tmp_path/'snap.jsonl'
    vs.write_text(json.dumps(v12 or {})); ns.write_text(json.dumps(v121 or {})); bs.write_text(json.dumps({'status':'OFF-BOX BACKUP NOT CONFIGURED'})); snap.write_text('x\n')
    day=(v121 or {}).get('current_day',now.date().isoformat()); (root/f'{day}_micro.jsonl').write_bytes(b'm'*20); (root/f'{day}_depth.jsonl').write_bytes(b'd'*10)
    return recorder_observability.operational_health(v12_snapshot_file=snap,v12_state_file=vs,v121_root=root,v121_state_file=ns,backup_state_file=bs,now=now,storage_mode='persistent',connection_state={'disconnect_count':1,'reconnect_count':1})

def test_operational_health_reports_capture_files_and_connection(tmp_path):
    out=_health(tmp_path,dt.datetime(2026,9,16,15,30),{'captured_slots':{'OPEN_STABLE':'2026-09-16T09:30:10','MIDDAY':'2026-09-16T13:00:08'},'quote_error_count':2},{'current_day':'2026-09-16','active_expiry':'2026-09-22','atm_strike':25200,'token_count':53,'session_last_tick_at':'2026-09-16T15:29:58','last_micro_write_at':'2026-09-16T15:29:55','last_depth_write_at':'2026-09-16T15:29:00','micro_snapshot_count':4320,'depth_snapshot_count':360,'status':'RECORDING'})
    assert out['v12']['slots']['OPEN_STABLE']['status']=='CAPTURED'; assert out['v12']['slots']['PRE_CAS']['status']=='MISSED'; assert out['v12']['slots']['POST_CAS']['status']=='PENDING'; assert out['v12']['snapshot_file_bytes']==2
    assert out['v121']['active_expiry']=='2026-09-22'; assert out['v121']['last_tick_age_seconds']==2.0; assert out['v121']['micro_file_bytes']==20; assert out['v121']['depth_file_bytes']==10; assert out['backup']['status']=='OFF-BOX BACKUP NOT CONFIGURED'; assert out['connection']['disconnect_count']==1

def test_slot_is_missed_only_after_grace(tmp_path):
    assert _health(tmp_path,dt.datetime(2026,9,16,15,15))['v12']['slots']['PRE_CAS']['status']=='PENDING'
    assert _health(tmp_path,dt.datetime(2026,9,16,15,18))['v12']['slots']['PRE_CAS']['status']=='MISSED'

def test_stale_capture_does_not_certify_current_day(tmp_path):
    out=_health(tmp_path,dt.datetime(2026,9,16,10,0),{'captured_slots':{'OPEN_STABLE':'2026-09-15T09:30:01'}})
    assert out['v12']['slots']['OPEN_STABLE']['status']=='MISSED'

def test_future_dated_v121_state_does_not_select_wrong_day_files(tmp_path):
    out=_health(tmp_path,dt.datetime(2026,9,16,15,30),v121={'current_day':'2026-09-15','session_last_tick_at':'2026-09-15T15:29:58'})
    assert out['v121']['recording_day']=='2026-09-16'
    assert out['v121']['state_day_matches_today'] is False
    assert out['v121']['last_tick_age_seconds'] is None
