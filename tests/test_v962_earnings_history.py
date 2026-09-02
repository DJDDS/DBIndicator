import json
import pandas as pd


def test_parse_financial_result_rows_uses_broadcast_or_filing_date_and_filters_symbol():
    from app import nse_earnings_history as ne
    rows = [
        {'symbol':'AAA','broadcastDate':'14-May-2022 18:30:00','filingDate':'14-May-2022','period':'Quarterly'},
        {'symbol':'AAA','broadcastDate':'','filingDate':'10-Aug-2022','period':'Quarterly'},
        {'symbol':'BBB','broadcastDate':'12-May-2022 12:00:00','filingDate':'12-May-2022','period':'Quarterly'},
    ]
    out = ne.parse_financial_result_rows(rows, symbol='AAA')
    assert list(out) == [pd.Timestamp('2022-05-14'), pd.Timestamp('2022-08-10')]


def test_earnings_client_calls_official_nse_endpoint_and_caches(tmp_path):
    from app import nse_earnings_history as ne
    payload = {'data':[{'symbol':'AAA','broadcastDate':'14-May-2022 18:30:00','filingDate':'14-May-2022'}]}
    class Resp:
        status_code=200
        def __init__(self, data): self._data=data; self.content=json.dumps(data).encode()
        def raise_for_status(self): pass
        def json(self): return self._data
    class Session:
        def __init__(self): self.calls=[]; self.headers={}
        def get(self,url,**kwargs):
            self.calls.append((url,kwargs))
            if url=='https://www.nseindia.com/': return Resp({})
            return Resp(payload)
    s=Session(); c=ne.NSEEarningsHistoryClient(session=s, cache_dir=tmp_path)
    dates=c.fetch_symbol('AAA', pd.Timestamp('2022-01-01'), pd.Timestamp('2022-12-31'))
    assert list(dates)==[pd.Timestamp('2022-05-14')]
    assert any('/api/corporates-financial-results' in u for u,_ in s.calls)
    before=len(s.calls)
    dates2=c.fetch_symbol('AAA', pd.Timestamp('2022-01-01'), pd.Timestamp('2022-12-31'))
    assert list(dates2)==list(dates)
    assert len(s.calls)==before


def test_build_earnings_map_reports_fetch_coverage():
    from app import nse_earnings_history as ne
    class Client:
        def fetch_symbol(self,symbol,start,end):
            if symbol=='BAD': raise RuntimeError('network')
            return pd.DatetimeIndex([pd.Timestamp('2022-05-10')]) if symbol=='AAA' else pd.DatetimeIndex([])
    out=ne.build_earnings_map(['AAA','BBB','BAD'], '2021-09-01','2023-09-01', Client())
    assert out['_meta']['symbols_requested']==3
    assert out['_meta']['symbols_loaded']==2
    assert out['_meta']['symbol_coverage']==2/3
    assert out['_meta']['loaded_symbols']==['AAA','BBB']
    assert list(out['AAA'])==[pd.Timestamp('2022-05-10')]
    assert list(out['BBB'])==[]
