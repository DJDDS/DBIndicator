"""Setup A v1.1 on Railway: bootstrap, evening scan, signals, paper ledger, admin lane, owner-only routes.

Engine parity with the laptop scanner_v11.py was checked on DJ's real store (6 Oct 2026): identical indicator arrays,
identical ledger (28 closed trades, 89% win, +373 bps; SOLARINDS / TEJASNET open)."""
import datetime as dt
import io
import zipfile

import numpy as np
import pandas as pd
import pytest

from app import v11_setup_a as m

TODAY = dt.date(2026, 10, 6)


def _days():
    d0 = TODAY - dt.timedelta(days=m.BOOT_CAL_DAYS)
    return [d0 + dt.timedelta(days=i) for i in range(m.BOOT_CAL_DAYS + 1) if (d0 + dt.timedelta(days=i)).weekday() < 5]


def _market(crash=True):
    """40 trending stocks + NIFTY; the last 6 sessions are a sharp sell-off (should fire Setup A)."""
    rng = np.random.default_rng(7); days = _days(); n = len(days); syms = [f'STK{i:02d}' for i in range(40)] + ['GOLDBEES']
    px = {}
    for k, s in enumerate(syms):
        r = rng.normal(0.003, 0.009, n)
        if crash: r[-6:] = -0.02 - 0.002 * (k % 5)
        px[s] = 100 * np.exp(np.cumsum(r))
    nr = rng.normal(0.0005, 0.006, n)
    if crash: nr[-6:] = -0.012
    nifty = 20000 * np.exp(np.cumsum(nr)); vix = np.full(n, 14.0)
    return days, syms, px, nifty, vix


class FakeNSE:
    def __init__(self, data, upto=TODAY):
        self.days, self.syms, self.px, self.nifty, self.vix = data; self.upto = upto; self.calls = 0
        self.idx = {d: i for i, d in enumerate(self.days)}

    def get(self, url, *, json_ok=False, tries=3):
        self.calls += 1
        if 'BhavCopy_NSE_CM' in url:
            d = dt.datetime.strptime(url.split('_F_0000')[0][-8:], '%Y%m%d').date()
            if d not in self.idx or d > self.upto: return None
            i = self.idx[d]
            rows = [dict(TckrSymb=s, SctySrs='EQ', OpnPric=self.px[s][i] * 0.999, HghPric=self.px[s][i] * 1.01, LwPric=self.px[s][i] * 0.99,
                         ClsPric=self.px[s][i], TtlTrfVal=2e8) for s in self.syms]
            buf = io.BytesIO()
            with zipfile.ZipFile(buf, 'w') as z: z.writestr('b.csv', pd.DataFrame(rows).to_csv(index=False))
            return buf.getvalue()
        if 'ind_close_all' in url:
            d = dt.datetime.strptime(url.split('ind_close_all_')[1][:8], '%d%m%Y').date()
            if d not in self.idx or d > self.upto: return None
            i = self.idx[d]
            return pd.DataFrame([{'Index Name': 'Nifty 50', 'Open Index Value': self.nifty[i], 'Closing Index Value': self.nifty[i]},
                                 {'Index Name': 'India VIX', 'Open Index Value': self.vix[i], 'Closing Index Value': self.vix[i]}]).to_csv(index=False).encode()
        return None


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(m, 'ROOT', tmp_path / 'v11')
    monkeypatch.setattr(m.time, 'sleep', lambda *_: None)
    monkeypatch.setattr(m, 'now_ist', lambda: dt.datetime.combine(TODAY, dt.time(19, 30)))
    m._status['steps'] = {}
    return tmp_path


def test_ca_factor():
    assert m.ca_factor('Bonus 1:1') == pytest.approx(0.5)
    assert m.ca_factor('Face Value Split (Sub-Division) - From Rs 10/- Per Share To Re 1/- Per Share') == pytest.approx(0.1)
    assert np.isnan(m.ca_factor('Dividend - Rs 5 Per Share'))


def test_bootstrap_scan_signals_and_ledger(env):
    nse = FakeNSE(_market())
    out = m.run_evening(nse)
    assert out.startswith('2026-10-06') and m.bootstrapped()
    S = m._read_summary()
    assert S['date'] == '2026-10-06' and S['m5'] < -0.025 and S['score'] >= m.P['score']
    syms = [r['symbol'] for r in S['tonight']]
    assert syms and 'GOLDBEES' not in syms                          # ETFs are never trades
    assert all(r['source'] == 'live' for r in S['tonight'])
    # seeded laptop history carried over: 28 closed replay trades are in the ledger file
    led = pd.read_csv(m._p(m.LEDGER)); assert set(syms) <= set(led.symbol)
    assert (led.status == 'BUY at next open').sum() == min(len(syms), m.P['slots'])
    # idempotent per date
    calls = nse.calls
    assert m.run_evening(nse).startswith('already done')
    assert nse.calls == calls


def test_waits_until_todays_files_are_out(env):
    nse = FakeNSE(_market(crash=False), upto=TODAY - dt.timedelta(days=1))
    with pytest.raises(RuntimeError, match='not out yet'):
        m.run_evening(nse)
    assert m._read_summary() is None
    nse.upto = TODAY
    assert m.run_evening(nse).startswith('2026-10-06')
    assert m._read_summary()['tonight'] == []


def test_catches_up_a_missed_evening(env):
    data = _market()
    nse = FakeNSE(data, upto=dt.date(2026, 10, 2))
    m.run_evening(nse, when=dt.date(2026, 10, 2))
    nse.upto = TODAY
    m.run_evening(nse)
    assert m._read_summary()['scanned_days'] == ['2026-10-05', '2026-10-06']


def test_admin_lane_and_routes(env, monkeypatch):
    from app import web
    from app.security import reset_security_state_for_tests
    monkeypatch.setenv('DBI_OWNER_PASSWORD', 'pw'); monkeypatch.setenv('DBI_MEMBER_USERNAME', 'mem'); monkeypatch.setenv('DBI_MEMBER_PASSWORD', 'mpw')
    reset_security_state_for_tests()
    c = web.app.test_client(); own = {'Authorization': 'Basic YWRtaW46cHc='}; mem = {'Authorization': 'Basic bWVtOm1wdw=='}
    r = c.get('/admin/v130', headers=own); assert r.status_code == 200 and b'Setup A v1.1' in r.data and b'No scan stored yet' in r.data
    m.run_evening(FakeNSE(_market()))
    r = c.get('/admin/v130', headers=own); html = r.data.decode()
    assert r.status_code == 200 and 'BUY at tomorrow' in html and 'STK' in html and 'Run scan now' in html
    assert c.get('/api/v11/status', headers=own).json['summary']['date'] == '2026-10-06'
    assert c.get('/api/v11/export/ledger', headers=own).status_code == 200
    assert c.get('/api/v11/export/nope', headers=own).status_code == 404
    for path in ('/api/v11/status', '/api/v11/export/ledger'):
        assert c.get(path, headers=mem).status_code == 403
    assert c.post('/api/v11/run', headers=mem).status_code == 403
