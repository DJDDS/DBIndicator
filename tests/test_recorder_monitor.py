import datetime as dt, json, os, threading, time
from pathlib import Path
from app import recorder_monitor as m

IST = m.IST


import pytest


@pytest.fixture(autouse=True)
def _threads(monkeypatch, request):
    if 'dead_scheduler' not in request.node.name:
        monkeypatch.setattr(m, '_alive_names', lambda: {'v130-swing', 'v131-intraday', 'v132-movers', 'v133-direction', 'v133-orderflow', 'v11-setup-a'})


def _touch(p: Path, when: dt.datetime, text='{"a":1}\n'):
    p.parent.mkdir(parents=True, exist_ok=True); p.write_text(text)
    ts = when.timestamp(); os.utime(p, (ts, ts))


def _row(rep, name):
    return next(r for r in rep['rows'] if r['name'] == name)


def test_daily_ok_missed_due_and_no_data(tmp_path):
    now = dt.datetime(2026, 10, 13, 10, 0, tzinfo=IST)               # Tuesday 10:00
    _touch(tmp_path / 'v131/shorts.jsonl', dt.datetime(2026, 10, 13, 9, 27, tzinfo=IST), '{"d":1}\n{"d":2}\n')   # after 09:30-ish due? due 09:30 -> written 09:27 is before
    _touch(tmp_path / 'v131/gaps.jsonl', dt.datetime(2026, 10, 13, 9, 22, tzinfo=IST))                           # after 09:20 due -> OK
    _touch(tmp_path / 'v132/picks.jsonl', dt.datetime(2026, 10, 12, 9, 30, tzinfo=IST))                          # yesterday only, due 09:25 +25 min grace passed at 09:50 -> MISSED
    rep = m.report(str(tmp_path), None, now)
    assert _row(rep, 'Gap-down bounce basket')['status'] == 'OK'
    assert _row(rep, '09:16 movement predictor')['status'] == 'MISSED'
    assert _row(rep, '09:25 short detector')['status'] in ('MISSED', 'OK')    # 09:27 < 09:30 due, 30 min later -> MISSED (grace 25)
    assert _row(rep, '09:25 short detector')['records'] == 2
    assert _row(rep, 'Pre-open snapshot')['status'] == 'NO DATA YET'
    assert rep['summary']['problems'] >= 1


def test_before_due_uses_previous_day_and_weekend(tmp_path):
    _touch(tmp_path / 'v130/plans.jsonl', dt.datetime(2026, 10, 12, 19, 5, tzinfo=IST))
    mon_night = dt.datetime(2026, 10, 13, 7, 40, tzinfo=IST)
    assert _row(m.report(str(tmp_path), None, mon_night), 'Swing desk evening plan')['status'] == 'OK'
    sat = dt.datetime(2026, 10, 17, 12, 0, tzinfo=IST)               # Saturday: last due was Fri 19:15, plan was only written Mon -> MISSED
    assert _row(m.report(str(tmp_path), None, sat), 'Swing desk evening plan')['status'] == 'MISSED'


def test_live_and_event_rules(tmp_path):
    _touch(tmp_path / 'v123_market_checkpoint.json', dt.datetime(2026, 10, 13, 10, 0, tzinfo=IST))
    _touch(tmp_path / 'v130/ledger.jsonl', dt.datetime(2026, 9, 1, 10, 0, tzinfo=IST))
    live_ok = m.report(str(tmp_path), None, dt.datetime(2026, 10, 13, 10, 5, tzinfo=IST))
    assert _row(live_ok, 'Universe live stream checkpoint')['status'] == 'OK'
    assert _row(live_ok, 'Swing desk ledger')['status'] == 'OK'      # events never go stale
    live_stale = m.report(str(tmp_path), None, dt.datetime(2026, 10, 13, 10, 40, tzinfo=IST))
    assert _row(live_stale, 'Universe live stream checkpoint')['status'] == 'STALE'
    closed = m.report(str(tmp_path), None, dt.datetime(2026, 10, 13, 18, 0, tzinfo=IST))
    assert _row(closed, 'Universe live stream checkpoint')['status'] == 'IDLE'


def test_dead_scheduler_thread_flagged(tmp_path, monkeypatch):
    monkeypatch.setattr(m, '_alive_names', lambda: set())      # deterministic: no scheduler thread alive
    _touch(tmp_path / 'v131/gaps.jsonl', dt.datetime(2026, 10, 13, 9, 22, tzinfo=IST))
    now = dt.datetime(2026, 10, 13, 10, 0, tzinfo=IST)
    assert _row(m.report(str(tmp_path), None, now), 'Gap-down bounce basket')['status'] == 'NOT RUNNING'   # no v131-intraday thread alive in the test
    monkeypatch.setattr(m, '_alive_names', lambda: {'v131-intraday'})                                         # thread alive -> OK
    assert _row(m.report(str(tmp_path), None, now), 'Gap-down bounce basket')['status'] == 'OK'


def test_route_and_page(tmp_path, monkeypatch):
    import base64
    from app import web
    monkeypatch.setenv("DBI_OWNER_PASSWORD", "t")
    monkeypatch.setattr(web.config, 'V12_STORAGE_ROOT', str(tmp_path))
    c = web.app.test_client()
    h = {"Authorization": "Basic " + base64.b64encode(b"admin:t").decode()}
    import time
    m._CACHE.update(rep=None, at=0.0, busy=False)
    r = c.get('/api/recorders', headers=h)                 # never blocks: first call may say 'loading'
    assert r.status_code == 200
    for _ in range(100):
        d = json.loads(c.get('/api/recorders', headers=h).data)
        if not d.get('loading'): break
        time.sleep(0.05)
    assert d['summary']['total'] == 16 and d['loading'] is False
    page = c.get('/backtest', headers=h)
    assert page.status_code == 200 and b'recorder-monitor' in page.data and b'/api/recorders' in page.data
    assert c.get('/api/recorders').status_code in (401, 403)


def test_orderflow_progress_and_expected_polls(tmp_path):
    assert m._expected_polls(dt.datetime(2026, 10, 13, 9, 0, tzinfo=IST)) == 0
    assert 140 < m._expected_polls(dt.datetime(2026, 10, 13, 9, 40, tzinfo=IST)) < 170          # 7 pre + ~150 fast
    assert m._expected_polls(dt.datetime(2026, 10, 13, 15, 30, tzinfo=IST)) > 220
    _touch(tmp_path / 'v124_orderflow_minutes.jsonl', dt.datetime(2026, 10, 13, 10, 58, tzinfo=IST), '{"m":1}\n{"m":2}\n')
    of = m.orderflow(str(tmp_path), dt.datetime(2026, 10, 13, 11, 0, tzinfo=IST))
    assert of['minute']['status'] == 'OK' and of['minute']['lines'] == 2
    assert of['lab']['target'] == 40 and of['lab']['status'] in ('OK', 'MISSED', 'BEHIND', 'NOT RUNNING')
    sat = m.orderflow(str(tmp_path), dt.datetime(2026, 10, 17, 11, 0, tzinfo=IST))
    assert sat['lab']['status'] == 'IDLE'


def test_stream_is_not_stale_in_the_first_minutes_after_the_open(tmp_path):
    _touch(tmp_path / 'v124_orderflow_minutes.jsonl', dt.datetime(2026, 10, 12, 15, 29, tzinfo=IST))
    _touch(tmp_path / 'v123_market_checkpoint.json', dt.datetime(2026, 10, 12, 15, 29, tzinfo=IST))
    early = dt.datetime(2026, 10, 13, 9, 18, tzinfo=IST)
    assert m.orderflow(str(tmp_path), early)['minute']['status'] == 'DUE'
    assert _row(m.report(str(tmp_path), None, early), 'Universe live stream checkpoint')['status'] == 'DUE'
    late = dt.datetime(2026, 10, 13, 9, 40, tzinfo=IST)                       # still nothing written 25 min after the open -> a real problem
    assert m.orderflow(str(tmp_path), late)['minute']['status'] == 'STALE'
    assert _row(m.report(str(tmp_path), None, late), 'Universe live stream checkpoint')['status'] == 'STALE'


def test_cached_report_does_not_block_a_slow_scan(tmp_path, monkeypatch):
    import threading, time
    gate = threading.Event()
    monkeypatch.setattr(m, 'report', lambda *a, **k: (gate.wait(5), {'as_of': 'x', 'rows': [], 'summary': {}})[1])
    m._CACHE.update(rep=None, at=0.0, busy=False)
    t0 = time.time(); a = m.cached_report(str(tmp_path)); b = m.cached_report(str(tmp_path))
    assert time.time() - t0 < 0.5 and a['loading'] and b['loading']                  # slow scan runs in the background, one at a time
    gate.set()
    for _ in range(100):
        if m._CACHE['rep'] is not None: break
        time.sleep(0.05)
    assert m.cached_report(str(tmp_path))['loading'] is False
