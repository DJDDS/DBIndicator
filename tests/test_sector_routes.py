import base64
import pytest
from app import web, security


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv('DBI_OWNER_USERNAME','owner')
    monkeypatch.setenv('DBI_OWNER_PASSWORD','test-owner-sector-password')
    monkeypatch.setattr(web,'_scanner_started',True)
    security._AUTH_FAILURES.clear()
    return web.app.test_client()


def auth():
    return {'Authorization':'Basic '+base64.b64encode(b'owner:test-owner-sector-password').decode()}


def test_sector_routes_exist_and_require_auth(client):
    assert client.get('/sector-analysis').status_code==401
    assert client.get('/api/sector-analysis').status_code==401


def test_page_uses_local_assets_and_mobile_viewport(client,monkeypatch):
    assert hasattr(web,'sector_service'), 'Sector routes not implemented'
    monkeypatch.setattr(web.sector_service,'ensure_started',lambda:None)
    r=client.get('/sector-analysis',headers=auth())
    assert r.status_code==200
    assert b'width=device-width' in r.data
    assert b'/static/sector_analysis.js' in r.data


def test_api_returns_cached_data_and_bad_selection_is_404(client,monkeypatch):
    assert hasattr(web,'sector_service'), 'Sector routes not implemented'
    monkeypatch.setattr(web.sector_service,'ensure_started',lambda:None)
    monkeypatch.setattr(web.sector_service,'overview',lambda:{'sectors':[],'status':{'error':'Kite login required'}})
    assert client.get('/api/sector-analysis',headers=auth()).json['sectors']==[]
    monkeypatch.setattr(web.sector_service,'detail',lambda sector,window:None)
    assert client.get('/api/sector-analysis/missing',headers=auth()).status_code==404
    assert client.get('/api/sector-analysis/missing?window=bad',headers=auth()).status_code==400


def test_overview_lists_member_symbols_for_each_sector(monkeypatch):
    from app import sector_service as s
    entry={'id':'nifty-it','name':'NIFTY IT','members':[{'symbol':'INFY','name':'Infosys Ltd.'},{'symbol':'TCS','name':'Tata Consultancy Services Ltd.'}]}
    monkeypatch.setattr(s,'_state',{**s._state,'catalogue':[entry]})
    row=s.overview()['sectors'][0]
    assert [m['symbol'] for m in row['members_list']]==['INFY','TCS']


def test_chart_bars_append_live_candle_only_when_quote_is_fresh(monkeypatch):
    import datetime as dt
    from app import sector_service as s
    now=dt.datetime(2026,10,6,11,0,0)
    hist=[{'date':'2026-10-05 00:00:00+05:30','open':100,'high':101,'low':99,'close':100.5,'volume':1}]
    monkeypatch.setitem(s._state['histories'],'NIFTY IT',hist)
    stamp=now.isoformat()
    quote={'last_price':103.0,'volume':5000,'ohlc':{'open':101.0,'high':104.0,'low':100.0,'close':100.5}}
    monkeypatch.setitem(s._state['quotes'],'NIFTY IT',{'data':quote,'timestamp':stamp})
    bars=s._chart_bars('NIFTY IT',now)
    assert len(bars)==2 and bars[-1]['close']==103.0 and bars[-1]['high']==104.0 and bars[-1]['low']==100.0 and bars[-1]['date'].startswith('2026-10-06')
    monkeypatch.setitem(s._state['quotes'],'NIFTY IT',{'data':quote,'timestamp':(now-dt.timedelta(minutes=30)).isoformat()})
    assert len(s._chart_bars('NIFTY IT',now))==1          # stale quote: history only, never a made-up live candle
