"""Recorder monitor: one read-only table that says, for every recorder / paper ledger, whether it is alive and writing.

Nothing here places orders or writes anything. Every row is computed from the recorder's own files (newest write time, record count),
its scheduler thread (alive or not) and the last step result it reported. Rules:

  daily  - writes once per trading day after ``due`` (IST). OK if the newest write is at/after the most recent due time,
           DUE for the first ``grace`` minutes after it, otherwise MISSED. Weekend: the last weekday's due time is used.
           A weekday market holiday therefore shows MISSED - read it together with the NSE holiday list.
  live   - streams during 09:15-15:30. OK if written in the last 10 minutes; STALE if not; outside market hours it shows the last write.
  event  - written only when something happens (a ledger). Never stale; shows the last event time.
"""
from __future__ import annotations

import datetime as dt
import gzip
import os
import re
import threading
from pathlib import Path

IST = dt.timezone(dt.timedelta(hours=5, minutes=30))
LINE_COUNT_MAX_BYTES = 3_000_000   # counting lines of big files on the data volume is slow; only small files get a record count


def _alive_names() -> set[str]:
    return {t.name for t in threading.enumerate() if t.is_alive()}


def _now() -> dt.datetime:
    return dt.datetime.now(IST)


def _stat(path: Path):
    try:
        return path.stat()
    except OSError:
        return None


def _gather(root: Path, patterns: list[str]) -> list[Path]:
    out: list[Path] = []
    for pat in patterns:
        try:
            out += [p for p in root.glob(pat) if p.is_file()]
        except (OSError, ValueError):
            continue
    return out


def _count_lines(path: Path) -> int | None:
    st = _stat(path)
    if st is None or st.st_size > LINE_COUNT_MAX_BYTES:
        return None
    try:
        op = gzip.open if path.suffix == '.gz' else open
        with op(path, 'rb') as fh:
            return sum(1 for _ in fh)
    except (OSError, EOFError):
        return None


def _last_due(now: dt.datetime, due: dt.time) -> dt.datetime:
    """Most recent weekday due moment that is <= now."""
    d = now.date()
    cand = dt.datetime.combine(d, due, tzinfo=IST)
    while d.weekday() >= 5 or cand > now:
        d -= dt.timedelta(days=1)
        cand = dt.datetime.combine(d, due, tzinfo=IST)
    return cand


def _opening_grace(now: dt.datetime, last_write: dt.datetime) -> bool:
    """First 12 minutes after the 09:15 open: a stream that has not written yet today is starting up, not stale."""
    open_at = dt.datetime.combine(now.date(), dt.time(9, 15), tzinfo=IST)
    return now.weekday() < 5 and open_at <= now < open_at + dt.timedelta(minutes=12) and last_write < open_at


def _market_open(now: dt.datetime) -> bool:
    return now.weekday() < 5 and dt.time(9, 15) <= now.time() <= dt.time(15, 30)


# name, group, what it records, kind, due (daily), grace minutes, file patterns (relative to storage root), scheduler thread
def registry(storage_root: str, index_vol_root: str | None = None) -> list[dict]:
    r = []
    add = lambda **k: r.append(k)
    add(name='Swing desk evening plan', group='V13.0 Swing desk', what='Tomorrow\'s gate, picks and rejected stocks (plans.jsonl)',
        kind='daily', due=dt.time(18, 45), grace=105, files=['v130/plans.jsonl'], thread='v130-swing')
    add(name='Swing desk ledger', group='V13.0 Swing desk', what='Entries, exits, skips, option instructions, settings changes',
        kind='event', files=['v130/ledger.jsonl'], thread='v130-swing')
    add(name='Pre-open snapshot', group='V13.1 Intraday lanes', what='09:00-09:08 pre-open prices of the F&O universe',
        kind='daily', due=dt.time(9, 10), grace=20, files=['v131/preopen.jsonl'], thread='v131-intraday')
    add(name='Gap-down bounce basket', group='V13.1 Intraday lanes', what='Market gap, active or not, basket and 09:45 / 10:15 exits',
        kind='daily', due=dt.time(9, 20), grace=25, files=['v131/gaps.jsonl'], thread='v131-intraday')
    add(name='09:25 short detector', group='V13.1 Intraday lanes', what='Three shorts at 09:25 and the 15:15 cover',
        kind='daily', due=dt.time(9, 30), grace=25, files=['v131/shorts.jsonl'], thread='v131-intraday')
    add(name='09:16 movement predictor', group='V13.2 Movement predictor', what='Top-3 stocks likely to move unusually, 09:45 final pick, result, paper ATM straddle entries and exits',
        kind='daily', due=dt.time(9, 25), grace=25, files=['v132/picks.jsonl'], thread='v132-movers')
    add(name='09:25 direction desk', group='V13.3 Direction desk', what='UP/DOWN lists for 30/60/90/120 min and the day',
        kind='daily', due=dt.time(9, 35), grace=25, files=['v133/days.jsonl', 'v133/scores_*.json.gz'], thread='v133-direction')
    add(name='V13.4 relative-strength desk', group='V13.4 Relative-strength desk', what='11:15 / 12:15 / 13:15 picks far stronger or weaker than the market, 15:15 results',
        kind='daily', due=dt.time(11, 20), grace=60, files=['v133/rs_days.jsonl'], thread='v133-orderflow')
    add(name='Order-flow lab (polls, bars, futures, scores)', group='V13.3 Direction desk', what='Every day\'s order-flow polls, 5-minute bars, futures and 09:25 model scores - the data for the research formula fit',
        kind='event', files=['v133/of_*.jsonl.gz', 'v133/of_*.jsonl', 'v133/of_*.json', 'v133/bars_*.json.gz', 'v133/futs_*.json', 'v133/of_lab.json'], thread=None)
    add(name='NG desk ledger', group='MCX commodity desk', what='Natural gas playbook days, paper results and MCX ledger',
        kind='event', files=['commodity_desk/ledger.jsonl', 'commodity_desk/mcx_ledger.jsonl', 'commodity_desk/year_round.json', 'commodity_desk/sensitivity.json'], thread=None)
    add(name='Setup A v1.1 scan', group='Setup A dip-buy', what='Nightly NIFTY-500 stress score, signals and paper ledger',
        kind='daily', due=dt.time(19, 15), grace=195, files=['v11/runs.jsonl', 'v11/summary.json'], thread='v11-setup-a')
    add(name='Setup A WIDE ledger', group='Setup A dip-buy', what='Second paper ledger with relaxed gates (F&O only)',
        kind='event', files=['v11/v11w_paper_ledger.csv', 'v11/summary_wide.json'], thread='v11-setup-a')
    add(name='Option edge recorder', group='V12 option research', what='Option snapshots at 09:30, 13:00, 15:10 and 15:37',
        kind='daily', due=dt.time(9, 40), grace=30, files=['v12_option_snapshots.jsonl', 'v12/v12_option_snapshots.jsonl'], thread=None)
    add(name='Skew validation recorder', group='V12 option research', what='ATM call IV minus put IV at 09:30 and 13:00',
        kind='daily', due=dt.time(9, 40), grace=30, files=['v123_skew_shadow.jsonl'], thread=None)
    add(name='Trial 25 event recorder', group='V12 option research', what='Earnings-event shadow trades and raw quotes',
        kind='event', files=['trial25/trial25_event_ledger.jsonl', 'trial25/trial25_universe_ledger.jsonl', 'v12_earnings_ledger.jsonl'], thread=None)
    add(name='Index volatility recorder', group='V12 option research', what='NIFTY option micro-structure and depth, 5-second WebSocket',
        kind='live', files=['__INDEX_VOL__/*_micro.jsonl', '__INDEX_VOL__/*_depth.jsonl', 'v121_index_vol_state.json'], thread=None)
    add(name='Math recorder (quant regime shadow)', group='V12 option research', what='Daily features and regimes of the F&O universe',
        kind='daily', due=dt.time(15, 40), grace=120, files=['v123_quant_regime_shadow/features_*.jsonl.gz', 'v123_quant_regime_shadow/regimes_*.jsonl.gz'], thread=None)
    add(name='CALL V1 / tactical event recorder', group='V12 option research', what='Tactical stock-option events and CALL V1 forward records',
        kind='event', files=['v122b_tactical_events.jsonl', 'v122b_tactical_state.json'], thread=None)
    add(name='Universe live stream checkpoint', group='V12 option research', what='Full F&O underlying quote stream checkpoint',
        kind='live', files=['v123_market_checkpoint.json'], thread=None)
    return r


def resolve_files(spec: dict, root: Path, iv_root: Path | None) -> list[Path]:
    """The real files behind one recorder row (whitelist = the registry patterns only; nothing else on the volume is reachable)."""
    files: list[Path] = []
    for p in spec['files']:
        if p.startswith('__INDEX_VOL__/'):
            if iv_root is not None: files += _gather(iv_root, [p.split('/', 1)[1]])
        else:
            files += _gather(root, [p])
    return sorted(set(files))


def export_zip(storage_root: str, index_vol_root: str | None, idx: int | None, dest_dir: str | None = None):
    """Zip one recorder (idx) or all recorders (idx None) into a temp file. Returns (path, download_name, file_count) or None when nothing exists yet.
    Stored paths are relative to the data volume; the caller deletes the temp file after sending."""
    import tempfile, zipfile
    root = Path(storage_root); iv = Path(index_vol_root) if index_vol_root else None
    specs = registry(storage_root, index_vol_root)
    if idx is not None:
        if not 0 <= idx < len(specs): return None
        specs = [specs[idx]]
    names: dict[str, Path] = {}
    for sp in specs:
        for f in resolve_files(sp, root, iv):
            try: arc = str(f.relative_to(root))
            except ValueError: arc = 'index_vol/' + f.name
            names.setdefault(arc, f)
    if not names: return None
    fd, tmp = tempfile.mkstemp(suffix='.zip', prefix='recorders_', dir=dest_dir if dest_dir and Path(dest_dir).is_dir() else None); os.close(fd)
    with zipfile.ZipFile(tmp, 'w', zipfile.ZIP_DEFLATED) as z:
        for arc, f in sorted(names.items()):
            try: z.write(f, arc)
            except OSError: continue
    label = 'all_recorders' if idx is None else re.sub(r'[^A-Za-z0-9]+', '_', specs[0]['name']).strip('_').lower()
    return tmp, f"{label}_{_now():%Y%m%d}.zip", len(names)


def _evaluate(spec: dict, root: Path, iv_root: Path | None, now: dt.datetime) -> dict:
    pats = []
    for p in spec['files']:
        if p.startswith('__INDEX_VOL__/'):
            if iv_root is not None:
                pats.append(('abs', iv_root, p.split('/', 1)[1]))
        else:
            pats.append(('rel', root, p))
    files: list[Path] = []
    for _, base, pat in pats:
        files += _gather(base, [pat])
    files = sorted(set(files))
    sts = [(f, _stat(f)) for f in files]
    sts = [(f, s) for f, s in sts if s is not None]
    newest = max(sts, key=lambda x: x[1].st_mtime) if sts else None
    last_write = dt.datetime.fromtimestamp(newest[1].st_mtime, IST) if newest else None
    size = sum(s.st_size for _, s in sts)
    records = None
    jl = [f for f, _ in sts if '.jsonl' in f.name or f.suffix == '.csv']
    if jl and spec['kind'] != 'live':
        counts = [_count_lines(f) for f in jl]
        records = sum(c for c in counts if c is not None) if any(c is not None for c in counts) else None
    kind = spec['kind']; status = 'OK'; note = ''
    if not sts:
        status = 'NO DATA YET'; note = 'no file written yet - appears after the first scheduled run'
        if kind == 'event':
            status = 'WAITING'; note = 'event-based: the file is created on the first event'
    elif kind == 'daily':
        due = _last_due(now, spec['due']); mins = (now - due).total_seconds() / 60
        if last_write >= due: status = 'OK'; note = f"written {last_write:%a %d-%b %H:%M} IST"
        elif mins <= spec['grace']: status = 'DUE'; note = f"due at {due:%H:%M}; checking for {spec['grace']} min"
        else: status = 'MISSED'; note = f"expected after {due:%a %d-%b %H:%M}; last write {last_write:%a %d-%b %H:%M} (a market holiday shows as MISSED)"
    elif kind == 'live':
        if _market_open(now):
            age = (now - last_write).total_seconds() / 60
            if _opening_grace(now, last_write): status = 'DUE'; note = f"market just opened; waiting for today's first write (last write {last_write:%a %d-%b %H:%M})"
            else: status = 'OK' if age <= 10 else 'STALE'; note = f"last write {age:.0f} min ago" if age >= 1 else 'writing now'
        else: status = 'IDLE'; note = f"market closed; last write {last_write:%a %d-%b %H:%M} IST"
    else:
        status = 'OK'; note = f"last event {last_write:%a %d-%b %H:%M} IST"
    thread_alive = None
    if spec.get('thread'):
        thread_alive = spec['thread'] in _alive_names()
        if not thread_alive and status in ('OK', 'DUE', 'IDLE'):
            status = 'NOT RUNNING'; note = f"scheduler thread {spec['thread']} is not alive - restart the service. {note}"
    return {'id': spec.get('_id'), 'name': spec['name'], 'group': spec['group'], 'what': spec['what'], 'kind': kind, 'status': status, 'note': note,
            'last_write': last_write.isoformat(timespec='seconds') if last_write else None, 'files': len(sts), 'size_kb': round(size / 1024, 1),
            'records': records, 'thread': spec.get('thread'), 'thread_alive': thread_alive}


def scheduler_steps() -> dict:
    """Last step results the V13 / Setup A schedulers reported (ok flag, time, error)."""
    out: dict = {}
    for mod, label in (('v130_swing', 'V13.0 swing desk'), ('v131_intraday', 'V13.1 intraday lanes'), ('v132_movers', 'V13.2 movement predictor'),
                       ('v133_direction', 'V13.3 direction desk'), ('v11_setup_a', 'Setup A')):
        try:
            m = __import__(f'app.{mod}', fromlist=['_status'])
            st = getattr(m, '_status', {}) or {}
            steps = {k: {'ok': v.get('ok'), 'at': str(v.get('at'))[:19], 'error': v.get('error')} for k, v in (st.get('steps') or {}).items() if isinstance(v, dict)}
            out[label] = {'started': str(st.get('started'))[:19] if st.get('started') else None, 'last_error': st.get('last_error'), 'steps': steps}
        except Exception as exc:  # noqa: BLE001
            out[label] = {'started': None, 'last_error': f'cannot read: {exc}', 'steps': {}}
    return out


def _expected_polls(now: dt.datetime) -> int:
    """Polls the V13.3 order-flow recorder should have made so far today: 60 s from 09:08, 10 s from 09:15, 5 min from 09:40 to 15:15."""
    if now.weekday() >= 5: return 0
    m = now.hour * 60 + now.minute + now.second / 60
    n = 0.0
    n += max(0.0, min(m, 555) - 548) / 1.0                       # 09:08-09:15, every 60 s
    n += max(0.0, min(m, 580) - 555) * 6                         # 09:15-09:40, every 10 s
    n += max(0.0, min(m, 915) - 580) / 5.0                       # 09:40-15:15, every 5 min
    return int(n)


def orderflow(storage_root: str, now: dt.datetime | None = None) -> dict:
    """The two order-flow recorders: the V13.3 LAB (all F&O stocks + futures, judged after 40 sessions) and the V12.4 minute recorder."""
    now = now or _now(); root = Path(storage_root); today = now.date().isoformat(); out: dict = {}
    # --- V13.3 LAB
    lab = {'name': 'V13.3 order-flow LAB (all F&O stocks + near-month futures)', 'target': 40, 'sessions': 0, 'today_polls': 0, 'expected_polls': _expected_polls(now),
           'stocks_per_poll': None, 'futures_per_poll': None, 'first': None, 'last_poll': None, 'by_phase': {}, 'sessions_first': None, 'sessions_last': None, 'status': 'NO DATA YET', 'note': ''}
    try:
        from . import v133_direction as d
        s = d.of_status(today); ls = d.lab_summary()
        lab.update(sessions=int(ls.get('sessions') or 0), target=int(ls.get('target') or 40), sessions_first=ls.get('first'), sessions_last=ls.get('last'),
                   today_polls=int(s.get('polls') or 0), stocks_per_poll=s.get('stocks'), futures_per_poll=s.get('futures'), first=s.get('first'), last_poll=s.get('last'), by_phase=s.get('by_phase') or {})
    except Exception as exc:  # noqa: BLE001
        lab['note'] = f'cannot read: {exc}'
    files = _gather(root / 'v133', ['of_20*.jsonl', 'of_20*.jsonl.gz', 'of_20*.json'])
    days = sorted({f.name[3:13] for f in files})
    lab['files_days'] = len(days)
    if lab['sessions'] == 0 and days: lab['sessions'] = len([x for x in days if x != today])    # leaderboard not built yet: count finished days
    open_now = now.weekday() < 5 and dt.time(9, 8) <= now.time() <= dt.time(15, 15)
    thread_ok = 'v133-orderflow' in _alive_names()
    if now.weekday() >= 5: lab['status'] = 'IDLE'; lab['note'] = 'weekend'
    elif now.time() < dt.time(9, 8): lab['status'] = 'WAITING'; lab['note'] = 'polling starts 09:08'
    elif open_now:
        gap = lab['expected_polls'] - lab['today_polls']
        if not thread_ok: lab['status'] = 'NOT RUNNING'; lab['note'] = 'order-flow thread is not alive - restart the service'
        elif lab['today_polls'] == 0 and now.time() > dt.time(9, 12): lab['status'] = 'MISSED'; lab['note'] = 'no polls today - Kite login missing or quote call failing'
        elif gap > max(12, 0.25 * lab['expected_polls']): lab['status'] = 'BEHIND'; lab['note'] = f"{lab['today_polls']} polls vs ~{lab['expected_polls']} expected by now (a restart or login gap loses polls)"
        else: lab['status'] = 'OK'; lab['note'] = f"{lab['today_polls']} polls so far (~{lab['expected_polls']} expected)"
    else:
        lab['status'] = 'OK' if lab['today_polls'] else 'MISSED'; lab['note'] = f"market closed; {lab['today_polls']} polls recorded today"
    out['lab'] = lab
    # --- V12.4 minute recorder (tactical stream)
    p = root / 'v124_orderflow_minutes.jsonl'; st = _stat(p)
    m = {'name': 'V12.4 minute order-flow recorder (focus-list stocks, 5-level book)', 'file': p.name, 'size_kb': round(st.st_size / 1024, 1) if st else 0, 'last_write': None, 'lines': None, 'status': 'NO DATA YET', 'note': ''}
    if st:
        lw = dt.datetime.fromtimestamp(st.st_mtime, IST); m['last_write'] = lw.isoformat(timespec='seconds'); m['lines'] = _count_lines(p)
        if _market_open(now) and _opening_grace(now, lw):
            m['status'] = 'DUE'; m['note'] = f"market just opened; waiting for today's first minute rows (last write {lw:%a %d-%b %H:%M})"
        elif _market_open(now):
            age = (now - lw).total_seconds() / 60
            m['status'] = 'OK' if age <= 10 else 'STALE'; m['note'] = f"last minute row {age:.0f} min ago" + ('' if age <= 10 else ' - the tactical stream may be down or the focus list empty')
        else: m['status'] = 'IDLE'; m['note'] = f"market closed; last write {lw:%a %d-%b %H:%M} IST"
    else: m['note'] = 'written only while the tactical stream has focus-list ticks; the file appears on the first session with ticks'
    out['minute'] = m
    return out


def report(storage_root: str, index_vol_root: str | None = None, now: dt.datetime | None = None) -> dict:
    now = now or _now(); root = Path(storage_root); iv = Path(index_vol_root) if index_vol_root else None
    rows = [_evaluate({**s, '_id': i}, root, iv, now) for i, s in enumerate(registry(storage_root, index_vol_root))]
    count = lambda *names: sum(1 for r in rows if r['status'] in names)
    return {'as_of': now.isoformat(timespec='seconds'), 'storage': str(root), 'rows': rows, 'schedulers': scheduler_steps(), 'orderflow': orderflow(storage_root, now),
            'summary': {'total': len(rows), 'ok': count('OK', 'IDLE'), 'waiting': count('DUE', 'WAITING', 'NO DATA YET'), 'problems': count('MISSED', 'STALE', 'NOT RUNNING')}}


# --- cached, single-flight access for the web route ---------------------------------------------------------
# The report touches many files on the data volume. It must never run inside a web request, or slow calls pile up and
# starve the worker threads (the whole app crawled when it ran every 30 s per open tab). One background thread
# refreshes it; the route only returns the last result at once.
import threading as _th
import time as _time

_CACHE: dict = {'rep': None, 'at': 0.0, 'busy': False, 'took': None, 'err': None}
_CLOCK = _th.Lock()
CACHE_TTL = 60.0


def _refresh(storage_root: str, index_vol_root: str | None) -> None:
    t0 = _time.time()
    try:
        rep = report(storage_root, index_vol_root)
        _CACHE.update(rep=rep, at=_time.time(), took=round(_time.time() - t0, 1), err=None)
    except Exception as exc:  # noqa: BLE001
        _CACHE.update(err=str(exc), at=_time.time(), took=round(_time.time() - t0, 1))
    finally:
        _CACHE['busy'] = False


def cached_report(storage_root: str, index_vol_root: str | None = None) -> dict:
    """Return the last report immediately; start a background refresh when it is older than CACHE_TTL."""
    with _CLOCK:
        stale = _time.time() - _CACHE['at'] > CACHE_TTL
        if stale and not _CACHE['busy']:
            _CACHE['busy'] = True
            _th.Thread(target=_refresh, args=(storage_root, index_vol_root), name='recorder-monitor', daemon=True).start()
    rep = _CACHE['rep']
    if rep is None:
        return {'as_of': None, 'rows': [], 'schedulers': {}, 'orderflow': {}, 'loading': True, 'error': _CACHE['err'],
                'summary': {'total': 0, 'ok': 0, 'waiting': 0, 'problems': 0}}
    out = dict(rep); out['loading'] = False; out['refreshing'] = _CACHE['busy']; out['took_s'] = _CACHE['took']; out['error'] = _CACHE['err']
    return out
