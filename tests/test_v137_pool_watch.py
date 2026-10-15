from app import v137_pool_watch as f

S = dict(f.DEFAULT_SETTINGS)
DAY = '2026-10-15'


def _polls(series, times):
    return [{'t': f'{DAY}T{t}:00', 'q': {s: [px[i]] for s, px in series.items()}} for i, t in enumerate(times)]


def _day925(status=('OPEN', 'WATCH', 'WATCH')):
    picks = [{'symbol': f'P{i}', 'side': 'DOWN', 'entry': 100.0, 'conf': 0.15, 'grade': 'A' if i == 0 else 'B', 'status': status[i], 'pod': i == 0,
              'rng20_pct': 2.0, 'entry_check': {'label': 'GOOD', 'score': 3}, 'of_agree': True} for i in range(3)]
    return {'date': DAY, 'at': f'{DAY} 09:27:05.1', 'settings': {'stop_k': 1.0, 'trail_k': 0.75}, 'lists': {'hc': {'picks': picks}}}


def test_pool_grows_with_strong_picks_up_to_the_cap():
    h = {'picks': [{'symbol': 'H1', 'side': 'UP', 'entry': 50.0, 'entry_poll': f'{DAY}T09:45:02', 'role': 'PICK'},
                   {'symbol': 'H2', 'side': 'UP', 'entry': 50.0, 'entry_poll': f'{DAY}T09:45:02', 'role': 'SHADOW'}]}
    rs = {'date': DAY, 'slots': {'11:15': {'picks': [{'symbol': 'R1', 'side': 'UP', 'entry': 10.0, 'z': 4.0, 'vs_mkt_pct': 2.0, 'grade': 'A'},
                                                     {'symbol': 'R2', 'side': 'DOWN', 'entry': 10.0, 'z': -3.1, 'vs_mkt_pct': -1.0, 'grade': 'B'},
                                                     {'symbol': 'R3', 'side': 'UP', 'entry': 10.0, 'z': 3.6, 'vs_mkt_pct': 1.5, 'grade': 'A'}]}}}
    items, notes = f.merge([], f.candidates(_day925(), h, rs, S), 5)
    assert [i['symbol'] for i in items] == ['P0', 'P1', 'P2', 'H1', 'R1']
    assert [i['trade'] for i in items[:3]] == [True, False, False]
    assert any(n['symbol'] == 'R3' and 'full' in n['note'] for n in notes)


def test_existing_items_never_change_and_opposite_calls_are_noted():
    items, _ = f.merge([], f.candidates(_day925(), None, None, S), 5)
    before = [dict(i) for i in items]
    rs = {'date': DAY, 'slots': {'12:15': {'picks': [{'symbol': 'P0', 'side': 'UP', 'entry': 99.0, 'z': 4.0, 'vs_mkt_pct': 2.0, 'grade': 'A'}]}}}
    items, notes = f.merge(items, f.candidates(_day925(), None, rs, S), 5)
    assert items == before and 'not added' in notes[0]['note']


def test_track_follows_live_books_stops_and_closes_at_the_exit():
    it = f.candidates(_day925(), None, None, S)[0]                        # DOWN from 100, stop 2%, trail 1.5%
    times = ['09:27', '10:00', '11:00', '12:00', '15:10', '15:15']
    mid = f.track(it, _polls({'P0': [100, 99.5, 98.8, 99.0]}, times[:4]), '15:15', 1.0)
    assert mid['now_pct'] == 1.0 and mid['booked'] and not mid.get('closed')
    early = f.track(it, _polls({'P0': [100, 99.5, 98.8, 99.0, 99.2]}, times[:5]), '15:15', 1.0)
    assert not early.get('closed')
    out = f.track(it, _polls({'P0': [100, 99.5, 98.8, 99.0, 99.2, 99.4]}, times), '15:15', 1.0)
    assert out['closed'] and abs(out['ret_pct'] - 0.54) < 1e-6 and out['win']
    assert out['book_ret_pct'] == 0.94 and out['best_pct'] == 1.2 and not out['stopped']
    st = f.track(dict(it, side='UP'), _polls({'P0': [100, 99, 97.9, 98, 99, 100]}, times), '15:15', 1.0)
    assert st['stopped'] and st['stopped_at'] == '11:00' and st['stop_ret_pct'] < 0


def test_scorecard_counts_traded_and_watch_separately():
    def item(trade, r): return {'symbol': 'X', 'side': 'UP', 'source': '09:25 pool', 'trade': trade, 'track': {'closed': True, 'ret_pct': r, 'book_ret_pct': r, 'win': r > 0}}
    sc = f.scorecard([{'items': [item(True, 0.5), item(False, -0.2)]}, {'items': [item(True, -0.3)]}])
    assert sc['hold']['n'] == 2 and sc['hold']['win'] == 50.0 and sc['watch']['n'] == 1 and sc['ge1_day'] == 50.0


def test_weak_entry_is_watched_not_traded():
    day = _day925(status=('OPEN', 'OPEN', 'WATCH'))
    day['lists']['hc']['picks'][1]['entry_check'] = {'label': 'WEAK', 'score': 0}
    c = f.candidates(day, None, None, S)
    assert [x['trade'] for x in c] == [True, False, False]


def test_breakdown_splits_the_live_ledger():
    p = lambda g, ec, r, of: {'grade': g, 'status': 'OPEN', 'entry_check': {'label': ec}, 'of_agree': of, 'ret_pct': r, 'book_ret_pct': r, 'pod': g == 'A'}  # noqa: E731
    days = [{'lists': {'hc': {'strong': True, 'picks': [p('A', 'GOOD', 0.5, True), p('B', 'WEAK', -0.4, False)]}}},
            {'lists': {'hc': {'strong': False, 'picks': [p('A', 'GOOD', 0.3, True), p('B', 'GOOD', -0.1, True)]}}}]
    b = {r['group']: r for r in f.pool_breakdown(days)}
    assert b['all pool picks']['n'] == 4 and b['all pool picks']['hold']['win'] == 50.0
    assert b['grade A']['hold']['win'] == 100.0 and b['entry WEAK']['n'] == 1 and b['not WEAK (new rule)']['hold']['win'] == 66.7
