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
    monkeypatch.setattr(web.sector_service,'detail',lambda sector,window,**k:None)
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


def test_live_quotes_only_poll_during_market_window():
    import datetime as dt
    from app import sector_service as s
    assert s._quote_window(dt.datetime(2026,10,5,10,0))          # Monday morning
    assert s._quote_window(dt.datetime(2026,10,5,15,40))         # final quote window after the 15:30 close
    assert not s._quote_window(dt.datetime(2026,10,5,15,46))
    assert not s._quote_window(dt.datetime(2026,10,5,18,30))     # evening
    assert not s._quote_window(dt.datetime(2026,10,5,8,59))
    assert not s._quote_window(dt.datetime(2026,10,3,11,0))      # Saturday


def test_sector_intraday_and_lite_detail(monkeypatch):
    from app import sector_service as s
    entry={'id':'nifty-it','name':'NIFTY IT','members':[{'symbol':'INFY','name':'Infosys Ltd.'}]}
    monkeypatch.setattr(s,'_state',{**s._state,'catalogue':[entry]})
    monkeypatch.setitem(s._intraday,'NIFTY IT',{'candles':[{'date':'2026-10-06 09:15:00+05:30','open':1,'high':1,'low':1,'close':1,'volume':0}],'as_of':'x'})
    r=s.sector_intraday('nifty-it')
    assert r['sector']=='NIFTY IT' and len(r['series']['NIFTY IT']['candles'])==1 and 'NIFTY 50' in r['series']
    assert s.sector_intraday('missing') is None
    d=s.detail('nifty-it',20,lite=True)
    assert d['rotation']=={} and d['breadth_series']==[] and d['members'][0]['evidence'] is None


def _m(sym, today, w, m, q):
    return {'symbol':sym,'returns':{'today':today,'1W':w,'1M':m,'3M':q}}


def test_movers_top_bottom_two_with_windows_and_ranks():
    from app import sector_service as s
    mem=[_m('A',3.0,6,9,20),_m('B',2.0,5,8,15),_m('C',0.5,1,1,1),_m('D',-1.0,-2,-3,-4),_m('E',-2.5,-6,-9,-20),{'symbol':'X','returns':{}}]
    mv=s.movers(mem)
    t=mv['today']
    assert [x['symbol'] for x in t['top']]==['A','B'] and [x['symbol'] for x in t['bottom']]==['E','D'] and t['n']==5
    a=t['top'][0]
    assert a['returns']=={'today':3.0,'1W':6,'1M':9,'3M':20} and a['ranks']=={'today':1,'1W':1,'1M':1,'3M':1}
    assert t['bottom'][0]['ranks']['3M']==5
    assert [x['symbol'] for x in mv['3M']['top']]==['A','B']
    few=s.movers([_m('A',1,1,1,1),_m('B',-1,-1,-1,-1),_m('C',0,0,0,0)])['today']
    assert [x['symbol'] for x in few['top']]==['A'] and [x['symbol'] for x in few['bottom']]==['B']   # small group: 1 leader + 1 laggard


def test_movers_log_once_per_day_and_seen_counts(monkeypatch, tmp_path):
    from app import sector_service as s
    monkeypatch.setattr(s,'ROOT',tmp_path)
    mem=[_m('A',3.0,6,9,20),_m('B',2.0,5,8,15),_m('D',-1.0,-2,-3,-4),_m('E',-2.5,-6,-9,-20)]
    rows=[{'id':'it','movers':s.movers(mem)}]
    assert s.log_movers(rows,'2026-10-12') is True and s.log_movers(rows,'2026-10-12') is False
    s.log_movers(rows,'2026-10-13')
    rows=[{'id':'it','movers':s.movers(mem)}]; s._attach_history(rows)
    top=rows[0]['movers']['today']['top'][0]; bot=rows[0]['movers']['today']['bottom'][0]
    assert top['symbol']=='A' and top['seen']=={'top':2,'bottom':0,'of':2} and bot['seen']=={'top':0,'bottom':2,'of':2}


def test_overview_carries_movers(monkeypatch, tmp_path):
    from app import sector_service as s
    monkeypatch.setattr(s,'ROOT',tmp_path)
    entry={'id':'nifty-it','name':'NIFTY IT','members':[{'symbol':'INFY','name':'Infosys'},{'symbol':'TCS','name':'TCS'}]}
    monkeypatch.setattr(s,'_state',{**s._state,'catalogue':[entry]})
    o=s.overview()
    assert o['movers_compare']==['today','1W','1M','3M'] and 'today' in o['sectors'][0]['movers']


def test_tech_read_describes_trend_breakout_and_stretch():
    from app import sector_service as s
    up={'price':120.0,'sma':{'20':110,'50':105,'200':90},'levels':{'swing_high':118,'swing_low':100},'below_high52':-1.0,'above_low52':40,'extension_atr':3.1}
    t=s.tech_read(up)
    assert t['bias']=='bullish' and t['notes'][0].startswith('uptrend') and any('breaking out' in n for n in t['notes']) and len(t['notes'])==3
    dn={'price':80.0,'sma':{'20':90,'50':95,'200':100},'levels':{'swing_high':99,'swing_low':82},'below_high52':-30,'above_low52':1.0,'extension_atr':-1}
    assert s.tech_read(dn)['bias']=='bearish'
    pb={'price':95.0,'sma':{'20':100,'50':98,'200':90},'levels':{},'below_high52':-12,'above_low52':20,'extension_atr':-1}
    t=s.tech_read(pb); assert t['bias']=='mixed' and 'pullback' in t['notes'][0]
    assert s.tech_read({'price':None})=={'bias':None,'notes':[]}


def test_industry_groups_use_basic_industry_then_broad_fallback():
    from app import sector_service as s, sector_industry as ind
    summ={k:{'symbol':k,'returns':{'today':v,'1W':v,'1M':v,'3M':v}} for k,v in (('HDFCBANK',1.0),('ICICIBANK',2.0),('SBIN',-1.0),('PNB',-3.0),('BANKX',-2.0),('ITC',0.5))}
    cls={'HDFCBANK':{'basic':'Private Sector Bank','sector':'Banks'},'ICICIBANK':{'basic':'Private Sector Bank','sector':'Banks'},
         'SBIN':{'basic':'Public Sector Bank','sector':'Banks'},'PNB':{'basic':'Public Sector Bank','sector':'Banks'},'BANKX':{'broad':'Public Sector Bank'},
         'ITC':{'basic':'Diversified FMCG'}}
    g={r['name']:r for r in s.industry_groups(summ,cls)}
    assert set(g)=={'Private Sector Bank','Public Sector Bank'}            # single-stock groups are dropped
    assert g['Private Sector Bank']['returns']['today']==1.5 and g['Private Sector Bank']['movers']['today']['top'][0]['symbol']=='ICICIBANK'
    assert g['Public Sector Bank']['n']==3 and g['Public Sector Bank']['returns']['today']==-2.0 and g['Public Sector Bank']['advancing_pct']==0
    assert ind.parse_quote({'industryInfo':{'macro':'Financial Services','sector':'Financial Services','industry':'Banks','basicIndustry':'Private Sector Bank'}})['basic']=='Private Sector Bank'
    assert ind.parse_quote({}) is None
    assert ind.parse_nifty500(b'Company Name,Industry,Symbol,Series,ISIN Code\nHDFC Bank,Financial Services,HDFCBANK,EQ,X\n')=={'HDFCBANK':'Financial Services'}


def test_industry_refresh_is_incremental_and_saved(tmp_path):
    import datetime as dt
    from app import sector_industry as ind
    class N:
        calls=[]
        def get(self,url,json_ok=False):
            N.calls.append(url)
            if url==ind.NIFTY500: return b'Company Name,Industry,Symbol\nA,Banks,AAA\nB,IT,BBB\n'
            return {'industryInfo':{'basicIndustry':'Group '+url[-3:],'sector':'S'}} if url.endswith('AAA') else None
    today=dt.date(2026,10,12)
    cls=ind.refresh(tmp_path,{'AAA','BBB'},N(),today,pause=0)
    assert cls['AAA']['basic']=='Group AAA' and cls['BBB']=={'broad':'IT'} and ind.group_of(cls['BBB'])==('IT','')
    assert ind.needs_refresh(tmp_path,{'AAA'},today) is False and ind.needs_refresh(tmp_path,{'AAA'},today+dt.timedelta(days=8)) is True
    N.calls.clear(); ind.refresh(tmp_path,{'AAA','BBB'},N(),today,pause=0)
    assert not any(u.endswith('AAA') for u in N.calls)                    # fresh entries are not fetched again


def test_quote_cycle_reuses_history_math_and_matches_full_summary(monkeypatch):
    import datetime as dt
    from app import sector_service as s, sector_analytics as a
    now=dt.datetime(2026,10,6,11,0,0)
    hist=[{'date':f'2026-{m:02d}-{d:02d} 00:00:00+05:30','open':100+d,'high':102+d,'low':99+d,'close':101+d+m,'volume':1}
          for m in range(1,10) for d in range(1,28)]
    monkeypatch.setitem(s._state['histories'],'TCS',hist)
    quote={'last_price':140.0,'ohlc':{'close':137.0}}
    monkeypatch.setitem(s._state['quotes'],'TCS',{'data':quote,'timestamp':now.isoformat()})
    monkeypatch.setattr(s,'_bases',{})
    calls=[]; real=a.summary_base
    monkeypatch.setattr(a,'summary_base',lambda *x,**k:(calls.append(1),real(*x,**k))[1])
    s._recalculate('TCS',now); s._recalculate('TCS',now+dt.timedelta(seconds=60))
    assert len(calls)==2                                   # one history base + one live base, reused by the 2nd quote cycle
    r=s._summaries['TCS']; r={k:v for k,v in r.items() if k not in ('_historical','quote_timestamp','quote_fresh','quote_retrieved_at')}
    assert r==a.summarize(hist,now,quote) and r['price']==140.0
    monkeypatch.setitem(s._state['histories'],'TCS',hist+[{'date':'2026-10-05 00:00:00+05:30','open':150,'high':151,'low':149,'close':150,'volume':1}])
    s._recalculate('TCS',now)
    assert len(calls)==5                                   # (+1 from the summarize() check) new history -> both bases recomputed


def test_overview_is_cached_until_inputs_change(monkeypatch):
    from app import sector_service as s
    entry={'id':'nifty-it','name':'NIFTY IT','members':[{'symbol':'INFY','name':'Infosys Ltd.'}]}
    monkeypatch.setattr(s,'_state',{**s._state,'catalogue':[entry],'quote_at':'2026-10-06T11:00:00'})
    monkeypatch.setattr(s,'_overview_cache',{'key':None,'at':0.0,'value':None})
    first=s.overview(); assert s.overview() is first
    s._state['quote_at']='2026-10-06T11:01:05'             # new quote cycle -> fresh payload
    assert s.overview() is not first


def test_fo_trim_keeps_fo_members_drops_thin_and_duplicate_sectors():
    from app import sector_service as s
    m=lambda *x:[{'symbol':v} for v in x]
    cat=[{'id':'bank','name':'NIFTY BANK','members':m('HDFCBANK','ICICIBANK','SBIN','AUBANK','IDFCFIRSTB')},
         {'id':'fin','name':'NIFTY FIN','members':m('HDFCBANK','ICICIBANK','SBIN','BAJFINANCE','SMALLCO1')},
         {'id':'fin2550','name':'NIFTY FIN 25/50','members':m('HDFCBANK','ICICIBANK','SBIN','BAJFINANCE','SMALLCO2')},
         {'id':'media','name':'NIFTY MEDIA','members':m('SUNTV','SMALLCO3','SMALLCO4')}]
    fo=['HDFCBANK','ICICIBANK','SBIN','AUBANK','IDFCFIRSTB','BAJFINANCE','SUNTV']
    out=s.trim_catalogue(cat,fo,fno_only=True,min_members=3,jaccard=0.8,hidden=())
    assert [e['id'] for e in out]==['bank','fin']                       # 25/50 = same F&O stocks as fin; media has 1 F&O stock
    assert [x['symbol'] for x in out[1]['members']]==['HDFCBANK','ICICIBANK','SBIN','BAJFINANCE'] and out[1]['official_members']==5
    assert len(s.trim_catalogue(cat,None))==4                           # no F&O list yet: official catalogue
    assert [e['id'] for e in s.trim_catalogue(cat,fo,hidden=('bank',),min_members=3,jaccard=0.8)]==['fin']
    nfo=[{'name':'SBIN','instrument_type':'FUT','segment':'NFO-FUT'},{'name':'NIFTY','instrument_type':'FUT','segment':'NFO-FUT'}]
    nse=[{'tradingsymbol':'SBIN','segment':'NSE'},{'tradingsymbol':'TCS','segment':'NSE'},{'tradingsymbol':'NIFTY 50','segment':'INDICES'}]
    assert s.fo_symbols(nfo,nse)==['SBIN']


def test_overview_members_mini_for_pulse_donut(monkeypatch):
    import datetime as dt
    from app import sector_service as s
    now=dt.datetime(2026,10,6,11,0,0)
    entry={'id':'nifty-it','name':'NIFTY IT','members':[{'symbol':'INFY','name':'Infosys'}]}
    monkeypatch.setattr(s,'_state',{**s._state,'catalogue':[entry],'quotes':{'INFY':{'data':{'last_price':101.0,'ohlc':{'open':100,'high':102,'low':99,'close':100.5}},'timestamp':now.isoformat()}}})
    monkeypatch.setattr(s,'_overview_cache',{'key':None,'at':0.0,'value':None})
    monkeypatch.setattr(s.scanner,'now_ist',lambda: now)
    mm=s.overview()['sectors'][0]['members_mini']
    assert mm[0]['s']=='INFY' and set(mm[0]['r'])=={'today','1W','1M','3M'} and mm[0]['d']=={'o':100,'h':102,'l':99,'pc':100.5}
    assert s._day_ohlc('INFY',now+dt.timedelta(hours=2))=={}          # stale quote: no made-up OHLC


def test_dashboard_summary_and_route(client, monkeypatch):
    import datetime as dt
    from app import sector_service as s
    now=dt.datetime(2026,10,6,11,0,0)
    entry={'id':'nifty-it','name':'NIFTY IT','members':[{'symbol':'INFY','name':'Infosys'}]}
    q={'data':{'last_price':101.0,'ohlc':{'open':100,'high':102,'low':99,'close':100.5}},'timestamp':now.isoformat()}
    monkeypatch.setattr(s,'_state',{**s._state,'catalogue':[entry],'quotes':{'INFY':q}})
    monkeypatch.setattr(s,'_overview_cache',{'key':None,'at':0.0,'value':None})
    monkeypatch.setattr(s.scanner,'now_ist',lambda: now)
    monkeypatch.setattr(s,'ensure_started',lambda: None)
    d=s.dashboard_summary()
    assert d['sectors'][0]['id']=='nifty-it' and d['stocks']['INFY']['sector']=='nifty-it' and d['stocks']['INFY']['d']['h']==102
    assert 'INDIA VIX' in s.EXTRA_INDICES
    assert client.get('/api/sector-analysis/dashboard').status_code==401
    r=client.get('/api/sector-analysis/dashboard',headers=auth()); assert r.status_code==200 and 'INFY' in r.get_json()['stocks']
    lite=client.get('/api/sector-analysis/dashboard?lite=1',headers=auth()).get_json()          # scanner dashboard: index strip only
    assert 'stocks' not in lite and 'sectors' not in lite and 'indices' in lite
    assert all('hist' in i for i in lite['indices'])


def test_close_quote_stays_valid_right_after_the_close():
    import datetime as dt
    from app import sector_service as s
    day = dt.datetime(2026, 10, 12)
    stamp = '2026-10-12 15:43:00'
    assert s._fresh_timestamp(stamp, day.replace(hour=15, minute=55), final_quote=True)      # was blank until 16:00
    assert not s._fresh_timestamp(stamp, day.replace(hour=15, minute=55))                    # live (non-final) rule unchanged
    assert not s._fresh_timestamp('2026-10-12 15:20:00', day.replace(hour=15, minute=55), final_quote=True)   # pre-close quote is not a final one


def test_dashboard_indices_points_and_gift_parser(monkeypatch):
    import datetime as dt
    from app import sector_service as s
    names=[n for n,_ in s.DASH_INDICES]
    assert names==['NIFTY 50','SENSEX','NIFTY BANK','GIFT NIFTY','INDIA VIX'] and 'NIFTY IT' not in names and 'NIFTY FINANCIAL SERVICES' not in names
    # NSE shapes: flat, nested under data, strings with commas
    assert s.parse_gift({'data':{'GiftNifty':{'LASTPRICE':'25,061.50','DAYCHANGE':'-42.5','PERCHANGE':'-0.17'}}})=={'price':25061.5,'chg':-42.5,'pct':-0.17}
    g=s.parse_gift({'lastPrice':25000,'pChange':0.4}); assert g['chg']==round(25000-25000/1.004,2)
    assert s.parse_gift({'data':{'LASTPRICE':12.3}}) is None and s.parse_gift({}) is None and s.parse_gift(None) is None
    # points = price - previous close (the quote's own), shown for an index that has a fresh quote
    now=dt.datetime(2026,10,6,11,0,0)
    q={'data':{'last_price':25100.0,'ohlc':{'open':25000,'high':25150,'low':24980,'close':25000.0}},'timestamp':now.isoformat()}
    monkeypatch.setattr(s,'_state',{**s._state,'catalogue':[],'quotes':{'NIFTY 50':q}})
    monkeypatch.setattr(s,'_overview_cache',{'key':None,'at':0.0,'value':None})
    monkeypatch.setattr(s.scanner,'now_ist',lambda: now)
    monkeypatch.setattr(s,'_summary',lambda name,n: {'price':25100.0,'returns':{'today':0.4,'1W':1.0,'1M':2.0,'3M':3.0}})
    monkeypatch.setattr(s,'_history',lambda name: [])
    monkeypatch.setattr(s,'gift_nifty',lambda: {'price':25060.0,'chg':60.0,'pct':0.24})
    d=s.dashboard_summary(lite=True)
    by={i['name']:i for i in d['indices']}
    assert by['NIFTY 50']['chg']==100.0 and by['GIFT NIFTY']['chg']==60.0 and by['GIFT NIFTY']['live'] and by['SENSEX']['label']=='SENSEX'


def test_gift_nifty_from_market_status_object_and_fetch_order(monkeypatch):
    from app import sector_service as ss
    g = {'INSTRUMENTTYPE': 'FUTIDX', 'SYMBOL': 'NIFTY', 'LASTPRICE': 25012.5, 'DAYCHANGE': -48.5, 'PERCHANGE': -0.19, 'TIMESTMP': '13-Oct-2026 11:10'}
    v = ss.parse_gift(g)
    assert v['price'] == 25012.5 and v['chg'] == -48.5 and v['pct'] == -0.19

    class FakeNSE:
        urls = []
        def get(self, url, json_ok=False, tries=3):
            FakeNSE.urls.append(url)
            return {'marketState': [{'last': 24000}], 'giftnifty': g} if url == ss.GIFT_STATUS_URL else None
    import threading
    for th in threading.enumerate():
        if th.name == 'gift-nifty': th.join(timeout=20)      # a background fetch started by an earlier test must not race this one
    monkeypatch.setattr(ss, '_gift_nse', FakeNSE())
    monkeypatch.setattr(ss, '_gift', {'at': 0.0, 'value': None, 'busy': True, 'warned': 0.0})
    ss._gift_fetch()
    assert ss._gift['value']['price'] == 25012.5 and FakeNSE.urls == [ss.GIFT_STATUS_URL]


def test_dashboard_gift_sanity_guard_and_sector_movers(monkeypatch):
    from app import sector_service as ss
    monkeypatch.setattr(ss, 'gift_nifty', lambda: {'price': 8210.65, 'chg': None, 'pct': None})         # a wrong field far from Nifty
    out = ss.dashboard_summary(lite=True)
    assert not any(x['name'] == 'GIFT NIFTY' for x in out['indices'])
    assert 'sector_movers' in out


def test_dashboard_lite_carries_day_line_and_breadth():
    from app import sector_service as ss
    out = ss.dashboard_summary(lite=True)
    assert 'breadth' in out and 'desk' not in out
    assert all('day' in x for x in out['indices'])


def test_breadth_counts_unique_stocks_and_needs_enough_of_them():
    from app import sector_service as ss
    mk = lambda i, v: {'s': 'S%d' % i, 'r': {'today': v}}
    few = {'sectors': [{'members_mini': [mk(i, 1) for i in range(10)]}]}
    assert ss._dash_breadth(few, []) is None
    ov = {'sectors': [{'members_mini': [mk(i, 1.5 if i < 40 else -0.5) for i in range(60)]}, {'members_mini': [mk(0, 9)]}]}
    b = ss._dash_breadth(ov, [{'name': 'NIFTY 50', 'r': {'today': 0.9}}])
    assert (b['n'], b['up'], b['dn'], b['up1']) == (60, 40, 20, 40) and b['gap'] is not None


def test_gift_nifty_first_call_waits_for_the_first_reading(monkeypatch):
    import time as _t
    from app import sector_service as s
    monkeypatch.setitem(s._gift, 'value', None); monkeypatch.setitem(s._gift, 'at', 0.0); monkeypatch.setitem(s._gift, 'busy', False); monkeypatch.setitem(s._gift_loop, 'on', False)
    def fake():
        _t.sleep(0.4)
        with s._lock: s._gift.update(at=_t.time(), busy=False, value={'price': 25000.0, 'chg': 10.0, 'pct': 0.04, 'at': 'x', 'moved': _t.time()})
        _t.sleep(3600)                                                              # the keep-warm loop would sleep here; test thread is a daemon
    monkeypatch.setattr(s, '_gift_fetch', fake)
    monkeypatch.setattr(s.time, 'sleep', s.time.sleep)
    g = s.gift_nifty()
    assert g and g['price'] == 25000.0                                              # first call returns the reading instead of None


def test_stock_movers_top_gainers_and_losers():
    from app import sector_service as s
    members = [{'s': f'S{i:03d}', 'p': 100.0 + i, 'r': {'today': float(i - 30)}} for i in range(60)]
    ov = {'sectors': [{'name': 'Banks', 'members_mini': members[:30]}, {'name': 'IT', 'members_mini': members[30:] + members[:3]}]}
    m = s._stock_movers(ov, 3)
    assert [r['s'] for r in m['gain']] == ['S059', 'S058', 'S057'] and [r['s'] for r in m['lose']] == ['S000', 'S001', 'S002'] and m['n'] == 60
    assert m['gain'][0]['pct'] == 29.0 and s._stock_movers({'sectors': [{'name': 'X', 'members_mini': members[:10]}]}) is None
