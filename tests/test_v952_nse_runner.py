import pandas as pd

from app import backtest
from tests.test_v950_daily_runner import FakeKite, _clear_scanner_caches


def _history_for(symbols, start='2023-01-02', periods=900, coverage=1.0):
    idx = pd.bdate_range(start, periods=periods)
    out = {}
    for j, symbol in enumerate(symbols):
        oi = pd.Series(1_000_000.0 + j * 1000 + pd.Series(range(periods), index=idx).to_numpy() * 800.0, index=idx)
        # inject deterministic shocks so the evidence engine has usable rows
        oi.iloc[::37] = oi.iloc[::37] * 1.08
        expiry = pd.Series(idx + pd.offsets.BDay(20), index=idx)
        out[symbol] = {
            'total_oi': oi * 1.2,
            'near_oi': oi,
            'next_oi': oi * 0.15,
            'far_oi': oi * 0.05,
            'membership': pd.Series(True, index=idx),
            'near_expiry': expiry,
            'near_dte': pd.Series(20.0, index=idx),
            'lot_size': pd.Series(500.0, index=idx),
            'source_format': pd.Series('UDIFF_FO_BHAVCOPY', index=idx),
        }
    out['_meta'] = {
        'dates_requested': periods,
        'dates_loaded': int(periods * coverage),
        'date_coverage': coverage,
        'errors': {},
        'source_formats': ['LEGACY_FO_BHAVCOPY', 'UDIFF_FO_BHAVCOPY'],
        'source': 'NSE_OFFICIAL_FO_BHAVCOPY',
    }
    return out


class StubHistoryModule:
    NSEFuturesArchiveClient = object


class _NoMWPL:
    class NSEHistoricalReportClient:
        def __init__(self, **kwargs):
            pass

    @staticmethod
    def build_validation_mwpl_controls(**kwargs):
        return {
            "available": False, "reason": "TEST_MWPL_UNAVAILABLE", "date_coverage": 0.0,
            "mwpl_by_symbol": {}, "ban_by_symbol": {}, "source": "TEST", "errors": {},
        }


def _disable_mwpl_network(monkeypatch):
    monkeypatch.setattr(backtest, "nse_mwpl", _NoMWPL, raising=False)


def test_v952_runner_uses_nse_near_oi_as_primary_and_kite_only_for_cash(monkeypatch, tmp_path):
    _disable_mwpl_network(monkeypatch)
    _clear_scanner_caches()
    kite = FakeKite()
    histories = _history_for(['AAA'])

    class StubModule:
        class NSEFuturesArchiveClient:
            def __init__(self, **kwargs):
                self.kwargs = kwargs

        @staticmethod
        def build_symbol_histories(days, symbols, client, progress_cb=None, **kwargs):
            return histories

    monkeypatch.setattr(backtest, 'nse_futures_history', StubModule, raising=False)
    out = backtest.run_v95_daily_oi_evidence(kite, symbols=['AAA'], resume_run_dir=tmp_path)

    assert out['symbols_completed'] == 1
    assert out['integrity']['historical_oi_source'] == 'NSE_OFFICIAL_FO_BHAVCOPY'
    assert out['integrity']['nse_oi_date_coverage'] == 1.0
    assert out['research']['controls']['historical_membership'] == 'APPLIED'
    assert out['research']['controls']['lot_size_normalization'] == 'APPLIED'
    assert not [c for c in kite.calls if c[3] is True], 'Kite OI must not be primary when NSE history is available'
    assert [c for c in kite.calls if c[3] is False], 'Kite cash history remains the price source'


def test_v952_runner_uses_actual_nse_expiry_series_not_derived_calendar(monkeypatch, tmp_path):
    _disable_mwpl_network(monkeypatch)
    _clear_scanner_caches()
    histories = _history_for(['AAA'])

    class StubModule:
        class NSEFuturesArchiveClient:
            def __init__(self, **kwargs): pass
        @staticmethod
        def build_symbol_histories(days, symbols, client, progress_cb=None, **kwargs): return histories

    monkeypatch.setattr(backtest, 'nse_futures_history', StubModule, raising=False)
    out = backtest.run_v95_daily_oi_evidence(FakeKite(), symbols=['AAA'], resume_run_dir=tmp_path)
    assert out['integrity']['expiry_calendar'] == 'NSE_ACTUAL_CONTRACT_EXPIRIES'
    assert out['coverage'][0]['derived_expiry_calendar'] is False


def test_v952_runner_fails_closed_when_nse_archive_coverage_is_below_gate(monkeypatch, tmp_path):
    _disable_mwpl_network(monkeypatch)
    _clear_scanner_caches()
    histories = _history_for(['AAA'], coverage=0.80)

    class StubModule:
        class NSEFuturesArchiveClient:
            def __init__(self, **kwargs): pass
        @staticmethod
        def build_symbol_histories(days, symbols, client, progress_cb=None, **kwargs): return histories

    monkeypatch.setattr(backtest, 'nse_futures_history', StubModule, raising=False)
    out = backtest.run_v95_daily_oi_evidence(FakeKite(), symbols=['AAA'], resume_run_dir=tmp_path)
    assert out['integrity']['nse_oi_date_coverage'] == 0.80
    assert out['integrity']['nse_oi_coverage_ok'] is False
    assert out['research']['primary_pass'] is False
    assert 'NSE_HISTORY_COVERAGE' in out['research']['status']


def test_v952_runner_exposes_near_next_far_oi_diagnostics(monkeypatch, tmp_path):
    _disable_mwpl_network(monkeypatch)
    _clear_scanner_caches()
    histories = _history_for(['AAA'])

    class StubModule:
        class NSEFuturesArchiveClient:
            def __init__(self, **kwargs): pass
        @staticmethod
        def build_symbol_histories(days, symbols, client, progress_cb=None, **kwargs): return histories

    monkeypatch.setattr(backtest, 'nse_futures_history', StubModule, raising=False)
    out = backtest.run_v95_daily_oi_evidence(FakeKite(), symbols=['AAA'], resume_run_dir=tmp_path)
    diag = out['integrity']['nse_oi_structure']
    assert diag['near_next_far_available'] is True
    assert diag['primary_series'] == 'near_oi_share_equivalent'


def test_v952_runner_researches_historical_members_discovered_by_nse(monkeypatch, tmp_path):
    _disable_mwpl_network(monkeypatch)
    _clear_scanner_caches()
    histories = _history_for(['AAA', 'BBB'])
    histories['_meta']['historical_symbols_discovered'] = 2
    captured = {}

    class StubModule:
        class NSEFuturesArchiveClient:
            def __init__(self, **kwargs):
                pass
        @staticmethod
        def build_symbol_histories(days, symbols, client, progress_cb=None, discover_historical=False):
            captured['discover_historical'] = discover_historical
            return histories

    monkeypatch.setattr(backtest, 'nse_futures_history', StubModule, raising=False)
    out = backtest.run_v95_daily_oi_evidence(FakeKite(), symbols=['AAA'], resume_run_dir=tmp_path)
    assert captured['discover_historical'] is True
    assert out['integrity']['historical_symbols_discovered'] == 2
    assert out['integrity']['historical_membership_price_coverage'] == 1.0
    assert out['integrity']['current_universe_replay'] is False
    assert out['symbols_completed'] == 2
