import datetime as dt

import pandas as pd

from app import backtest, v95_daily_evidence


class FakeKite:
    def __init__(self):
        self.calls = []
        self._nse = [
            {'tradingsymbol':'AAA','instrument_token':101,'segment':'NSE'},
            {'tradingsymbol':'BBB','instrument_token':102,'segment':'NSE'},
        ]
        today = dt.date.today()
        exp = today + dt.timedelta(days=20)
        self._nfo = [
            {'name':'AAA','instrument_type':'FUT','expiry':exp,'tradingsymbol':'AAAFUT','instrument_token':201},
            {'name':'BBB','instrument_type':'FUT','expiry':exp,'tradingsymbol':'BBBFUT','instrument_token':202},
        ]

    def instruments(self, exchange):
        return self._nse if exchange == 'NSE' else self._nfo

    def historical_data(self, token, from_date, to_date, interval, continuous=False, oi=False):
        self.calls.append((token, interval, bool(continuous), bool(oi), from_date, to_date))
        idx = pd.bdate_range(pd.Timestamp(to_date).normalize() - pd.Timedelta(days=1300), pd.Timestamp(to_date).normalize())
        rows = []
        for i, d in enumerate(idx):
            base = 100 + i * 0.02 + (token % 10)
            row = {'date':d.to_pydatetime(), 'open':base, 'high':base+1.0, 'low':base-1.0, 'close':base+0.2, 'volume':1000+i}
            if oi:
                row['oi'] = 1_000_000 + i * 1000 + (50000 if i % 37 == 0 else 0)
            rows.append(row)
        return rows


def _clear_scanner_caches():
    from app import scanner
    scanner._instrument_cache = {}
    scanner._fut_map_cache = {'date':None,'map':{},'tokens':{}}


def _install_v952_history(monkeypatch, symbols):
    from tests.test_v952_nse_runner import _history_for
    histories = _history_for(list(symbols))

    class StubHistory:
        class NSEFuturesArchiveClient:
            def __init__(self, **kwargs):
                pass

        @staticmethod
        def build_symbol_histories(days, symbols, client, progress_cb=None, **kwargs):
            return histories

    class NoMWPL:
        class NSEHistoricalReportClient:
            def __init__(self, **kwargs):
                pass

        @staticmethod
        def build_validation_mwpl_controls(**kwargs):
            return {
                'available': False, 'reason': 'TEST_UNAVAILABLE', 'date_coverage': 0.0,
                'mwpl_by_symbol': {}, 'ban_by_symbol': {}, 'source': 'TEST', 'errors': {},
            }

    monkeypatch.setattr(backtest, 'nse_futures_history', StubHistory, raising=False)
    monkeypatch.setattr(backtest, 'nse_mwpl', NoMWPL, raising=False)


def test_v950_runner_is_daily_only_and_defaults_to_three_years(monkeypatch):
    _install_v952_history(monkeypatch, ['AAA', 'BBB'])
    _clear_scanner_caches()
    kite = FakeKite()
    progress = []
    out = backtest.run_v95_daily_oi_evidence(kite, symbols=['AAA','BBB'], progress_cb=lambda d,t,s: progress.append((d,t,s)))
    assert out['days'] == 1095
    assert out['symbols_scanned'] == 2
    assert out['symbols_completed'] == 2
    assert out['research']['build'] == v95_daily_evidence.BUILD_ID
    assert progress[-1][:2] == (2,2)
    assert all(call[1] == 'day' for call in kite.calls)
    oi_calls = [c for c in kite.calls if c[3]]
    assert oi_calls == []
    assert out['integrity']['intraday_pipeline_used'] is False


def test_v950_runner_does_not_call_v94_rank_builder(monkeypatch):
    _install_v952_history(monkeypatch, ['AAA'])
    _clear_scanner_caches()
    kite = FakeKite()
    monkeypatch.setattr(backtest, '_build_v91_ranked_events_checkpoint', lambda *a, **k: (_ for _ in ()).throw(AssertionError('must not call V9.4 rank builder')))
    out = backtest.run_v95_daily_oi_evidence(kite, symbols=['AAA'])
    assert out['symbols_completed'] == 1


def test_v950_runner_discloses_missing_point_in_time_controls(monkeypatch):
    _install_v952_history(monkeypatch, ['AAA'])
    _clear_scanner_caches()
    out = backtest.run_v95_daily_oi_evidence(FakeKite(), symbols=['AAA'])
    r = out['research']
    assert r['controls']['mwpl_control'] == 'UNAVAILABLE'
    assert r['controls']['historical_membership'] == 'APPLIED'
    assert r['controls']['lot_size_normalization'] == 'APPLIED'
    assert r['controls']['atm_iv_control'] == 'UNAVAILABLE_NOT_FABRICATED'
    assert r['status'].startswith('INCONCLUSIVE_')


def test_v950_runner_accepts_integrity_series_and_filters_membership(monkeypatch):
    _install_v952_history(monkeypatch, ['AAA'])
    _clear_scanner_caches()
    kite = FakeKite()
    dates = pd.bdate_range(pd.Timestamp.today().normalize() - pd.Timedelta(days=1200), pd.Timestamp.today().normalize())
    membership = pd.Series(True, index=dates)
    mwpl = pd.Series(50.0, index=dates)
    ban = pd.Series(False, index=dates)
    lot = pd.Series(1.0, index=dates)
    out = backtest.run_v95_daily_oi_evidence(kite, symbols=['AAA'], integrity_data={
        'membership_by_symbol': {'AAA':membership},
        'mwpl_by_symbol': {'AAA':mwpl},
        'ban_by_symbol': {'AAA':ban},
        'lot_size_by_symbol': {'AAA':lot},
    })
    assert out['research']['controls']['mwpl_control'] == 'APPLIED'
    assert out['research']['controls']['historical_membership'] == 'APPLIED'
    assert out['research']['controls']['lot_size_normalization'] == 'APPLIED'


def test_v950_runner_checkpoints_each_symbol_and_resumes_without_refetch(monkeypatch, tmp_path):
    _install_v952_history(monkeypatch, ['AAA', 'BBB'])
    _clear_scanner_caches()
    first = FakeKite()
    out1 = backtest.run_v95_daily_oi_evidence(first, symbols=['AAA','BBB'], resume_run_dir=tmp_path)
    assert out1['symbols_completed'] == 2
    assert len(list(tmp_path.glob('*.pkl'))) == 2
    assert first.calls

    _clear_scanner_caches()
    second = FakeKite()
    out2 = backtest.run_v95_daily_oi_evidence(second, symbols=['AAA','BBB'], resume_run_dir=tmp_path)
    assert out2['symbols_completed'] == 2
    assert second.calls == []
    assert out2['integrity']['resumed_symbol_shards'] == 2


def test_v950_primary_runner_enforces_three_year_minimum_window(monkeypatch):
    _install_v952_history(monkeypatch, ['AAA'])
    _clear_scanner_caches()
    out = backtest.run_v95_daily_oi_evidence(FakeKite(), symbols=['AAA'], days=800)
    assert out['days'] == 1095


def test_v950_three_year_partition_keeps_v94_180day_discovery_window_in_locked_final(monkeypatch):
    _install_v952_history(monkeypatch, ['AAA'])
    _clear_scanner_caches()
    out = backtest.run_v95_daily_oi_evidence(FakeKite(), symbols=['AAA'])
    assert out['research']['controls']['v94_discovery_overlap_guard'] == 'APPLIED'
