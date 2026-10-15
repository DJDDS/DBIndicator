import json

from app import v133_direction as d
from app import v137_pool_watch as f

S = dict(f.DEFAULT_SETTINGS, add_rs=True)   # most tests exercise the relative-strength add-in explicitly
DAY = '2026-10-15'


def _polls(series, times, crowd=None):
    """series: symbol -> prices per time; crowd: optional market move (%) per time applied to 40 filler stocks."""
    out = []
    for i, t in enumerate(times):
        q = {s: [px[i], 1000] for s, px in series.items()}
        for k in range(40): q[f'M{k}'] = [100 * (1 + (crowd[i] if crowd else 0) / 100), 1000]
        out.append({'t': f'{DAY}T{t}:00', 'q': q})
    return out


def _day925(status=('OPEN', 'WATCH', 'WATCH')):
    picks = [{'symbol': f'P{i}', 'side': 'DOWN', 'entry': 100.0, 'conf': 0.15, 'grade': 'A' if i == 0 else 'B', 'status': status[i], 'pod': i == 0,
              'rng20_pct': 2.0, 'entry_check': {'label': 'GOOD', 'score': 3}, 'of_agree': True} for i in range(3)]
    return {'date': DAY, 'at': f'{DAY} 09:27:05.1', 'settings': {'stop_k': 1.0, 'trail_k': 0.75}, 'lists': {'hc': {'picks': picks}}}


def _rs(hm, picks):
    return {'date': DAY, 'slots': {hm: {'picks': picks}}}


def test_pool_grows_with_strong_picks_up_to_the_cap():
    h = {'picks': [{'symbol': 'H1', 'side': 'UP', 'entry': 50.0, 'entry_poll': f'{DAY}T09:45:02', 'role': 'PICK'},
                   {'symbol': 'H2', 'side': 'UP', 'entry': 50.0, 'entry_poll': f'{DAY}T09:45:02', 'role': 'SHADOW'}]}
    rs = _rs('11:15', [{'symbol': 'R1', 'side': 'UP', 'entry': 10.0, 'z': 4.0, 'vs_mkt_pct': 2.0, 'grade': 'A'},
                       {'symbol': 'R2', 'side': 'DOWN', 'entry': 10.0, 'z': -3.1, 'vs_mkt_pct': -1.0, 'grade': 'B'},
                       {'symbol': 'R3', 'side': 'UP', 'entry': 10.0, 'z': 3.6, 'vs_mkt_pct': 1.5, 'grade': 'A'}])
    items, notes = f.merge([], f.candidates(_day925(), h, rs, S, {'H1': 1.5, 'R1': 3.0}), S)
    assert [i['symbol'] for i in items] == ['P0', 'P1', 'P2', 'H1', 'R1']
    assert [i['trade'] for i in items[:3]] == [True, False, False]
    assert items[3]['stop_pct'] == 1.5 and items[4]['trail_pct'] == 2.25          # add-ins get a range-sized stop too
    assert any(n['symbol'] == 'R3' and n['kind'] == 'full' for n in notes)


def test_a_stopped_stock_frees_its_slot_for_a_fresh_signal_at_the_current_price():
    rs = _rs('11:15', [{'symbol': 'R1', 'side': 'UP', 'entry': 10.0, 'z': 4.0, 'vs_mkt_pct': 2.0, 'grade': 'A'},
                       {'symbol': 'R3', 'side': 'UP', 'entry': 10.0, 'z': 3.6, 'vs_mkt_pct': 1.5, 'grade': 'A'}])
    s = dict(S, max_items=4)
    items, notes = f.merge([], f.candidates(_day925(), None, rs, s), s, [], f'{DAY}T11:16:00')
    assert [i['symbol'] for i in items] == ['P0', 'P1', 'P2', 'R1']
    items[1]['track'] = {'stopped': True}
    items, new = f.merge(items, f.candidates(_day925(), None, rs, s), s, notes, f'{DAY}T11:50:00', {'R3': 10.4})
    r3 = items[-1]
    assert r3['symbol'] == 'R3' and r3['late'] and r3['entry'] == 10.4 and r3['added'] == f'{DAY}T11:50:00'
    items[2]['track'] = {'stopped': True}
    rs2 = _rs('11:15', rs['slots']['11:15']['picks'] + [{'symbol': 'R4', 'side': 'UP', 'entry': 9.0, 'z': 3.7, 'vs_mkt_pct': 1, 'grade': 'A'}])
    items, n2 = f.merge(items, f.candidates(_day925(), None, rs2, s), s, notes + new, f'{DAY}T13:30:00', {'R4': 9.5})
    assert items[-1]['symbol'] == 'R4' and not items[-1].get('late')            # never turned away, so it keeps its own entry
    s5 = dict(s, max_day_items=6)
    items, n3 = f.merge(items, f.candidates(_day925(), None, _rs('11:15', [{'symbol': 'R9', 'side': 'UP', 'entry': 1, 'z': 5, 'vs_mkt_pct': 1, 'grade': 'A'}]), s5), s5, [], f'{DAY}T13:31:00')
    assert all(i['symbol'] != 'R9' for i in items) and n3[0]['kind'] == 'full'    # day cap reached


def test_stale_signal_does_not_join_late():
    rs = _rs('11:15', [{'symbol': 'R1', 'side': 'UP', 'entry': 10.0, 'z': 4.0, 'vs_mkt_pct': 2.0, 'grade': 'A'}])
    s = dict(S, max_items=3)
    items, notes = f.merge([], f.candidates(_day925(), None, rs, s), s, [], f'{DAY}T11:16:00')
    items[0]['track'] = {'stopped': True}
    items, new = f.merge(items, f.candidates(_day925(), None, rs, s), s, notes, f'{DAY}T13:00:00', {'R1': 11})
    assert len(items) == 3 and new[0]['kind'] == 'stale'


def test_existing_items_never_change_and_opposite_calls_are_noted():
    items, _ = f.merge([], f.candidates(_day925(), None, None, S), S)
    before = [dict(i) for i in items]
    rs = _rs('12:15', [{'symbol': 'P0', 'side': 'UP', 'entry': 99.0, 'z': 4.0, 'vs_mkt_pct': 2.0, 'grade': 'A'}])
    items, notes = f.merge(items, f.candidates(_day925(), None, rs, S), S)
    assert items == before and notes[0]['kind'] == 'conflict'


def test_track_follows_live_books_stops_guards_and_closes_at_the_exit():
    it = f.candidates(_day925(), None, None, S)[0]                        # DOWN from 100, stop 2%, trail 1.5%
    times = ['09:28', '10:00', '11:00', '12:00', '15:10', '15:15']
    mid = f.track(it, _polls({'P0': [100, 99.5, 98.8, 99.0]}, times[:4]), '15:15', 1.0)
    assert mid['now_pct'] == 1.0 and mid['booked'] and not mid.get('closed')
    early = f.track(it, _polls({'P0': [100, 99.5, 98.8, 99.0, 99.2]}, times[:5]), '15:15', 1.0)
    assert not early.get('closed')
    out = f.track(it, _polls({'P0': [100, 99.5, 98.8, 99.0, 99.2, 99.4]}, times), '15:15', 1.0)
    assert out['closed'] and abs(out['ret_pct'] - 0.54) < 1e-6 and out['win']
    assert out['book_ret_pct'] == 0.94 and out['best_pct'] == 1.2 and not out['stopped'] and not out['guarded'] and out['guard_ret_pct'] == out['ret_pct']
    st = f.track(dict(it, side='UP'), _polls({'P0': [100, 99, 97.9, 98, 99, 100]}, times), '15:15', 1.0)
    assert st['stopped'] and st['stopped_at'] == '11:00' and st['stop_ret_pct'] < 0
    g = f.track(it, _polls({'P0': [100, 99.8, 100.2, 100.5, 101, 101]}, times, crowd=[0, 0.2, 0.6, 0.8, 1, 1]), '15:15', 1.0, 0.5)
    assert g['guarded'] and g['guarded_at'] == '11:00' and g['guard_ret_pct'] > g['ret_pct'] and g['mkt_pct'] == 1.0


def test_scorecard_counts_traded_watch_and_guard():
    def item(trade, r, gr=None): return {'symbol': 'X', 'side': 'UP', 'source': '09:25 pool', 'trade': trade,
                                         'track': {'closed': True, 'ret_pct': r, 'book_ret_pct': r, 'guard_ret_pct': gr if gr is not None else r, 'win': r > 0}}
    sc = f.scorecard([{'items': [item(True, 0.5), item(False, -0.2)]}, {'items': [item(True, -0.3, 0.1)]}])
    assert sc['hold']['n'] == 2 and sc['hold']['win'] == 50.0 and sc['watch']['n'] == 1 and sc['ge1_day'] == 50.0 and sc['guard']['win'] == 100.0


def test_weak_entry_is_watched_not_traded():
    day = _day925(status=('OPEN', 'OPEN', 'WATCH'))
    day['lists']['hc']['picks'][1]['entry_check'] = {'label': 'WEAK', 'score': 0}
    assert [x['trade'] for x in f.candidates(day, None, None, S)] == [True, False, False]


def test_breakdown_splits_the_live_ledger():
    p = lambda g, ec, r, of: {'grade': g, 'status': 'OPEN', 'entry_check': {'label': ec}, 'of_agree': of, 'ret_pct': r, 'book_ret_pct': r, 'pod': g == 'A'}  # noqa: E731
    days = [{'lists': {'hc': {'strong': True, 'picks': [p('A', 'GOOD', 0.5, True), p('B', 'WEAK', -0.4, False)]}}},
            {'lists': {'hc': {'strong': False, 'picks': [p('A', 'GOOD', 0.3, True), p('B', 'GOOD', -0.1, True)]}}}]
    b = {r['group']: r for r in f.pool_breakdown(days)}
    assert b['all pool picks']['n'] == 4 and b['grade A']['hold']['win'] == 100.0 and b['not WEAK (new rule)']['hold']['win'] == 66.7


def test_lite_polls_reads_only_new_lines(tmp_path, monkeypatch):
    monkeypatch.setattr(d, 'ROOT', tmp_path); d._lite.clear()
    p = tmp_path / f'of_{DAY}.jsonl'
    row = lambda t, px: json.dumps({'t': f'{DAY}T{t}', 'ph': 'slow', 'q': {'A': [px, 5, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 99]}}) + '\n'  # noqa: E731
    p.write_text(row('10:00:00', 100) + row('10:05:00', 101)[:20])                 # second line still being written
    assert [x['q']['A'] for x in d.lite_polls(DAY)] == [[100, 5, 99]]
    with p.open('a') as fh: fh.write(row('10:05:00', 101)[20:] + row('10:10:00', 102))
    got = d.lite_polls(DAY)
    assert [x['q']['A'][0] for x in got] == [100, 101, 102] and got[1]['t'] == f'{DAY}T10:05:00'


def test_up_pool_picks_can_exit_earlier():
    day = _day925(); day['lists']['hc']['picks'][2]['side'] = 'UP'
    s = dict(S, up_exit_at='11:25')
    c = f.candidates(day, None, None, s)
    assert 'exit_at' not in c[0] and c[2]['exit_at'] == '11:25'
    times = ['09:28', '10:00', '11:25', '12:00', '15:15']
    out = f.track(c[2], _polls({'P2': [100, 100.5, 100.8, 99.0, 98.0]}, times), c[2]['exit_at'], 1.0)
    assert out['closed'] and out['exit_px'] == 100.8 and out['win']


def test_relative_strength_does_not_join_by_default():
    rs = _rs('11:15', [{'symbol': 'R1', 'side': 'UP', 'entry': 10.0, 'z': 4.0, 'vs_mkt_pct': 2.0, 'grade': 'A'}])
    assert f.DEFAULT_SETTINGS['add_rs'] is False
    assert all(c['source'] != '11:15 strength' for c in f.candidates(_day925(), None, rs, dict(f.DEFAULT_SETTINGS)))
