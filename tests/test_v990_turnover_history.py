import pandas as pd
import pytest

from app import nse_futures_history as nf


def test_v990_legacy_parser_exposes_rupee_notional_turnover():
    csv = '''INSTRUMENT,SYMBOL,EXPIRY_DT,OPEN,HIGH,LOW,CLOSE,SETTLE_PR,CONTRACTS,VAL_INLAKH,OPEN_INT,CHG_IN_OI,TIMESTAMP\nFUTSTK,AAA,28-SEP-2023,100,100,100,100,100,20,10,500000,25000,01-SEP-2023\n'''
    row = nf.parse_legacy_fo_bhavcopy(csv, pd.Timestamp('2023-09-01')).iloc[0]
    assert row['turnover_notional'] == pytest.approx(1_000_000.0)


def test_v990_udiff_parser_computes_rupee_notional_from_lots_lot_and_price():
    csv = '''TradDt,Sgmt,FinInstrmTp,TckrSymb,XpryDt,FininstrmActlXpryDt,ClsPric,SttlmPric,OpnIntrst,ChngInOpnIntrst,TtlTradgVol,TtlTrfVal,NewBrdLotQty\n2026-09-01,FO,STF,AAA,2026-09-29,2026-09-29,104,104,1000,50,200,999,500\n'''
    row = nf.parse_udiff_fo_bhavcopy(csv, pd.Timestamp('2026-09-01')).iloc[0]
    assert row['turnover_notional'] == pytest.approx(200 * 500 * 104)


def test_v990_market_activity_parser_computes_rupee_notional_from_quantity_and_price():
    csv = '''Instrument,Symbol,Expiry Date,Open Price,High Price,Low Price,Close Price,Open Interest,Traded Value,Traded Quantity,No of Contracts,No of Trades\nFUTSTK,AAA,29-Sep-2026,100,105,99,104,500000,123,100000,200,120\n'''
    row = nf.parse_market_activity_futures_csv(csv, pd.Timestamp('2026-09-01')).iloc[0]
    assert row['turnover_notional'] == pytest.approx(100000 * 104)


def test_v990_symbol_history_aggregates_total_turnover_across_expiries():
    d = pd.Timestamp('2026-09-01')
    frame = pd.DataFrame([
        {'date': d, 'symbol': 'AAA', 'expiry': d + pd.Timedelta(days=28), 'open_interest': 100.0,
         'oi_share_equivalent': 100.0, 'change_oi': 1.0, 'lot_size': 500.0, 'close': 100.0,
         'settle': 100.0, 'volume': 20.0, 'turnover_notional': 1_000_000.0, 'source_format': 'TEST'},
        {'date': d, 'symbol': 'AAA', 'expiry': d + pd.Timedelta(days=56), 'open_interest': 50.0,
         'oi_share_equivalent': 50.0, 'change_oi': 1.0, 'lot_size': 500.0, 'close': 101.0,
         'settle': 101.0, 'volume': 10.0, 'turnover_notional': 505_000.0, 'source_format': 'TEST'},
    ])

    class Client:
        def fetch_day(self, day):
            return frame

    hist = nf.build_symbol_histories([d], ['AAA'], Client())['AAA']
    assert hist['total_turnover_notional'].loc[d] == pytest.approx(1_505_000.0)
    assert hist['near_turnover_notional'].loc[d] == pytest.approx(1_000_000.0)
