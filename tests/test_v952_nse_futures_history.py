import io
import zipfile
import datetime as dt

import pandas as pd
import pytest

from app import nse_futures_history as nf


def _zip_csv(name: str, text: str) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w', zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(name, text)
    return buf.getvalue()


class FakeResponse:
    def __init__(self, content=b'', status_code=200):
        self.content = content
        self.status_code = status_code
        self.headers = {'content-type': 'application/zip'}

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f'HTTP {self.status_code}')


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


def test_parse_legacy_bhavcopy_keeps_only_stock_futures_and_normalizes_schema():
    csv = '''INSTRUMENT,SYMBOL,EXPIRY_DT,STRIKE_PR,OPTION_TYP,OPEN,HIGH,LOW,CLOSE,SETTLE_PR,CONTRACTS,VAL_INLAKH,OPEN_INT,CHG_IN_OI,TIMESTAMP\nFUTSTK,AAA,28-SEP-2023,0,XX,100,105,99,104,104,200,2080,500000,25000,01-SEP-2023\nFUTSTK,AAA,26-OCT-2023,0,XX,101,106,100,105,105,50,525,120000,10000,01-SEP-2023\nFUTIDX,NIFTY,28-SEP-2023,0,XX,19000,19100,18900,19050,19050,1000,10000,999999,1,01-SEP-2023\nOPTSTK,AAA,28-SEP-2023,100,CE,5,6,4,5,5,10,10,5000,100,01-SEP-2023\n'''
    out = nf.parse_legacy_fo_bhavcopy(csv, pd.Timestamp('2023-09-01'))
    assert list(out['symbol']) == ['AAA', 'AAA']
    assert list(out['expiry'].dt.strftime('%Y-%m-%d')) == ['2023-09-28', '2023-10-26']
    assert list(out['open_interest']) == [500000.0, 120000.0]
    assert set(out['source_format']) == {'LEGACY_FO_BHAVCOPY'}
    assert out['lot_size'].notna().all()


def test_parse_udiff_bhavcopy_accepts_stf_and_uses_actual_expiry_and_lot_size():
    csv = '''TradDt,BizDt,Sgmt,Src,FinInstrmTp,FinInstrmId,ISIN,TckrSymb,SctySrs,XpryDt,FininstrmActlXpryDt,StrkPric,OptnTp,FinInstrmNm,OpnPric,HghPric,LwPric,ClsPric,LastPric,PrvsClsgPric,UndrlygPric,SttlmPric,OpnIntrst,ChngInOpnIntrst,TtlTradgVol,TtlTrfVal,TtlNbOfTxsExctd,SsnId,NewBrdLotQty,Rmks\n2026-09-01,2026-09-01,FO,NSE,STF,1,,AAA,,2026-09-29,2026-09-29,0,,AAA FUT,100,105,99,104,104,103,103,104,1000,50,200,100000,100,F1,500,\n2026-09-01,2026-09-01,FO,NSE,IDF,2,,NIFTY,,2026-09-29,2026-09-29,0,,NIFTY FUT,100,105,99,104,104,103,103,104,999,1,10,1000,10,F1,25,\n'''
    out = nf.parse_udiff_fo_bhavcopy(csv, pd.Timestamp('2026-09-01'))
    assert len(out) == 1
    row = out.iloc[0]
    assert row['symbol'] == 'AAA'
    assert row['expiry'] == pd.Timestamp('2026-09-29')
    assert row['open_interest'] == 1000.0
    assert row['lot_size'] == 500.0
    assert row['oi_share_equivalent'] == 1000.0
    assert row['oi_contracts'] == 2.0
    assert row['source_format'] == 'UDIFF_FO_BHAVCOPY'


def test_archive_client_routes_old_and_new_dates_and_caches(tmp_path):
    legacy_zip = _zip_csv('fo01SEP2023bhav.csv', 'INSTRUMENT,SYMBOL,EXPIRY_DT,OPEN,HIGH,LOW,CLOSE,SETTLE_PR,CONTRACTS,VAL_INLAKH,OPEN_INT,CHG_IN_OI,TIMESTAMP\nFUTSTK,AAA,28-SEP-2023,1,1,1,1,1,1,1,100,2,01-SEP-2023\n')
    modern_zip = _zip_csv('BhavCopy_NSE_FO_0_0_0_20260901_F_0000.csv', 'TradDt,Sgmt,FinInstrmTp,TckrSymb,XpryDt,FininstrmActlXpryDt,ClsPric,SttlmPric,OpnIntrst,ChngInOpnIntrst,TtlTradgVol,NewBrdLotQty\n2026-09-01,FO,STF,AAA,2026-09-29,2026-09-29,104,104,1000,50,200,500\n')
    session = FakeSession([FakeResponse(legacy_zip), FakeResponse(modern_zip)])
    client = nf.NSEFuturesArchiveClient(session=session, cache_dir=tmp_path, prefer_market_activity=False)

    old = client.fetch_day(dt.date(2023, 9, 1))
    new = client.fetch_day(dt.date(2026, 9, 1))
    assert old.iloc[0]['symbol'] == 'AAA'
    assert new.iloc[0]['symbol'] == 'AAA'
    assert '/content/historical/DERIVATIVES/2023/SEP/fo01SEP2023bhav.csv.zip' in session.calls[0][0]
    assert '/content/fo/BhavCopy_NSE_FO_0_0_0_20260901_F_0000.csv.zip' in session.calls[1][0]

    # cached re-read must not hit network
    old2 = client.fetch_day(dt.date(2023, 9, 1))
    assert len(session.calls) == 2
    assert len(old2) == 1


def test_archive_client_fails_closed_on_non_zip_payload(tmp_path):
    session = FakeSession([FakeResponse(b'<html>blocked</html>')])
    client = nf.NSEFuturesArchiveClient(session=session, cache_dir=tmp_path, prefer_market_activity=False)
    with pytest.raises(ValueError, match='valid NSE F&O bhavcopy zip'):
        client.fetch_day(dt.date(2026, 9, 1))


def test_build_symbol_histories_aggregates_all_expiries_and_derives_membership():
    d1 = pd.Timestamp('2026-08-31')
    d2 = pd.Timestamp('2026-09-01')
    frames = {
        d1.date(): pd.DataFrame([
            {'date': d1, 'symbol':'AAA','expiry':pd.Timestamp('2026-09-29'),'open_interest':100.0,'change_oi':5.0,'lot_size':500.0,'close':100.0,'settle':100.0,'volume':10.0,'source_format':'UDIFF_FO_BHAVCOPY'},
            {'date': d1, 'symbol':'AAA','expiry':pd.Timestamp('2026-10-27'),'open_interest':40.0,'change_oi':2.0,'lot_size':500.0,'close':101.0,'settle':101.0,'volume':5.0,'source_format':'UDIFF_FO_BHAVCOPY'},
            {'date': d1, 'symbol':'BBB','expiry':pd.Timestamp('2026-09-29'),'open_interest':70.0,'change_oi':1.0,'lot_size':100.0,'close':50.0,'settle':50.0,'volume':4.0,'source_format':'UDIFF_FO_BHAVCOPY'},
        ]),
        d2.date(): pd.DataFrame([
            {'date': d2, 'symbol':'AAA','expiry':pd.Timestamp('2026-09-29'),'open_interest':110.0,'change_oi':10.0,'lot_size':500.0,'close':102.0,'settle':102.0,'volume':12.0,'source_format':'UDIFF_FO_BHAVCOPY'},
            {'date': d2, 'symbol':'AAA','expiry':pd.Timestamp('2026-10-27'),'open_interest':60.0,'change_oi':20.0,'lot_size':500.0,'close':103.0,'settle':103.0,'volume':8.0,'source_format':'UDIFF_FO_BHAVCOPY'},
            {'date': d2, 'symbol':'AAA','expiry':pd.Timestamp('2026-11-24'),'open_interest':20.0,'change_oi':20.0,'lot_size':500.0,'close':104.0,'settle':104.0,'volume':2.0,'source_format':'UDIFF_FO_BHAVCOPY'},
        ]),
    }

    class StubClient:
        def fetch_day(self, day):
            return frames.get(pd.Timestamp(day).date(), pd.DataFrame())

    out = nf.build_symbol_histories([d1, d2], ['AAA','BBB'], StubClient())
    aaa = out['AAA']
    assert aaa['total_oi'].loc[d1] == 140.0
    assert aaa['near_oi'].loc[d2] == 110.0
    assert aaa['next_oi'].loc[d2] == 60.0
    assert aaa['far_oi'].loc[d2] == 20.0
    assert aaa['near_expiry'].loc[d2] == pd.Timestamp('2026-09-29')
    assert aaa['near_dte'].loc[d2] == 28
    assert bool(aaa['membership'].loc[d1]) is True
    assert bool(out['BBB']['membership'].loc[d1]) is True
    assert bool(out['BBB']['membership'].loc[d2]) is False


def test_build_symbol_histories_reports_date_coverage_without_inventing_missing_days():
    dates = pd.bdate_range('2026-08-24', periods=5)

    class SparseClient:
        def fetch_day(self, day):
            d = pd.Timestamp(day)
            if d == dates[0]:
                return pd.DataFrame([{'date':d,'symbol':'AAA','expiry':d+pd.Timedelta(days=20),'open_interest':100.0,'change_oi':1.0,'lot_size':500.0,'close':100.0,'settle':100.0,'volume':10.0,'source_format':'UDIFF_FO_BHAVCOPY'}])
            raise FileNotFoundError('holiday-or-missing')

    out = nf.build_symbol_histories(dates, ['AAA'], SparseClient())
    assert out['_meta']['dates_requested'] == 5
    assert out['_meta']['dates_loaded'] == 1
    assert out['_meta']['calendar_hit_rate'] == 0.2
    assert out['_meta']['date_coverage'] == 1.0
    assert out['_meta']['dates_not_found'] == 4
    assert len(out['AAA']['membership']) == 5
    assert out['AAA']['membership'].sum() == 1

def test_legacy_parser_infers_lot_and_keeps_open_int_as_share_equivalent():
    # 20 contracts * 500 lot * Rs100 = Rs10,00,000 = 10 lakh turnover.
    csv = '''INSTRUMENT,SYMBOL,EXPIRY_DT,OPEN,HIGH,LOW,CLOSE,SETTLE_PR,CONTRACTS,VAL_INLAKH,OPEN_INT,CHG_IN_OI,TIMESTAMP\nFUTSTK,AAA,28-SEP-2023,100,100,100,100,100,20,10,500000,25000,01-SEP-2023\n'''
    out = nf.parse_legacy_fo_bhavcopy(csv, pd.Timestamp('2023-09-01'))
    row = out.iloc[0]
    assert row['lot_size'] == 500.0
    assert row['oi_share_equivalent'] == 500000.0
    assert row['oi_contracts'] == 1000.0


def test_udiff_parser_treats_open_interest_as_quantity_and_derives_contract_count_from_lot():
    csv = '''TradDt,Sgmt,FinInstrmTp,TckrSymb,XpryDt,FininstrmActlXpryDt,ClsPric,SttlmPric,OpnIntrst,ChngInOpnIntrst,TtlTradgVol,NewBrdLotQty\n2026-09-01,FO,STF,AAA,2026-09-29,2026-09-29,104,104,1000,50,200,500\n'''
    out = nf.parse_udiff_fo_bhavcopy(csv, pd.Timestamp('2026-09-01'))
    row = out.iloc[0]
    assert row['oi_share_equivalent'] == 1000.0
    assert row['oi_contracts'] == 2.0


def test_build_symbol_histories_counts_real_archive_errors_against_coverage_but_not_holidays():
    dates = pd.bdate_range('2026-08-24', periods=5)

    class MixedClient:
        def fetch_day(self, day):
            d = pd.Timestamp(day)
            if d == dates[0]:
                return pd.DataFrame([{'date':d,'symbol':'AAA','expiry':d+pd.Timedelta(days=20),'open_interest':100.0,'change_oi':1.0,'lot_size':500.0,'close':100.0,'settle':100.0,'volume':10.0,'source_format':'UDIFF_FO_BHAVCOPY'}])
            if d in (dates[1], dates[2], dates[3]):
                raise FileNotFoundError('holiday')
            raise RuntimeError('parse/network failure')

    out = nf.build_symbol_histories(dates, ['AAA'], MixedClient())
    assert out['_meta']['dates_loaded'] == 1
    assert out['_meta']['dates_not_found'] == 3
    assert out['_meta']['hard_error_days'] == 1
    assert out['_meta']['calendar_hit_rate'] == 0.2
    assert out['_meta']['date_coverage'] == 0.5


def test_build_symbol_histories_can_discover_historical_futstk_members_outside_current_universe():
    d = pd.Timestamp('2025-01-02')

    class StubClient:
        def fetch_day(self, day):
            return pd.DataFrame([
                {'date':d,'symbol':'AAA','expiry':d+pd.Timedelta(days=20),'open_interest':100.0,'change_oi':1.0,'lot_size':500.0,'close':100.0,'settle':100.0,'volume':10.0,'source_format':'UDIFF_FO_BHAVCOPY'},
                {'date':d,'symbol':'OLDMEMBER','expiry':d+pd.Timedelta(days=20),'open_interest':200.0,'change_oi':2.0,'lot_size':250.0,'close':50.0,'settle':50.0,'volume':20.0,'source_format':'UDIFF_FO_BHAVCOPY'},
            ])

    out = nf.build_symbol_histories([d], ['AAA'], StubClient(), discover_historical=True)
    assert 'AAA' in out
    assert 'OLDMEMBER' in out
    assert bool(out['OLDMEMBER']['membership'].loc[d]) is True
    assert out['_meta']['historical_symbols_discovered'] == 2


def test_parse_market_activity_contract_futures_csv_keeps_futstk_and_treats_oi_as_quantity():
    csv = '''Instrument,Symbol,Expiry Date,Open Price,High Price,Low Price,Close Price,Open Interest,Traded Value,Traded Quantity,No of Contracts,No of Trades\nFUTSTK,AAA,29-Sep-2026,100,105,99,104,500000,1040000,100000,200,120\nFUTSTK,AAA,27-Oct-2026,101,106,100,105,150000,315000,25000,50,30\nFUTIDX,NIFTY,29-Sep-2026,25000,25100,24900,25050,999999,1000000,1000,10,20\n'''
    out = nf.parse_market_activity_futures_csv(csv, pd.Timestamp('2026-09-01'))
    assert list(out['symbol']) == ['AAA', 'AAA']
    assert list(out['oi_share_equivalent']) == [500000.0, 150000.0]
    assert list(out['lot_size']) == [500.0, 500.0]
    assert list(out['oi_contracts']) == [1000.0, 300.0]
    assert set(out['source_format']) == {'NSE_MARKET_ACTIVITY_FOD'}


def test_archive_client_prefers_market_activity_report_and_extracts_fod_csv(tmp_path):
    market_zip = _zip_csv(
        'fo01092026/fo01092026.csv',
        'Instrument,Symbol,Expiry Date,Open Price,High Price,Low Price,Close Price,Open Interest,Traded Value,Traded Quantity,No of Contracts,No of Trades\n'
        'FUTSTK,AAA,29-Sep-2026,100,105,99,104,500000,1040000,100000,200,120\n',
    )
    session = FakeSession([FakeResponse(b'warm'), FakeResponse(market_zip)])
    client = nf.NSEFuturesArchiveClient(session=session, cache_dir=tmp_path)

    out = client.fetch_day(dt.date(2026, 9, 1))

    assert len(out) == 1
    assert out.iloc[0]['symbol'] == 'AAA'
    assert out.iloc[0]['source_format'] == 'NSE_MARKET_ACTIVITY_FOD'
    assert session.calls[0][0] == 'https://www.nseindia.com/'
    assert session.calls[1][0] == 'https://www.nseindia.com/api/reports'
    assert session.calls[1][1]['params']['date'] == '01-Sep-2026'
    assert 'F&O - Market Activity Report' in session.calls[1][1]['params']['archives']
    assert not any('/content/fo/BhavCopy_' in url for url, _ in session.calls)


def test_archive_client_falls_back_to_bhavcopy_when_market_activity_unavailable(tmp_path):
    modern_zip = _zip_csv(
        'BhavCopy_NSE_FO_0_0_0_20260901_F_0000.csv',
        'TradDt,Sgmt,FinInstrmTp,TckrSymb,XpryDt,FininstrmActlXpryDt,ClsPric,SttlmPric,OpnIntrst,ChngInOpnIntrst,TtlTradgVol,NewBrdLotQty\n'
        '2026-09-01,FO,STF,AAA,2026-09-29,2026-09-29,104,104,1000,50,200,500\n',
    )
    session = FakeSession([
        FakeResponse(b'warm'),
        FakeResponse(b'{"message":"file unavailable"}'),
        FakeResponse(modern_zip),
    ])
    # Teach the fake response enough JSON semantics for the report path.
    session.responses[1].headers = {'content-type': 'application/json'}
    session.responses[1].json = lambda: {'message': 'file unavailable'}
    client = nf.NSEFuturesArchiveClient(session=session, cache_dir=tmp_path)

    out = client.fetch_day(dt.date(2026, 9, 1))

    assert len(out) == 1
    assert out.iloc[0]['source_format'] == 'UDIFF_FO_BHAVCOPY'
    assert any('/content/fo/BhavCopy_NSE_FO_0_0_0_20260901_F_0000.csv.zip' in url for url, _ in session.calls)


def test_direct_bhavcopy_route_can_be_requested_for_regression_tests(tmp_path):
    modern_zip = _zip_csv(
        'BhavCopy_NSE_FO_0_0_0_20260901_F_0000.csv',
        'TradDt,Sgmt,FinInstrmTp,TckrSymb,XpryDt,FininstrmActlXpryDt,ClsPric,SttlmPric,OpnIntrst,ChngInOpnIntrst,TtlTradgVol,NewBrdLotQty\n'
        '2026-09-01,FO,STF,AAA,2026-09-29,2026-09-29,104,104,1000,50,200,500\n',
    )
    session = FakeSession([FakeResponse(modern_zip)])
    client = nf.NSEFuturesArchiveClient(session=session, cache_dir=tmp_path, prefer_market_activity=False)
    out = client.fetch_day(dt.date(2026, 9, 1))
    assert len(out) == 1
    assert session.calls[0][0].endswith('BhavCopy_NSE_FO_0_0_0_20260901_F_0000.csv.zip')
