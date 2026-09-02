import json
import pandas as pd


def test_parse_vix_and_nifty_payload_shapes():
    from app import nse_market_regime as nm
    vix={'data':[{'EOD_TIMESTAMP':'01-SEP-2022','EOD_CLOSE_INDEX_VAL':'18.25'}]}
    nifty={'data':{'indexCloseOnlineRecords':[{'EOD_TIMESTAMP':'01-SEP-2022','EOD_CLOSE_INDEX_VAL':'17542.80'}]}}
    vs=nm.parse_vix_payload(vix); ns=nm.parse_index_payload(nifty)
    assert vs.loc[pd.Timestamp('2022-09-01')] == 18.25
    assert ns.loc[pd.Timestamp('2022-09-01')] == 17542.80


def test_market_regime_client_uses_official_nse_historical_endpoints_and_builds_rv(tmp_path):
    from app import nse_market_regime as nm
    dates=pd.bdate_range('2022-01-03', periods=30)
    vrows=[{'EOD_TIMESTAMP':d.strftime('%d-%b-%Y').upper(),'EOD_CLOSE_INDEX_VAL':15+i/10} for i,d in enumerate(dates)]
    nrows=[{'EOD_TIMESTAMP':d.strftime('%d-%b-%Y').upper(),'EOD_CLOSE_INDEX_VAL':17000+i*10} for i,d in enumerate(dates)]
    class Resp:
        status_code=200
        def __init__(self,data): self._data=data; self.content=json.dumps(data).encode()
        def raise_for_status(self): pass
        def json(self): return self._data
    class Session:
        def __init__(self): self.calls=[]; self.headers={}
        def get(self,url,**kwargs):
            self.calls.append((url,kwargs))
            if url=='https://www.nseindia.com/': return Resp({})
            if 'vixhistory' in url: return Resp({'data':vrows})
            return Resp({'data':{'indexCloseOnlineRecords':nrows}})
    s=Session(); c=nm.NSEMarketRegimeClient(session=s, cache_dir=tmp_path)
    out=c.fetch(dates.min(), dates.max())
    assert {'india_vix','nifty_close','nifty_rv20_prev'}.issubset(out.columns)
    assert out['india_vix'].notna().sum()==30
    assert out['nifty_rv20_prev'].notna().sum() > 0
    assert any('/api/historical/vixhistory' in u for u,_ in s.calls)
    assert any('/api/historical/indicesHistory' in u for u,_ in s.calls)
    assert out.attrs['coverage']['event_date_coverage'] == 1.0
