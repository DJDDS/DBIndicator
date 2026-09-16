import datetime as dt
import json
from app import recorder_observability

def test_operational_health_reports_capture_files_and_connection(tmp_path):
    v12_state=tmp_path/'v12_state.json'; v12_snapshots=tmp_path/'v12.jsonl'; v121_state=tmp_path/'v121_state.json'; root=tmp_path/'index'; backup=tmp_path/'backup.json'; root.mkdir()
    v12_state.write_text(json.dumps({'captured_slots': {'OPEN_STABLE':'2026-09-16T09:30:10','MIDDAY':'2026-09-16T13:00:08'},'last_capture_at':'2026-09-16T13:00:08','last_successful_write_at':'2026-09-16T13:00:09','quote_error_count':2,'last_write_error':None})); v12_snapshots.write_text('x\n')
    v121_state.write_text(json.dumps({'current_day':'2026-09-16','active_expiry':'2026-09-22','atm_strike':25200,'token_count':53,'session_last_tick_at':'2026-09-16T15:29:58','last_micro_write_at':'2026-09-16T15:29:55','last_depth_write_at':'2026-09-16T15:29:00','micro_snapshot_count':4320,'depth_snapshot_count':360,'last_write_error':None,'status':'RECORDING'})); (root/'2026-09-16_micro.jsonl').write_bytes(b'm'*20); (root/'2026-09-16_depth.jsonl').write_bytes(b'd'*10); backup.write_text(json.dumps({'status':'OFF-BOX BACKUP NOT CONFIGURED'}))
    out=recorder_observability.operational_health(v12_snapshot_file=v12_snapshots,v12_state_file=v12_state,v121_root=root,v121_state_file=v121_state,backup_state_file=backup,now=dt.datetime(2026,9,16,15,30),storage_mode='persistent',connection_state={'disconnect_count':1,'reconnect_count':1})
    assert out['v12']['slots']['OPEN_STABLE']['status']=='CAPTURED'; assert out['v12']['slots']['PRE_CAS']['status']=='MISSED'; assert out['v12']['slots']['POST_CAS']['status']=='PENDING'; assert out['v12']['snapshot_file_bytes']==2
    assert out['v121']['active_expiry']=='2026-09-22'; assert out['v121']['last_tick_age_seconds']==2.0; assert out['v121']['micro_file_bytes']==20; assert out['v121']['depth_file_bytes']==10; assert out['backup']['status']=='OFF-BOX BACKUP NOT CONFIGURED'; assert out['connection']['disconnect_count']==1

def test_operational_health_marks_slot_missed_only_after_grace(tmp_path):
    state=tmp_path/'state.json'; state.write_text('{}'); common=dict(v12_snapshot_file=tmp_path/'missing.jsonl',v12_state_file=state,v121_root=tmp_path,v121_state_file=tmp_path/'missing-state.json',backup_state_file=tmp_path/'missing-backup.json',storage_mode='persistent')
    pending=recorder_observability.operational_health(now=dt.datetime(2026,9,16,15,15),**common); missed=recorder_observability.operational_health(now=dt.datetime(2026,9,16,15,18),**common)
    assert pending['v12']['slots']['PRE_CAS']['status']=='PENDING'; assert missed['v12']['slots']['PRE_CAS']['status']=='MISSED'

def test_captured_slots_are_scoped_to_observed_day(tmp_path):
    state=tmp_path/'state.json'; state.write_text(json.dumps({'captured_slots':{'OPEN_STABLE':'2026-09-15T09:30:01'}}))
    out=recorder_observability.operational_health(v12_snapshot_file=tmp_path/'none',v12_state_file=state,v121_root=tmp_path,v121_state_file=tmp_path/'none2',backup_state_file=tmp_path/'none3',now=dt.datetime(2026,9,16,10,0),storage_mode='persistent')
    assert out['v12']['slots']['OPEN_STABLE']['status']=='MISSED'
