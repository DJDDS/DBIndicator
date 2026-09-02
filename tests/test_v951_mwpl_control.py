import datetime as dt

import pandas as pd

from app import nse_mwpl


def _install_nse_history_stub(monkeypatch, backtest, symbols=("AAA",)):
    from tests.test_v952_nse_runner import _history_for
    histories = _history_for(list(symbols))

    class StubHistory:
        class NSEFuturesArchiveClient:
            def __init__(self, **kwargs):
                pass

        @staticmethod
        def build_symbol_histories(days, symbols, client, progress_cb=None, **kwargs):
            return histories

    monkeypatch.setattr(backtest, "nse_futures_history", StubHistory, raising=False)


class FakeResponse:
    def __init__(self, content=b'', status_code=200, headers=None, json_payload=None):
        self.content = content
        self.status_code = status_code
        self.headers = headers or {'content-type': 'text/csv'}
        self._json_payload = json_payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f'HTTP {self.status_code}')

    def json(self):
        if self._json_payload is None:
            raise ValueError('not json')
        return self._json_payload


class FakeSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []
        self.headers = {}

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        if not self.responses:
            raise AssertionError('unexpected request')
        return self.responses.pop(0)


def test_parse_combined_oi_csv_computes_mwpl_utilisation_from_official_fields():
    csv = b'''Date,ISIN,Scrip Name,NSE Symbol,MWPL,Open Interest,Future Equivalent Open Interest,Limit for Next Day\n01-Sep-2026,INE000A01001,AAA LTD,AAA,1000000,750000,700000,250000\n01-Sep-2026,INE000B01002,BBB LTD,BBB,2000000,1900000,1800000,100000\n'''
    rows = nse_mwpl.parse_combined_oi_csv(csv)
    assert rows['AAA']['mwpl'] == 1_000_000
    assert rows['AAA']['open_interest'] == 750_000
    assert rows['AAA']['mwpl_pct'] == 75.0
    assert rows['BBB']['mwpl_pct'] == 95.0


def test_derive_ban_flags_applies_entry_next_day_and_exit_after_80pct():
    dates = pd.bdate_range('2026-08-24', periods=6)
    pct = pd.Series([70.0, 96.0, 93.0, 82.0, 79.0, 70.0], index=dates)
    flags = nse_mwpl.derive_ban_flags(pct, initially_banned=False)
    assert list(flags.astype(bool)) == [False, False, True, True, True, False]


def test_report_client_uses_official_historical_report_endpoint_and_parses_csv(tmp_path):
    csv = b'Date,ISIN,Scrip Name,NSE Symbol,MWPL,Open Interest\n01-Sep-2026,X,AAA LTD,AAA,1000,500\n'
    session = FakeSession([
        FakeResponse(b'<html></html>', headers={'content-type': 'text/html'}),
        FakeResponse(csv),
    ])
    client = nse_mwpl.NSEHistoricalReportClient(session=session, cache_dir=tmp_path)
    rows = client.fetch_combined_oi(dt.date(2026, 9, 1))
    assert rows['AAA']['mwpl_pct'] == 50.0
    api_url, kwargs = session.calls[1]
    assert api_url.endswith('/api/reports')
    assert kwargs['params']['date'] == '01-Sep-2026'
    assert 'Combine Open Interest across exchanges' in kwargs['params']['archives']


def test_build_validation_mwpl_maps_requires_high_date_coverage_and_preserves_missing(tmp_path):
    dates = pd.bdate_range('2026-08-24', periods=5)
    snapshots = {
        dates[0].date(): {'AAA': {'mwpl_pct': 70.0, 'mwpl': 1000, 'open_interest': 700}},
        dates[1].date(): {'AAA': {'mwpl_pct': 96.0, 'mwpl': 1000, 'open_interest': 960}},
        dates[2].date(): {'AAA': {'mwpl_pct': 93.0, 'mwpl': 1000, 'open_interest': 930}},
        dates[3].date(): {'AAA': {'mwpl_pct': 79.0, 'mwpl': 1000, 'open_interest': 790}},
        dates[4].date(): {'AAA': {'mwpl_pct': 70.0, 'mwpl': 1000, 'open_interest': 700}},
    }

    class StubClient:
        def fetch_combined_oi(self, day):
            return snapshots.get(pd.Timestamp(day).date(), {})

    out = nse_mwpl.build_validation_mwpl_controls(
        validation_dates=dates,
        symbols=['AAA'],
        client=StubClient(),
        min_date_coverage=0.95,
    )
    assert out['available'] is True
    assert out['date_coverage'] == 1.0
    assert out['mwpl_by_symbol']['AAA'].loc[dates[1]] == 96.0
    assert bool(out['ban_by_symbol']['AAA'].loc[dates[2]]) is True
    assert bool(out['ban_by_symbol']['AAA'].loc[dates[4]]) is False


def test_build_validation_mwpl_maps_fails_closed_when_report_coverage_is_incomplete():
    dates = pd.bdate_range('2026-08-24', periods=10)

    class SparseClient:
        def fetch_combined_oi(self, day):
            d = pd.Timestamp(day)
            return {'AAA': {'mwpl_pct': 50.0, 'mwpl': 1000, 'open_interest': 500}} if d == dates[0] else {}

    out = nse_mwpl.build_validation_mwpl_controls(
        validation_dates=dates,
        symbols=['AAA'],
        client=SparseClient(),
        min_date_coverage=0.95,
    )
    assert out['available'] is False
    assert out['date_coverage'] == 0.1
    assert 'INSUFFICIENT_MWPL_DATE_COVERAGE' in out['reason']


def test_v951_runner_auto_loads_mwpl_for_validation_only(monkeypatch, tmp_path):
    from app import backtest
    from tests.test_v950_daily_runner import FakeKite, _clear_scanner_caches

    _clear_scanner_caches()
    _install_nse_history_stub(monkeypatch, backtest)
    captured = {}

    class StubLoader:
        class NSEHistoricalReportClient:
            def __init__(self, **kwargs):
                captured['client_kwargs'] = kwargs

        @staticmethod
        def build_validation_mwpl_controls(*, validation_dates, symbols, client, min_date_coverage=0.95):
            dates = pd.DatetimeIndex(validation_dates)
            captured['dates'] = dates
            captured['symbols'] = list(symbols)
            return {
                'available': True,
                'reason': 'APPLIED',
                'date_coverage': 1.0,
                'dates_requested': len(dates),
                'dates_loaded': len(dates),
                'mwpl_by_symbol': {s: pd.Series(50.0, index=dates) for s in symbols},
                'ban_by_symbol': {s: pd.Series(False, index=dates) for s in symbols},
                'source': 'NSE_F&O_COMBINED_OPEN_INTEREST',
                'ban_source': 'NSE_SECBAN_INITIAL_STATE_PLUS_95_80_RULE',
                'errors': {},
            }

    monkeypatch.setattr(backtest, 'nse_mwpl', StubLoader, raising=False)
    out = backtest.run_v95_daily_oi_evidence(
        FakeKite(), symbols=['AAA'], resume_run_dir=tmp_path,
    )
    assert out['research']['controls']['mwpl_control'] == 'APPLIED'
    assert out['integrity']['mwpl_date_coverage'] == 1.0
    assert out['integrity']['mwpl_source'] == 'NSE_F&O_COMBINED_OPEN_INTEREST'
    final_start = pd.Timestamp(out['research']['partitions']['final_start'])
    assert len(captured['dates']) > 0
    assert captured['dates'].max() < final_start


def test_v951_runner_keeps_mwpl_fail_closed_when_auto_download_incomplete(monkeypatch, tmp_path):
    from app import backtest
    from tests.test_v950_daily_runner import FakeKite, _clear_scanner_caches

    _clear_scanner_caches()
    _install_nse_history_stub(monkeypatch, backtest)

    class StubLoader:
        class NSEHistoricalReportClient:
            def __init__(self, **kwargs):
                pass

        @staticmethod
        def build_validation_mwpl_controls(**kwargs):
            return {
                'available': False,
                'reason': 'INSUFFICIENT_MWPL_DATE_COVERAGE:70.0%',
                'date_coverage': 0.70,
                'dates_requested': 100,
                'dates_loaded': 70,
                'mwpl_by_symbol': {}, 'ban_by_symbol': {},
                'source': 'NSE_F&O_COMBINED_OPEN_INTEREST',
                'ban_source': 'NSE_SECBAN_INITIAL_STATE_PLUS_95_80_RULE',
                'errors': {'x': 'missing'},
            }

    monkeypatch.setattr(backtest, 'nse_mwpl', StubLoader, raising=False)
    out = backtest.run_v95_daily_oi_evidence(FakeKite(), symbols=['AAA'], resume_run_dir=tmp_path)
    assert out['research']['controls']['mwpl_control'] == 'UNAVAILABLE'
    assert out['research']['status'].startswith('INCONCLUSIVE_MISSING_MWPL_CONTROL')
    assert out['integrity']['mwpl_date_coverage'] == 0.70
    assert 'INSUFFICIENT_MWPL_DATE_COVERAGE' in out['integrity']['mwpl_reason']
