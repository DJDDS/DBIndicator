import io
import zipfile
import pandas as pd

from app import nse_mwpl


def _legacy_zip(symbol='AAA', pct=90.0):
    csv = f'Date,ISIN,Scrip Name,NSE Symbol,MWPL,NSE Open Interest\n03-SEP-2018,X,{symbol} LTD,{symbol},1000000,{int(pct*10000)}\n'.encode()
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w', zipfile.ZIP_DEFLATED) as zf:
        zf.writestr('nseoi_03092018.csv', csv)
    return buf.getvalue()


def test_v972_prefers_real_legacy_mwpl_zip_route(tmp_path):
    payload = _legacy_zip()

    class Resp:
        def __init__(self, content=b'', status=200):
            self.content = content
            self.status_code = status
            self.headers = {'content-type': 'application/zip'}
        def raise_for_status(self):
            if self.status_code >= 400:
                raise RuntimeError(f'HTTP {self.status_code}')
        def json(self):
            return {'message': 'unavailable'}

    class Session:
        def __init__(self):
            self.calls = []
            self.headers = {}
        def get(self, url, **kwargs):
            self.calls.append((url, kwargs))
            if url == 'https://www.nseindia.com/':
                return Resp(b'warm')
            if url.endswith('/archives/nsccl/mwpl/nseoi_03092018.zip'):
                return Resp(payload, 200)
            return Resp(b'', 404)

    session = Session()
    client = nse_mwpl.NSEHistoricalReportClient(session=session, cache_dir=tmp_path)
    rows = client.fetch_combined_oi(pd.Timestamp('2018-09-03').date())
    assert rows['AAA']['mwpl_pct'] == 90.0
    archive_calls = [u for u, _ in session.calls if 'nseoi_03092018' in u or 'combineoi_03092018' in u]
    assert archive_calls[0].endswith('/archives/nsccl/mwpl/nseoi_03092018.zip')


def test_v972_build_controls_emits_date_progress():
    dates = pd.bdate_range('2018-09-03', periods=5)

    class Client:
        def fetch_combined_oi(self, day):
            return {'AAA': {'mwpl': 1_000_000.0, 'open_interest': 500_000.0, 'mwpl_pct': 50.0}}
        def fetch_secban(self, day):
            return set()

    seen = []
    out = nse_mwpl.build_validation_mwpl_controls(
        validation_dates=dates,
        symbols=['AAA'],
        client=Client(),
        min_date_coverage=0.95,
        progress_cb=lambda done, total, label: seen.append((done, total, label)),
    )
    assert out['available'] is True
    assert seen[0][0] == 0
    assert seen[-1][0] == len(dates)
    assert seen[-1][1] == len(dates)
    assert '2018-' in seen[-1][2]


def test_v972_trial19_surfaces_monthly_mwpl_progress(monkeypatch, tmp_path):
    from app import backtest
    from tests.test_v950_daily_runner import FakeKite, _clear_scanner_caches
    from tests.test_v952_nse_runner import _history_for
    from tests.test_v970_runner import _cash_for

    _clear_scanner_caches()
    histories = _history_for(['AAA'], start='2018-03-01', periods=950)

    class StubHistory:
        class NSEFuturesArchiveClient:
            def __init__(self, **kwargs): pass
        @staticmethod
        def build_symbol_histories(*args, **kwargs): return histories

    class StubCash:
        class NSECashArchiveClient:
            def __init__(self, **kwargs): pass
        @staticmethod
        def build_symbol_price_histories(*args, **kwargs): return _cash_for(histories)

    class StubMWPL:
        class NSEHistoricalReportClient:
            def __init__(self, **kwargs): pass
        @staticmethod
        def build_monthly_mwpl_controls(*, validation_dates, symbols, total_oi_by_symbol, client, min_date_coverage=0.95, progress_cb=None):
            assert progress_cb is not None
            assert 'AAA' in total_oi_by_symbol
            progress_cb(0, 36, '2018-09')
            progress_cb(36, 36, '2021-08')
            return {'available': False, 'reason': 'TEST', 'date_coverage': 0.0, 'month_coverage': 1.0,
                    'observation_coverage': 0.0, 'mwpl_by_symbol': {}, 'ban_by_symbol': {}, 'source': 'TEST', 'errors': {}}

    monkeypatch.setattr(backtest, 'nse_futures_history', StubHistory, raising=False)
    monkeypatch.setattr(backtest, 'nse_cash_history', StubCash, raising=False)
    monkeypatch.setattr(backtest, 'nse_mwpl', StubMWPL, raising=False)

    stages = []
    backtest.run_v97_trial19(
        FakeKite(), symbols=['AAA'], resume_run_dir=tmp_path,
        integrity_data={'earnings_map': {'_meta': {'symbol_coverage': 0.0}}},
        stage_cb=lambda i, total, label, pct: stages.append((i, total, label, pct)),
    )
    assert any('MWPL months 0/36' in label for _, _, label, _ in stages)
    assert any('MWPL months 36/36' in label for _, _, label, _ in stages)

