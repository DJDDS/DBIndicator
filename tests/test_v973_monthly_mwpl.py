import pandas as pd
from app import nse_mwpl


def test_parse_monthly_mwpl_csv_accepts_legacy_mpl_shape():
    csv = 'UNDERLYING_NAME,MWPL (Sep2018)\nAAA,1000000\nBBB,2500000\n'
    rows = nse_mwpl.parse_monthly_mwpl_csv(csv)
    assert rows == {'AAA': 1_000_000.0, 'BBB': 2_500_000.0}


def test_client_fetch_monthly_mwpl_uses_mpl_month_file_and_caches(tmp_path):
    payload = b'UNDERLYING_NAME,MWPL (Sep2018)\nAAA,1000000\n'
    class Resp:
        def __init__(self, content=b'', status=200):
            self.content=content; self.status_code=status; self.headers={'content-type':'text/csv'}
        def raise_for_status(self):
            if self.status_code >= 400: raise RuntimeError('bad')
    class Session:
        def __init__(self): self.headers={}; self.calls=[]
        def get(self, url, **kwargs):
            self.calls.append(url)
            if url.endswith('/content/nsccl/mpl_sep2018.csv'): return Resp(payload)
            return Resp(b'',404)
    s=Session(); client=nse_mwpl.NSEHistoricalReportClient(session=s, cache_dir=tmp_path)
    rows=client.fetch_monthly_mwpl(pd.Timestamp('2018-09-15'))
    assert rows['AAA'] == 1_000_000.0
    before=len(s.calls)
    rows2=client.fetch_monthly_mwpl(pd.Timestamp('2018-09-20'))
    assert rows2 == rows
    assert len(s.calls) == before


def test_monthly_builder_reconstructs_daily_utilisation_without_daily_mwpl_fetches():
    dates=pd.DatetimeIndex(['2018-09-03','2018-09-04','2018-10-01'])
    total={'AAA':pd.Series([700000,960000,790000],index=dates,dtype=float)}
    class Client:
        def __init__(self): self.months=[]; self.secban_days=[]
        def fetch_monthly_mwpl(self, month):
            p=pd.Timestamp(month).to_period('M'); self.months.append(str(p)); return {'AAA':1_000_000.0}
        def fetch_combined_oi(self, day): raise AssertionError('daily combined OI must not be used')
        def fetch_secban(self, day): self.secban_days.append(pd.Timestamp(day).date()); return set()
    c=Client()
    out=nse_mwpl.build_monthly_mwpl_controls(validation_dates=dates,symbols=['AAA'],total_oi_by_symbol=total,client=c,min_date_coverage=0.95)
    assert out['available'] is True
    assert c.months == ['2018-09','2018-10']
    assert out['mwpl_by_symbol']['AAA'].tolist() == [70.0,96.0,79.0]
    assert out['ban_by_symbol']['AAA'].tolist() == [False,False,True]
    assert out['month_coverage'] == 1.0
    assert out['date_coverage'] == 1.0
    assert out['source'] == 'NSE_MONTHLY_MWPL_PLUS_RECONSTRUCTED_TOTAL_FUTSTK_OI'


def test_monthly_builder_only_crosschecks_secban_on_risk_dates():
    dates=pd.bdate_range('2018-09-03', periods=5)
    total={'AAA':pd.Series([50,70,81,96,79],index=dates,dtype=float)*10000}
    class Client:
        def __init__(self): self.days=[]
        def fetch_monthly_mwpl(self, month): return {'AAA':1_000_000.0}
        def fetch_secban(self, day): self.days.append(pd.Timestamp(day).date()); return {'AAA'} if pd.Timestamp(day).date()==dates[4].date() else set()
    c=Client(); out=nse_mwpl.build_monthly_mwpl_controls(validation_dates=dates,symbols=['AAA'],total_oi_by_symbol=total,client=c,min_date_coverage=0.95)
    # Only >=80% / derived-ban neighbourhood needs an authoritative daily list.
    assert set(c.days).issubset(set(dates[2:].date))
    assert len(c.days) < len(dates)
    assert out['secban_dates_requested'] == len(c.days)
    assert out['ban_by_symbol']['AAA'].iloc[-1] == True

def test_trial19_runner_uses_monthly_mwpl_with_existing_total_oi(monkeypatch, tmp_path):
    from app import backtest
    from tests.test_v950_daily_runner import FakeKite, _clear_scanner_caches
    from tests.test_v952_nse_runner import _history_for
    from tests.test_v970_runner import _cash_for
    _clear_scanner_caches()
    histories=_history_for(['AAA'],start='2018-03-01',periods=950)
    class StubHistory:
        class NSEFuturesArchiveClient:
            def __init__(self,**kwargs): pass
        @staticmethod
        def build_symbol_histories(*args,**kwargs): return histories
    class StubCash:
        class NSECashArchiveClient:
            def __init__(self,**kwargs): pass
        @staticmethod
        def build_symbol_price_histories(*args,**kwargs): return _cash_for(histories)
    called={}
    class StubMWPL:
        class NSEHistoricalReportClient:
            def __init__(self,**kwargs): pass
        @staticmethod
        def build_monthly_mwpl_controls(*,validation_dates,symbols,total_oi_by_symbol,client,min_date_coverage=0.95,progress_cb=None):
            called['symbols']=list(symbols); called['total']=total_oi_by_symbol
            dates=pd.DatetimeIndex(validation_dates)
            if progress_cb: progress_cb(0,36,'2018-09'); progress_cb(36,36,'2021-08')
            return {'available':False,'reason':'TEST','date_coverage':0.0,'month_coverage':1.0,'observation_coverage':0.0,'mwpl_by_symbol':{},'ban_by_symbol':{},'source':'TEST','errors':{}}
    monkeypatch.setattr(backtest,'nse_futures_history',StubHistory,raising=False)
    monkeypatch.setattr(backtest,'nse_cash_history',StubCash,raising=False)
    monkeypatch.setattr(backtest,'nse_mwpl',StubMWPL,raising=False)
    stages=[]
    backtest.run_v97_trial19(FakeKite(),symbols=['AAA'],resume_run_dir=tmp_path,integrity_data={'earnings_map':{'_meta':{'symbol_coverage':0.0}}},stage_cb=lambda i,t,l,p: stages.append(l))
    assert called['symbols']==['AAA']
    assert 'AAA' in called['total'] and isinstance(called['total']['AAA'],pd.Series)
    assert any('MWPL months' in label for label in stages)
