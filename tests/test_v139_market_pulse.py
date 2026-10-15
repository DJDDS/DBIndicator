from app import v139_market_pulse as mp

DAY = '2026-10-15'


def _polls():
    ind, out = {}, []
    for k in range(60):
        ind[f'B{k}'] = 'Banks' if k < 30 else 'FMCG'
    for i, hm in enumerate(['09:15', '09:16', '09:20', '10:00', '12:00']):
        q = {}
        for k in range(60):
            drift = (0.01 * i) if k < 30 else (-0.01 * i)             # banks drift up, FMCG down
            if k == 3: drift = 0.05 * i                                 # a bank leader
            if k == 40: drift = 0.0                                     # an FMCG stock holding up = leader vs its sector
            q[f'B{k}'] = [100 * (1 + drift), 1000 * (i + 1), 100.0]
        out.append({'t': f'{DAY}T{hm}:00', 'q': q})
    return out, ind


def test_pulse_curve_sectors_and_relative_leaders():
    polls, ind = _polls()
    r = mp.compute(polls, ind)
    assert [p[0] for p in r['curve']] == ['09:16', '09:20', '10:00', '12:00']     # one point per 5-minute bucket (09:16 replaces 09:15)
    assert r['breadth'] == 50.0 and r['at'] == '12:00' and r['n'] == 60
    assert [s['sector'] for s in r['sectors']] == ['Banks', 'FMCG'] and r['sectors'][0]['up'] == 30
    assert r['leaders'][0]['symbol'] == 'B3' and r['leaders'][0]['vs_sector'] > 0
    assert r['laggards'][0]['vs_sector'] <= 0 and any(x['symbol'] == 'B40' for x in r['leaders'])


def test_pulse_needs_enough_stocks():
    polls, ind = _polls()
    for p in polls: p['q'] = dict(list(p['q'].items())[:10])
    assert mp.compute(polls, ind) is None
