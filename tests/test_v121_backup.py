import datetime as dt
import gzip
import json


def test_compress_completed_day_creates_gzip_and_removes_source(tmp_path):
    from app.v121_backup import compress_completed_day
    root=tmp_path/'index_vol'; root.mkdir()
    p=root/'2026-09-07_micro.jsonl'; p.write_text('{"x":1}\n',encoding='utf-8')
    out=compress_completed_day(root,dt.date(2026,9,7))
    assert len(out)==1 and out[0].name.endswith('.jsonl.gz')
    assert not p.exists()
    with gzip.open(out[0],'rt',encoding='utf-8') as f:
        assert json.loads(f.readline())['x']==1


class FakeS3:
    def __init__(self, fail=False): self.calls=[]; self.fail=fail
    def upload_file(self, filename, bucket, key):
        if self.fail: raise RuntimeError('upload failed')
        self.calls.append((filename,bucket,key))


def test_backup_files_uses_s3_compatible_client_and_prefix(tmp_path):
    from app.v121_backup import backup_files
    p=tmp_path/'a.jsonl.gz'; p.write_bytes(b'x')
    fake=FakeS3()
    out=backup_files([p],bucket='bucket',prefix='dbindicator/v121',endpoint_url='',region='ap-south-1',client_factory=lambda **kw:fake)
    assert out['status']=='OK' and out['uploaded']==1
    assert fake.calls[0][1:] == ('bucket','dbindicator/v121/a.jsonl.gz')


def test_backup_failure_is_reported_not_raised(tmp_path):
    from app.v121_backup import backup_files
    p=tmp_path/'a.jsonl.gz'; p.write_bytes(b'x')
    out=backup_files([p],bucket='bucket',prefix='p',endpoint_url='',region='ap-south-1',client_factory=lambda **kw:FakeS3(fail=True))
    assert out['status']=='ERROR' and 'upload failed' in out['error']


def test_daily_backup_cycle_compresses_after_close_and_marks_not_configured(tmp_path):
    from app.v121_backup import run_daily_backup_cycle
    root=tmp_path/'index_vol'; root.mkdir()
    (root/'2026-09-07_micro.jsonl').write_text('{"x":1}\n',encoding='utf-8')
    state=tmp_path/'backup.json'
    out=run_daily_backup_cycle(root,state,now=dt.datetime(2026,9,7,16,5),bucket='',prefix='p',endpoint_url='',region='ap-south-1')
    assert out['status']=='OFF-BOX BACKUP NOT CONFIGURED'
    assert (root/'2026-09-07_micro.jsonl.gz').exists()
    assert json.loads(state.read_text())['last_cycle_day']=='2026-09-07'


def test_daily_backup_error_remains_retryable_same_day(tmp_path):
    from app.v121_backup import run_daily_backup_cycle
    root=tmp_path/'index_vol'; root.mkdir()
    (root/'2026-09-07_micro.jsonl').write_text('{"x":1}\n',encoding='utf-8')
    state=tmp_path/'backup.json'
    out=run_daily_backup_cycle(root,state,now=dt.datetime(2026,9,7,16,5),bucket='b',prefix='p',endpoint_url='',region='ap-south-1',client_factory=lambda **kw:FakeS3(fail=True))
    persisted=json.loads(state.read_text())
    assert out['status']=='ERROR'
    assert persisted.get('last_cycle_day') is None
    assert persisted['last_attempt_day']=='2026-09-07'


def test_backup_status_empty_is_explicit(tmp_path):
    from app.v121_backup import backup_status
    assert backup_status(tmp_path/'missing.json')['status']=='OFF-BOX BACKUP NOT CONFIGURED'


def test_backup_error_is_throttled_before_retry_window(tmp_path):
    from app.v121_backup import run_daily_backup_cycle
    root=tmp_path/'index_vol'; root.mkdir()
    (root/'2026-09-07_micro.jsonl.gz').write_bytes(b'x')
    state=tmp_path/'backup.json'
    state.write_text(json.dumps({'status':'ERROR','last_cycle_at':'2026-09-07T16:05:00','last_attempt_day':'2026-09-07'}),encoding='utf-8')
    calls=[]
    out=run_daily_backup_cycle(root,state,now=dt.datetime(2026,9,7,16,10),bucket='b',prefix='p',endpoint_url='',region='ap-south-1',client_factory=lambda **kw:calls.append(1) or FakeS3())
    assert out['status']=='ERROR'
    assert calls==[]
