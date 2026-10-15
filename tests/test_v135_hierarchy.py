import datetime as dt

from app import v135_hierarchy as h

S = dict(h.DEFAULT_SETTINGS)


def _polls(moves, vols=None, day='2026-10-14'):
    """moves: symbol -> list of 4 prices (09:30, 09:35, 09:40, 09:45) then a later series for outcomes."""
    times = ['09:30', '09:35', '09:40', '09:45', '09:48', '10:15', '15:15']
    out = []
    for i, t in enumerate(times):
        q = {}
        for sym, px in moves.items():
            vol = (vols or {}).get(sym, 1000)
            q[sym] = [px[i], 1000 + (vol if i >= 3 else 0) * (1 if i < 3 else 1), 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, px[0]]
        out.append({'t': f'{day}T{t}:00', 'q': q})
    return out


def _world(other=-0.0002):
    ind, moves, hist = {}, {}, {}
    for g in range(6):                                  # 6 industries x 20 stocks = 120
        for k in range(20):
            sym = f'S{g}_{k}'; ind[sym] = f'IND{g}'
            drift = -0.002 if g == 0 else other        # industry 0 is weak
            p0 = 100.0
            moves[sym] = [p0, p0 * (1 + drift / 3), p0 * (1 + 2 * drift / 3), p0 * (1 + drift), p0 * (1 + drift), p0 * (1 + drift) * 0.99, p0 * 0.97]
            hist[sym] = [[f'2026-09-{i:02d}', 1000] for i in range(1, 21)]
    return ind, moves, hist


def test_pick_is_the_strongest_residual_down_leader_and_rejections_are_audited():
    ind, moves, hist = _world()
    moves['S0_3'] = [100, 99.6, 99.2, 98.6, 98.6, 97.0, 95.0]           # leads its weak industry
    moves['S1_0'] = [100, 100.2, 100.4, 100.6, 100.6, 101, 101]
    res = h.compute(_polls(moves, {'S0_3': 4000}), hist, ind, S)
    top = res['picks'][0]
    assert top['symbol'] == 'S0_3' and top['role'] == 'PICK' and top['side'] == 'DOWN'
    assert top['resid'] > 0 and 0 < top['eff'] <= 1 and top['volrel'] > 1
    assert sum(c['role'] == 'PICK' for c in res['picks']) == 1                # no lab yet: DOWN active, UP shadow
    assert all(c['role'] != 'PICK' for c in res['picks'] if c['side'] == 'UP')
    assert sum(c['side'] == 'DOWN' for c in res['picks']) <= 1 + S['observe_n']


def test_market_against_blocks_the_down_side():
    ind, moves, hist = _world(other=0.001)                              # market median up, industry 0 down
    moves['S0_3'] = [100, 99.6, 99.2, 98.6, 98.6, 97.0, 95.0]
    res = h.compute(_polls(moves, {'S0_3': 4000}), hist, ind, S)
    assert not res['picks'] and res['rejected'].get('MARKET_AGAINST', 0) >= 1


def test_missing_volume_baseline_is_not_eligible_and_not_zero_filled():
    ind, moves, hist = _world()
    moves['S0_3'] = [100, 99.6, 99.2, 98.6, 98.6, 97.0, 95.0]
    hist['S0_3'] = []
    res = h.compute(_polls(moves, {'S0_3': 4000}), hist, ind, S)
    assert all(c['symbol'] != 'S0_3' for c in res['picks'])
    assert res['rejected'].get('NO_VOLUME_BASELINE') == 1


def test_small_industry_group_is_excluded():
    ind, moves, hist = _world()
    ind['S0_3'] = 'TINY'; ind['S0_4'] = 'TINY'
    moves['S0_3'] = [100, 99.6, 99.2, 98.6, 98.6, 97.0, 95.0]
    res = h.compute(_polls(moves), hist, ind, S)
    assert all(c['symbol'] != 'S0_3' for c in res['picks'])
    assert res['rejected'].get('GROUP_TOO_SMALL', 0) >= 2


def test_settle_records_horizons_mae_mfe_and_exit():
    ind, moves, hist = _world()
    moves['S0_3'] = [100, 99.6, 99.2, 98.6, 98.6, 97.0, 95.0]
    polls = _polls(moves, {'S0_3': 4000})
    res = h.compute(polls, hist, ind, S)
    rec = {'picks': res['picks']}
    assert h.settle(rec, polls, S)
    c = rec['picks'][0]
    assert c['out']['exit'] > 3 and c['settled'] and c['mfe_pct'] >= c['out']['exit'] and c['mae_pct'] <= 0
    assert '30' in c['out'] and c['out']['30'] > 0


def test_settings_defaults_and_registry_has_the_desk(tmp_path):
    assert h.load_settings()['side_mode'] == 'auto'
    from app import recorder_monitor
    names = [r['name'] for r in recorder_monitor.registry(str(tmp_path))]
    assert any('hierarchy' in n for n in names)


def _up_world():
    ind, moves, hist = _world(other=0.001)                              # market up, industry 1..5 up
    moves['S1_3'] = [100, 100.4, 100.8, 101.4, 101.4, 102.0, 104.0]     # leads its strong industry
    return ind, moves, hist


def test_up_side_is_recorded_as_shadow_when_not_active_and_picked_when_active():
    ind, moves, hist = _up_world()
    res = h.compute(_polls(moves, {'S1_3': 4000}), hist, ind, S)
    top = res['picks'][0]
    assert top['symbol'] == 'S1_3' and top['side'] == 'UP' and top['role'] == 'SHADOW'
    s = dict(S, side_mode='both')
    res = h.compute(_polls(moves, {'S1_3': 4000}), hist, ind, s, h.side_plan(s))
    assert res['picks'][0]['symbol'] == 'S1_3' and res['picks'][0]['role'] == 'PICK'


def test_auto_mode_follows_the_lab_verdict_and_its_gates():
    lab = {'UP': {'status': 'ACTIVE', 'cfg': {'min_eff': 0.5, 'min_volrel': 1.5, 'min_mkt': 0.0, 'exit': '60', 'top': 1}},
           'DOWN': {'status': 'SHADOW', 'cfg': None}}
    plan = h.side_plan(S, lab)
    assert plan['UP']['active'] and not plan['DOWN']['active'] and plan['UP']['exit'] == '60' and plan['UP']['min_volrel'] == 1.5
    ind, moves, hist = _up_world()
    res = h.compute(_polls(moves, {'S1_3': 4000}), hist, ind, S, plan)
    assert res['picks'][0]['role'] == 'PICK' and res['picks'][0]['exit_rule'] == '60'
    res = h.compute(_polls(moves, {'S1_3': 1000}), hist, ind, S, plan)  # vol x 1 < 1.5: the lab's gate blocks it
    assert all(c['symbol'] != 'S1_3' for c in res['picks'])


def test_warmup_reports_how_many_baseline_days_exist():
    ind, moves, hist = _world()
    hist = {k: v[:9] for k, v in hist.items()}
    res = h.compute(_polls(moves), hist, ind, S)
    assert res['baseline_days_have'] == 9 and not res['picks'] and res['rejected']['NO_VOLUME_BASELINE'] > 0


def test_settle_books_at_one_percent():
    ind, moves, hist = _world()
    moves['S0_3'] = [100, 99.6, 99.2, 98.6, 98.6, 97.0, 95.0]
    polls = _polls(moves, {'S0_3': 4000})
    rec = {'picks': h.compute(polls, hist, ind, S)['picks']}
    h.settle(rec, polls, S)
    assert rec['picks'][0]['out']['book1'] == 1.0
