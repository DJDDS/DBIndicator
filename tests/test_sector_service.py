import importlib
import json
from pathlib import Path
import pytest


def provider():
    assert importlib.util.find_spec('app.sector_data'), 'Sector provider missing'
    return importlib.import_module('app.sector_data')


def test_catalogue_official_links_deduplicate_and_reject_external():
    d=provider(); html='''<a href="/indices/equity/sectoral-indices/nifty-auto">Nifty Auto</a><a href="/indices/equity/sectoral-indices/nifty-auto">NIFTY AUTO</a><a href="https://evil.test/indices/equity/sectoral-indices/nifty-fake">Fake</a><a href="/indices/equity/thematic-indices/nifty-energy">Energy</a>'''
    r=d.parse_catalogue(html)
    assert len(r)==1 and r[0]['name']=='Nifty Auto'


def test_csv_membership_and_optional_dated_weights():
    d=provider(); data='Company Name,Industry,Symbol,Series,ISIN Code,Weight(%)\nOne Ltd,Auto,AAA,EQ,INE1,60\nTwo Ltd,Auto,BBB,EQ,INE2,40\n'
    r=d.parse_members(data)
    assert [x['symbol'] for x in r]==['AAA','BBB']
    assert r[0]['weight']==60
    assert d.parse_members('Company Name,Symbol\nOne,AAA\n')[0]['weight'] is None
    with pytest.raises(ValueError): d.parse_members('<html>blocked</html>')


def test_csv_url_requires_official_host():
    d=provider()
    assert d.constituent_url('<a href="https://www.niftyindices.com//IndexConstituent/ind_niftyautolist.csv">List</a>').endswith('ind_niftyautolist.csv')
    assert d.constituent_url('<a href="https://evil.test/list.csv">List</a>') is None


def test_atomic_cache_preserves_good_data_on_bad_json(tmp_path):
    d=provider(); p=tmp_path/'cache.json'
    d.save_json(p,{'good':1})
    assert d.load_json(p,{})=={'good':1}
    with pytest.raises(ValueError): d.save_json(p,{'bad':float('nan')})
    assert d.load_json(p,{})=={'good':1}


def test_empty_service_read_has_visible_gaps_and_no_network(monkeypatch,tmp_path):
    assert importlib.util.find_spec('app.sector_service'), 'Sector service missing'
    s=importlib.import_module('app.sector_service')
    monkeypatch.setattr(s,'ROOT',tmp_path)
    monkeypatch.setattr(s,'_state',{'histories':{},'quotes':{},'catalogue':[{'id':'nifty-auto','name':'Nifty Auto','members':[],'source':'official','retrieved_at':'2026-10-05'}], 'running':False,'done':0,'total':0,'error':None,'updated_at':None,'quote_at':None})
    r=s.overview()
    assert r['sectors'][0]['returns']['52W'] is None
    assert r['sectors'][0]['coverage']['total']==0
    assert s.detail('missing') is None
    assert r['status']['updated_at'] is None


def test_index_token_aliases_include_indices_segment():
    from app import sector_service as s
    tokens,quotes=s.resolve_tokens([{'tradingsymbol':'NIFTY FIN SERVICE','instrument_token':1,'segment':'INDICES'}, {'tradingsymbol':'NIFTY 50','instrument_token':2,'segment':'INDICES'}],[{'name':'Nifty Financial Services','members':[]}])
    assert tokens['Nifty Financial Services']==1
    assert quotes['NIFTY 50']=='NSE:NIFTY 50'


def test_off_market_guard_covers_intraday_and_after_close():
    import datetime as dt
    from app import sector_service as s
    assert s._market_hours(dt.datetime(2026,10,5,15,59))
    assert not s._market_hours(dt.datetime(2026,10,5,16,0))


def test_same_day_stale_quote_is_not_presented_as_today(monkeypatch,tmp_path):
    import datetime as dt
    from app import sector_service as s
    monkeypatch.setattr(s,'ROOT',tmp_path)
    monkeypatch.setattr(s,'_summaries',{})
    monkeypatch.setattr(s,'_state',{'histories':{},'quotes':{'A':{'data':{'last_price':100,'ohlc':{'close':90}},'timestamp':'2026-10-05 09:15:00','retrieved_at':'2026-10-05T11:00:00'}}})
    s._recalculate('A',dt.datetime(2026,10,5,11))
    assert s._summaries['A']['returns']['today'] is None
    assert not s._summaries['A']['quote_fresh']


def test_detail_excludes_old_intraday_vwap(monkeypatch,tmp_path):
    import datetime as dt
    from app import sector_service as s
    monkeypatch.setattr(s,'ROOT',tmp_path)
    monkeypatch.setattr(s.scanner,'now_ist',lambda:dt.datetime(2026,10,5,11))
    monkeypatch.setattr(s,'_summaries',{})
    monkeypatch.setattr(s,'_state',{'histories':{},'quotes':{},'catalogue':[{'id':'auto','name':'Nifty Auto','members':[{'symbol':'A','name':'A','weight':None}]}], 'running':False,'done':0,'total':0,'error':None,'updated_at':None,'quote_at':None})
    monkeypatch.setattr(s,'_intraday',{'A':{'vwap':100,'as_of':'2026-10-02T15:00:00','volume_ratio':2}})
    r=s.detail('auto')
    assert r['members'][0]['vwap'] is None
    assert r['members'][0]['volume_ratio'] is None


def test_cached_summary_expires_quote_on_read(monkeypatch,tmp_path):
    import datetime as dt
    from app import sector_service as s
    monkeypatch.setattr(s,'ROOT',tmp_path)
    monkeypatch.setattr(s,'_state',{'histories':{}})
    monkeypatch.setattr(s,'_summaries',{'A':{'price':110,'quote_fresh':True,'quote_timestamp':'2026-10-05 10:00:00','returns':{'today':10}}})
    r=s._summary('A',dt.datetime(2026,10,5,13))
    assert r['returns']['today'] is None
    assert not r['quote_fresh']
    assert r['price'] is None


def test_same_session_old_vwap_cannot_mix_with_current_quote(monkeypatch,tmp_path):
    import datetime as dt
    from app import sector_service as s
    monkeypatch.setattr(s,'ROOT',tmp_path)
    monkeypatch.setattr(s.scanner,'now_ist',lambda:dt.datetime(2026,10,5,13))
    monkeypatch.setattr(s,'_summaries',{})
    monkeypatch.setattr(s,'_state',{'histories':{},'quotes':{},'catalogue':[{'id':'auto','name':'Nifty Auto','members':[{'symbol':'A','name':'A','weight':None}]}], 'running':False,'done':0,'total':0,'error':None,'updated_at':None,'quote_at':None})
    monkeypatch.setattr(s,'_intraday',{'A':{'vwap':100,'as_of':'2026-10-05T10:00:00','volume_ratio':2}})
    r=s.detail('auto')['members'][0]
    assert r['vwap_distance'] is None and r['volume_ratio'] is None


def test_intraday_history_does_not_take_scanner_heavy_slot(monkeypatch):
    import datetime as dt
    from app import sector_service as s
    monkeypatch.setattr(s,'_requested_intraday',{'A'})
    monkeypatch.setattr(s,'_tokens',{'A':1})
    monkeypatch.setattr(s.time,'sleep',lambda _:None)
    monkeypatch.setattr(s.research_runtime,'live_scan_slot',lambda:(_ for _ in ()).throw(AssertionError('must not take scanner slot')))
    class Kite:
        def historical_data(self,*args): return []
    s._load_intraday(Kite(),dt.datetime(2026,10,5,13))


def test_busy_hydration_is_not_complete(monkeypatch,tmp_path):
    import datetime as dt
    from app import sector_service as s
    monkeypatch.setattr(s,'ROOT',tmp_path)
    monkeypatch.setattr(s,'_tokens',{'A':1})
    monkeypatch.setattr(s,'_state',{'histories':{},'quotes':{},'catalogue':[]})
    monkeypatch.setattr(s.scanner,'now_ist',lambda:dt.datetime(2026,10,5,16))
    monkeypatch.setattr(s.research_runtime,'live_scan_slot',lambda:False)
    monkeypatch.setattr(s.time,'sleep',lambda _:None)
    assert s._hydrate(object(),dt.datetime(2026,10,5,16)) is False
