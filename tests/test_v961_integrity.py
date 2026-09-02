import io
import zipfile

import pandas as pd

from app import backtest
from app import nse_mwpl
from app import v96_trial17


def _zip_csv(name: str, text: str) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w', zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(name, text)
    return buf.getvalue()


def test_v961_parse_legacy_cm_bhavcopy_keeps_eq_ohlc():
    from app import nse_cash_history as nc
    csv = '''SYMBOL,SERIES,OPEN,HIGH,LOW,CLOSE,LAST,PREVCLOSE,TOTTRDQTY,TOTTRDVAL,TIMESTAMP,TOTALTRADES,ISIN\nAAA,EQ,100,105,99,104,104,101,10000,1040000,01-SEP-2022,100,INE000A\nAAA,BE,90,92,88,91,91,90,100,9100,01-SEP-2022,3,INE000A\nBBB,EQ,50,52,49,51,51,50,5000,255000,01-SEP-2022,50,INE000B\n'''
    out = nc.parse_legacy_cm_bhavcopy(csv, pd.Timestamp('2022-09-01'))
    assert list(out['symbol']) == ['AAA', 'BBB']
    assert list(out['close']) == [104.0, 51.0]
    assert set(out['series']) == {'EQ'}


def test_v961_cash_archive_client_uses_official_legacy_nse_url_and_caches(tmp_path):
    from app import nse_cash_history as nc

    payload = _zip_csv(
        'cm01SEP2022bhav.csv',
        'SYMBOL,SERIES,OPEN,HIGH,LOW,CLOSE,LAST,PREVCLOSE,TOTTRDQTY,TOTTRDVAL,TIMESTAMP,TOTALTRADES,ISIN\n'
        'AAA,EQ,100,105,99,104,104,101,10000,1040000,01-SEP-2022,100,INE000A\n',
    )

    class Resp:
        status_code = 200
        headers = {'content-type': 'application/zip'}
        content = payload
        def raise_for_status(self): pass

    class Session:
        def __init__(self): self.calls=[]; self.headers={}
        def get(self, url, **kwargs): self.calls.append((url, kwargs)); return Resp()

    s = Session()
    client = nc.NSECashArchiveClient(session=s, cache_dir=tmp_path)
    out = client.fetch_day(pd.Timestamp('2022-09-01').date())
    assert out.iloc[0]['symbol'] == 'AAA'
    assert '/content/historical/EQUITIES/2022/SEP/cm01SEP2022bhav.csv.zip' in s.calls[0][0]
    out2 = client.fetch_day(pd.Timestamp('2022-09-01').date())
    assert len(s.calls) == 1
    assert len(out2) == 1


def test_v961_build_cash_histories_reports_point_in_time_price_coverage():
    from app import nse_cash_history as nc
    d1 = pd.Timestamp('2022-09-01')
    d2 = pd.Timestamp('2022-09-02')

    class Client:
        def fetch_day(self, day):
            d = pd.Timestamp(day)
            if d == d1:
                return pd.DataFrame([
                    {'date': d, 'symbol':'AAA','series':'EQ','open':100.0,'high':105.0,'low':99.0,'close':104.0,'source_format':'LEGACY_CM_BHAVCOPY'},
                    {'date': d, 'symbol':'OLD','series':'EQ','open':50.0,'high':51.0,'low':49.0,'close':50.5,'source_format':'LEGACY_CM_BHAVCOPY'},
                ])
            if d == d2:
                return pd.DataFrame([
                    {'date': d, 'symbol':'AAA','series':'EQ','open':104.0,'high':106.0,'low':103.0,'close':105.0,'source_format':'LEGACY_CM_BHAVCOPY'},
                ])
            raise FileNotFoundError('holiday')

    out = nc.build_symbol_price_histories([d1,d2], ['AAA','OLD'], Client())
    assert out['AAA'].loc[d1,'close'] == 104.0
    assert out['OLD'].loc[d1,'close'] == 50.5
    assert pd.isna(out['OLD'].loc[d2,'close'])
    assert out['_meta']['dates_loaded'] == 2
    assert out['_meta']['date_coverage'] == 1.0


def test_v961_trial17_has_separate_historical_cash_integrity_gate():
    # The evaluator must not call a result fully validated if the historical
    # price universe is incomplete, even when membership itself is known.
    idx = pd.bdate_range('2021-09-01', periods=180)
    frame = pd.DataFrame(index=idx)
    frame['eligible'] = True
    frame['fno_member_pti'] = True
    frame['movement_1d_atr'] = 1.2
    frame['movement_2d_atr'] = 1.15
    frame['realized_vol20_prev'] = 0.2
    frame['atr_pct_prev'] = 0.02
    frame['nse_near_dte'] = 15
    frame['nse_near_oi'] = 100.0
    frame['nse_next_oi'] = 50.0
    frame['nse_total_oi'] = pd.Series(range(1000, 1180), index=idx, dtype=float)
    frame['ban_flag'] = False
    frame['mwpl_pct'] = 50.0

    out = v96_trial17.evaluate_trial17(
        {'AAA': frame},
        controls={
            'historical_membership_available': True,
            'historical_cash_price_available': False,
            'lot_size_normalization_available': True,
            'mwpl_available': True,
        },
        bootstrap_reps=10,
    )
    assert out['status'] != 'PASS_INDEPENDENT_VALIDATION'
    assert out['controls']['historical_cash_price'] == 'UNAVAILABLE'


def test_v961_mwpl_client_falls_back_to_direct_legacy_nseoi_archive(tmp_path):
    csv = b'Date,ISIN,Scrip Name,NSE Symbol,MWPL,NSE Open Interest,Limit_for_Next_Day\n01-SEP-2022,INE000A,AAA LTD,AAA,1000000,900000,100000\n'

    class Resp:
        def __init__(self, content=b'', status=200, ctype='text/csv'):
            self.content = content; self.status_code = status; self.headers={'content-type': ctype}
        def raise_for_status(self):
            if self.status_code >= 400:
                raise RuntimeError(f'HTTP {self.status_code}')
        def json(self): return {'message':'unavailable'}

    class Session:
        def __init__(self): self.calls=[]; self.headers={}
        def get(self, url, **kwargs):
            self.calls.append((url, kwargs))
            if url == 'https://www.nseindia.com/':
                return Resp(b'warm')
            if '/api/reports' in url:
                return Resp(b'{"message":"unavailable"}', ctype='application/json')
            if 'nseoi_01092022.csv' in url:
                return Resp(csv)
            return Resp(b'', status=404)

    client = nse_mwpl.NSEHistoricalReportClient(session=Session(), cache_dir=tmp_path)
    rows = client.fetch_combined_oi(pd.Timestamp('2022-09-01').date())
    assert rows['AAA']['mwpl_pct'] == 90.0
    assert any('nseoi_01092022.csv' in url for url, _ in client.session.calls)


def test_v961_runner_can_use_historical_cash_without_current_kite_token(monkeypatch, tmp_path):
    from tests.test_v952_nse_runner import _history_for
    from tests.test_v950_daily_runner import FakeKite, _clear_scanner_caches

    _clear_scanner_caches()
    histories = _history_for(['OLD'], start='2021-04-01', periods=650)
    # Force point-in-time membership and OI to be present for an old symbol.
    histories['_meta']['date_coverage'] = 1.0
    histories['_meta']['historical_symbols_discovered'] = 1
    idx = histories['OLD']['membership'].index
    prices = pd.DataFrame(index=idx, data={
        'open':100.0, 'high':102.0, 'low':99.0, 'close':101.0,
    })
    cash_hist = {'OLD': prices, '_meta': {'date_coverage':1.0, 'dates_loaded':len(idx), 'dates_requested':len(idx)}}

    class StubHistory:
        class NSEFuturesArchiveClient:
            def __init__(self, **kwargs): pass
        @staticmethod
        def build_symbol_histories(*args, **kwargs): return histories

    class StubCash:
        class NSECashArchiveClient:
            def __init__(self, **kwargs): pass
        @staticmethod
        def build_symbol_price_histories(*args, **kwargs): return cash_hist

    class StubMWPL:
        class NSEHistoricalReportClient:
            def __init__(self, **kwargs): pass
        @staticmethod
        def build_validation_mwpl_controls(**kwargs):
            dates = pd.DatetimeIndex(kwargs['validation_dates'])
            return {
                'available': True, 'reason':'APPLIED', 'date_coverage':1.0,
                'mwpl_by_symbol': {'OLD': pd.Series(50.0,index=dates)},
                'ban_by_symbol': {'OLD': pd.Series(False,index=dates)},
                'source':'TEST','errors':{},
            }

    monkeypatch.setattr(backtest, 'nse_futures_history', StubHistory, raising=False)
    monkeypatch.setattr(backtest, 'nse_cash_history', StubCash, raising=False)
    monkeypatch.setattr(backtest, 'nse_mwpl', StubMWPL, raising=False)

    kite = FakeKite()
    monkeypatch.setattr(backtest, '_load_instrument_map', lambda kite: {})
    out = backtest.run_v96_trial17(kite, symbols=['OLD'], resume_run_dir=tmp_path)
    assert out['integrity']['historical_membership_available'] is True
    assert out['integrity']['historical_cash_price_available'] is True
    assert out['symbols_completed'] == 1
