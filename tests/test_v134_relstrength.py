import numpy as np
from app import v134_relstrength as rs


def _poll(t, moves, base=100.0):
    q = {}
    for i, mv in enumerate(moves):
        q['S%03d' % i] = [base * (1 + mv / 100), 1000, 10, 10, base, 1, 0, 0, 0, 0, 0, 0, 0, base]
    return {'t': '2026-10-13T%s:00+05:30' % t, 'q': q}


def _moves(n=150, strong=None, seed=1):
    rng = np.random.default_rng(seed); m = list(rng.normal(0, 0.3, n))
    for k, v in (strong or {}).items(): m[k] = v
    return m


S = dict(rs.DEFAULT_SETTINGS)


def test_building_strength_is_picked_and_sudden_spike_skipped():
    prev = _moves(strong={0: 2.5, 1: 0.0}); now = _moves(strong={0: 3.0, 1: -3.0})
    out = rs.slot_picks([_poll('10:15', prev), _poll('11:15', now)], '11:15', S)
    assert [p['symbol'] for p in out['picks']] == ['S000'] and out['picks'][0]['side'] == 'UP'
    assert [k['symbol'] for k in out['skipped']] == ['S001']


def test_none_qualify_gives_empty_list():
    out = rs.slot_picks([_poll('10:15', _moves()), _poll('11:15', _moves())], '11:15', S)
    assert out['picks'] == []


def test_settle_measures_against_market():
    prev = _moves(strong={0: 2.5}); now = _moves(strong={0: 3.0})
    polls = [_poll('10:15', prev), _poll('11:15', now), _poll('15:15', [x + 1.0 for x in now])]
    rec = {'slots': {'11:15': rs.slot_picks(polls, '11:15', S)}}
    assert rs.settle(rec, polls, S)
    p = rec['slots']['11:15']['picks'][0]
    assert p['win'] and 'edge_pct' in p and rec['slots']['11:15']['settled']
