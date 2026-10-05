"""Isolated cached sector workspace. All network work is in one daemon thread.

Five-year daily candles stay on disk, not in the scanner's process heap. History
hydration runs outside cash-market hours and yields the shared heavy-work slot.
A page/API read only copies cached summaries; it never waits for Kite history.
"""
from __future__ import annotations
import copy
import datetime as dt
import hashlib
import logging
import threading
import time
from pathlib import Path
import pandas as pd
from . import config, kite_auth, scanner, news, research_runtime
from . import sector_analytics as a
from . import sector_data as data

log=logging.getLogger(__name__)
ROOT=Path(config.V12_STORAGE_ROOT)/'sector_analysis'
SEED=Path(__file__).parent/'data'/'sector_catalogue.json'
_lock=threading.RLock()
_started=False
_requested_intraday=set()
_intraday={}
_summaries={}
_tokens={}
_instrument_symbols={}
_state={'histories':{},'quotes':{},'catalogue':data.load_json(SEED,[]),'running':False,'done':0,'total':0,'error':None,'updated_at':None,'quote_at':None}


def _path(symbol):
    return ROOT/'daily'/(hashlib.sha256(symbol.encode()).hexdigest()[:24]+'.json')


def _history(symbol):
    with _lock:
        seeded=_state.get('histories',{}).get(symbol)
    return seeded if seeded is not None else data.load_json(_path(symbol),{}).get('candles',[])


def _status():
    keys=('running','done','total','error','updated_at','quote_at')
    with _lock:
        return {k:_state.get(k) for k in keys}


def _fresh_timestamp(stamp, now, *, final_quote=False, max_age=180):
    if not stamp: return False
    try:
        t=pd.Timestamp(stamp)
        if t.tzinfo: t=t.tz_convert('Asia/Kolkata').tz_localize(None)
        if t.date()!=now.date(): return False
        if final_quote and now.time()>=dt.time(16,0) and t.time()>=dt.time(15,30): return True
        return -30 <= (now-t.to_pydatetime()).total_seconds() <= max_age
    except (ValueError, TypeError): return False


def _summary(symbol, now):
    with _lock:
        r=copy.deepcopy(_summaries.get(symbol))
    if r is None:
        r=a.summarize([],now)
    elif r.get('quote_fresh') and not _fresh_timestamp(r.get('quote_timestamp'),now,final_quote=True):
        metadata={k:r.get(k) for k in ('quote_timestamp','quote_retrieved_at')}
        r=r.get('_historical') or a.summarize(_history(symbol),now)
        r.update(metadata,quote_fresh=False)
    r.pop('_historical',None)
    r['symbol']=symbol
    return r


def _breadth(members,kind,n):
    valid=[r for r in members if r.get(kind,{}).get(str(n)) is not None and r.get('price') is not None]
    return {'pct':sum(r['price']>r[kind][str(n)] for r in valid)/len(valid)*100 if valid else None,'covered':len(valid)}


def _sector_row(entry, benchmark, now):
    row=_summary(entry['name'],now)
    row.update({k:copy.deepcopy(entry.get(k)) for k in ('id','name','source','member_source','retrieved_at','membership_error')})
    members=[_summary(m['symbol'],now) for m in entry.get('members',[])]
    row['coverage']={'available':sum(r['bars']>0 for r in members),'total':len(members)}
    row['breadth']={str(n):_breadth(members,'sma',n) for n in a.MA_PERIODS}
    row['ema_breadth']={str(n):_breadth(members,'ema',n) for n in a.MA_PERIODS}
    valid=[r['returns']['today'] for r in members if r['returns']['today'] is not None]
    row['advancing_pct']=sum(v>0 for v in valid)/len(valid)*100 if valid else None
    row['median_return']=a.finite(pd.Series(valid,dtype=float).median()) if valid else None
    row['vs_nifty']=a.relative_returns(row,benchmark)
    row['vwap']=None
    return row


def overview():
    now=scanner.now_ist()
    with _lock:
        catalogue=copy.deepcopy(_state['catalogue'])
    benchmark=_summary('NIFTY 50',now)
    return {'sectors':[_sector_row(e,benchmark,now) for e in catalogue],'benchmark':benchmark,
            'status':_status(),'periods':list(a.PERIODS),'as_of':now.isoformat(timespec='seconds'),
            'methodology':{'membership':'Official current constituents; dated cache. Historical breadth uses current membership.',
                'returns':'Price returns, not total returns. Rolling calendar week/month/year; four completed calendar quarters. Daily SMA/EMA use completed bars.',
                'vwap':'5-minute bar typical-price approximation, reset per IST session. Cash indices have no traded-volume VWAP.',
                'adjustment':'Vendor OHLC; independent corporate-action adjustment unverified. Large discontinuities flagged.',
                'rotation':'Relative sector/index ratio return over N sessions; momentum is its change over the preceding N sessions. Descriptive, not a validated trading signal.'}}


def _evidence(row, sector, benchmark):
    observations=[]
    for key,label in [('today','Today'),('1W','Week'),('1M','Month')]:
        value=row.get('returns',{}).get(key)
        if value is not None:
            rel=row.get('vs_sector',{}).get(key)
            observations.append(f'{label}: {value:+.2f}%; '+(f'{rel:+.2f} percentage points vs sector.' if rel is not None else 'sector comparison unavailable.'))
    if row.get('extension_atr') is not None:
        observations.append(f"Price is {row['extension_atr']:+.2f} ATR from SMA20.")
    levels=row.get('levels',{})
    def level(k):
        v=levels.get(k)
        return f'{v:.2f}' if v is not None else 'unavailable'
    articles=news.get_news_for_symbol(row['symbol'],limit=3)
    return {'observed':observations or ['History or fresh quote unavailable.'],
            'explanation':'Price/relative-strength observations describe the move; they do not establish its cause. '+('Related cached headlines below; causal link unverified.' if articles else 'No verified catalyst is available in the news cache.'),
            'watch_next':[f"Strength scenario: sustain above recent swing high {level('swing_high')} with improving relative strength and broad participation.",
                          f"Weakness scenario: lose recent swing low {level('swing_low')}; reassess the structure.",
                          f"Pullback reference: prior-session low {level('previous_low')} and SMA20; scenarios are conditional, not forecasts."],
            'news':articles, 'by_period':{p:[f'{p}: {row["returns"][p]:+.2f}% price return.' if row.get('returns',{}).get(p) is not None else f'{p}: history unavailable.', f'Vs sector: {row.get("vs_sector",{}).get(p):+.2f} percentage points.' if row.get('vs_sector',{}).get(p) is not None else 'Matched sector comparison unavailable.'] for p in a.PERIODS}}


def detail(sector_id, window=20):
    now=scanner.now_ist()
    with _lock:
        entry=next((copy.deepcopy(e) for e in _state['catalogue'] if e['id']==sector_id),None)
    if entry is None: return None
    benchmark=_summary('NIFTY 50',now); sector=_sector_row(entry,benchmark,now)
    members=[]
    calendar=data.load_json(config.V12_EARNINGS_STATE_FILE,{})
    for m in entry.get('members',[]):
        r=_summary(m['symbol'],now); r.update(m)
        r['vs_sector']=a.relative_returns(r,sector); r['vs_nifty']=a.relative_returns(r,benchmark)
        with _lock:
            intraday=copy.deepcopy(_intraday.get(m['symbol'],{}))
        if not _fresh_timestamp(intraday.get('bar_asof') or intraday.get('as_of'),now,max_age=600):
            intraday={}
        r['vwap']=intraday.get('vwap'); r['vwap_at']=intraday.get('as_of')
        r['vwap_distance']=a.change(r['price'],r['vwap'])
        r['volume_ratio']=intraday.get('volume_ratio')
        r['evidence']=_evidence(r,sector,benchmark)
        event=(calendar.get('events') or {}).get(m['symbol'])
        if event and str(event.get('meeting_date','')) >= now.date().isoformat():
            r['event']=event
            r['event_observed_at']=calendar.get('last_refresh_at')
        members.append(r)
    # Prior daily snapshots establish breadth acceleration with valid denominators.
    breadth=a.breadth_series([_history(m['symbol']) for m in members])
    sector['breadth_change']={str(n):(breadth[-1][str(n)]-breadth[-6][str(n)] if len(breadth)>=6 and breadth[-1][str(n)] is not None and breadth[-6][str(n)] is not None else None) for n in (20,50,200)}
    sector['evidence']=_evidence(sector,sector,benchmark)
    rotations={}
    for entry2 in overview()['sectors']:
        rotations[entry2['id']]=a.rotation_series(_history(entry2['name']),_history('NIFTY 50'),window)
    weights_available=bool(members) and all(a.finite(m.get('weight')) is not None for m in members)
    contributions=[]
    if weights_available:
        for m in members:
            contributions.append({'symbol':m['symbol'],'value':m['weight']*m['returns']['today']/100 if m['returns']['today'] is not None else None})
    return {'sector':sector,'members':members,'chart':a.chart_series(_history(sector['name'])),
            'benchmark_chart':a.chart_series(_history('NIFTY 50')),'breadth_series':breadth,
            'rotation':rotations,'rotation_window':window,'contributions':contributions,
            'contribution_note':'Current-weight approximation; not exact index attribution.' if weights_available else 'Unavailable: official constituent CSV does not supply dated index weights. Equal weights are not substituted.',
            'weight_date':entry.get('retrieved_at') if weights_available else None,'status':_status()}


def stock_chart(symbol):
    with _lock:
        allowed={m['symbol'] for e in _state['catalogue'] for m in e.get('members',[])}
        if symbol not in allowed: return None
        # Queue, never fetch on a Flask request thread.
        if len(_requested_intraday)<20: _requested_intraday.add(symbol)
        intraday=copy.deepcopy(_intraday.get(symbol,{}))
    now=scanner.now_ist()
    intraday['fresh']=_fresh_timestamp(intraday.get('bar_asof') or intraday.get('as_of'),now,max_age=600)
    if not intraday['fresh']:
        intraday['vwap']=None
        intraday['volume_ratio']=None
    return {'symbol':symbol,'chart':a.chart_series(_history(symbol)),'intraday':intraday,
            'status':_status(),'vwap_note':'Approximate 5-minute session VWAP; appears after queued history refresh.'}


def ensure_started():
    global _started
    with _lock:
        if _started: return
        _started=True
        saved=data.load_json(ROOT/'snapshot.json',{})
        _state.update({k:saved[k] for k in _state if k in saved and k!='running'})
        _state['running']=False
        _summaries.update(saved.get('summaries',{}))
        threading.Thread(target=_worker,name='sector-analysis-cache',daemon=True).start()


def _persist():
    with _lock:
        saved={k:copy.deepcopy(v) for k,v in _state.items() if k!='histories'}
        saved['summaries']=copy.deepcopy(_summaries)
    data.save_json(ROOT/'snapshot.json',saved)


def _normal(name):
    return ''.join(c for c in name.upper() if c.isalnum()).replace('INDEX','').replace('SERVICES','SERVICE')


def resolve_tokens(instruments, catalogue):
    by_normal={_normal(r.get('tradingsymbol','')):r for r in instruments}
    aliases={'NIFTY FINANCIAL SERVICES':'NIFTY FIN SERVICE','NIFTY PRIVATE BANK':'NIFTY PVT BANK',
             'NIFTY CONSUMER DURABLES':'NIFTY CONSR DURBL','NIFTY HEALTHCARE':'NIFTY HEALTHCARE',
             'NIFTY OIL AND GAS':'NIFTY OIL AND GAS','NIFTY FINANCIAL SERVICES 25/50':'NIFTY FINSRV25 50'}
    names={'NIFTY 50'}|{e['name'] for e in catalogue}|{m['symbol'] for e in catalogue for m in e.get('members',[])}
    tokens,quotes={},{}
    for name in names:
        resolved=by_normal.get(_normal(name)) or by_normal.get(_normal(aliases.get(name.upper(),'')))
        if resolved:
            tokens[name]=resolved['instrument_token']; quotes[name]='NSE:'+resolved['tradingsymbol']
    return tokens,quotes


def _refresh_quotes(kite, now):
    names=list(_instrument_symbols)
    for start in range(0,len(names),400):
        subset=names[start:start+400]
        quotes=kite.quote([_instrument_symbols[n] for n in subset])
        with _lock:
            for name in subset:
                q=quotes.get(_instrument_symbols[name])
                if q and a.finite(q.get('last_price')):
                    # Preserve the provider timestamp, not merely the retrieval time.
                    stamp=q.get('timestamp') or q.get('last_trade_time')
                    # Undated quotes are explicitly not certified as live.
                    _state['quotes'][name]={'data':q,'retrieved_at':now.isoformat(timespec='seconds'),'timestamp':str(stamp) if stamp else None}
            _state['quote_at']=now.isoformat(timespec='seconds')
        for name in subset:
            _recalculate(name,now)
        time.sleep(1.1)


def _recalculate(name,now):
    with _lock:
        stored=copy.deepcopy(_state['quotes'].get(name,{}))
    q=stored.get('data',{})
    stamp=stored.get('timestamp')
    acceptable=_fresh_timestamp(stamp,now,final_quote=True)
    bars=_history(name)
    historical=a.summarize(bars,now)
    r=a.summarize(bars,now,q) if acceptable else copy.deepcopy(historical)
    r.update(quote_timestamp=stamp,quote_fresh=acceptable,quote_retrieved_at=stored.get('retrieved_at'),_historical=historical)
    with _lock: _summaries[name]=r


def _market_hours(now):
    # Gate even on exchange holidays: under-loading is safer than a history sweep
    # colliding with the live cash-market clock. Quotes themselves prove freshness.
    return now.weekday()<5 and dt.time(9,0)<=now.time()<dt.time(16,0)


def _hydrate(kite, now):
    with _lock:
        catalogue=copy.deepcopy(_state['catalogue']); _state.update(running=True,done=0,total=len(_tokens),error=None)
    failures=[]
    complete=True
    sector_names={e["name"] for e in catalogue}|{"NIFTY 50"}
    ordered=sorted(_tokens.items(),key=lambda item:(item[0] not in sector_names,item[0] != "NIFTY 50",item[0]))
    try:
        for i,(name,token) in enumerate(ordered):
            clock=scanner.now_ist()
            if _market_hours(clock):
                complete=False
                break
            cached=data.load_json(_path(name),{})
            refresh_key=now.date().isoformat()+(' PM' if now.hour>=16 else ' AM')
            if cached.get('fetched_key')==refresh_key and cached.get('token')==token:
                _recalculate(name,clock)
            else:
                # Yield instead of pausing the live scanner or research recorder.
                if not research_runtime.live_scan_slot():
                    complete=False
                    time.sleep(1); continue
                try:
                    start=clock-dt.timedelta(days=1830) if not cached.get('candles') or cached.get('token')!=token else clock-dt.timedelta(days=10)
                    raw=kite.historical_data(token,start,clock,'day')
                    bars={str(b['date'])[:10]:b for b in cached.get('candles',[])}
                    for b in raw:
                        item=dict(b); item['date']=str(item['date']); bars[item['date'][:10]]=item
                    if not raw:
                        complete=False
                        failures.append(name+': no historical bars returned')
                    if cached.get('token') not in (None,token):
                        bars={str(b['date'])[:10]:dict(b,date=str(b['date'])) for b in raw}
                    if bars:
                        data.save_json(_path(name),{'candles':[bars[d] for d in sorted(bars)],'fetched_day':now.date().isoformat(),'fetched_key':refresh_key,'token':token})
                except Exception as exc:
                    failures.append(name+': '+str(exc)[:80])
                    complete=False
                finally:
                    research_runtime.exit_live_scan()
                _recalculate(name,clock)
                time.sleep(1)
            with _lock: _state['done']=i+1
            if i%20==0: _persist()
        with _lock:
            _state['updated_at']=scanner.now_ist().isoformat(timespec='seconds')
            _state['error']='; '.join(failures[:5]) or None
    finally:
        with _lock: _state['running']=False
        _persist()
    return complete


def _load_intraday(kite,now):
    with _lock:
        wanted=list(_requested_intraday)
    for name in wanted:
        token=_tokens.get(name)
        if not token: continue
        try:
            bars=kite.historical_data(token,now-dt.timedelta(days=12),now,'5minute')
            completed_until=pd.Timestamp(now).floor('5min')
            bars=[b for b in bars if pd.Timestamp(b['date']).tz_localize(None) < completed_until]
            series=a.vwap_series(bars)
            df=a.frame(bars)
            current=df[df.index.date==now.date()]
            previous=df[df.index.date!=now.date()]
            cutoff=current.index[-1].time() if len(current) else (completed_until-pd.Timedelta(minutes=5)).time()
            previous=previous[previous.index.time<=cutoff]
            totals=previous.groupby(previous.index.date).volume.sum()
            normal=a.finite(totals.tail(5).median()) if len(totals) else None
            vwap=next((v['value'] for v in reversed(series) if pd.Timestamp(v['time']).date()==now.date() and v['value'] is not None),None)
            with _lock:
                _intraday[name]={'series':series[-1500:],'candles':[dict(b,date=str(b['date'])) for b in bars[-1500:]],'vwap':vwap,'as_of':now.isoformat(timespec='seconds'),'bar_asof':(current.index[-1]+pd.Timedelta(minutes=5)).isoformat() if len(current) else None,
                    'volume_ratio':float(current.volume.sum())/normal if normal else None,'comparison_sessions':len(totals.tail(5))}
                _requested_intraday.discard(name)
        except Exception as exc:
            log.warning('Sector intraday history unavailable for %s: %s',name,exc)
            with _lock: _requested_intraday.discard(name)
        time.sleep(1)


def _worker():
    instrument_day=membership_day=hydrate_day=None
    while True:
        now=scanner.now_ist()
        try:
            kite=kite_auth.get_kite_client()
            if kite:
                day=now.date().isoformat()
                if membership_day!=day and not _market_hours(now):
                    with _lock: previous=copy.deepcopy(_state['catalogue'])
                    try:
                        catalogue=data.refresh_catalogue(previous,now)
                        with _lock: _state['catalogue']=catalogue
                        _persist()
                    except Exception as exc:
                        with _lock: _state['error']='Official membership refresh failed: '+str(exc)[:160]
                    membership_day=day
                if instrument_day!=day:
                    with _lock: catalogue=copy.deepcopy(_state['catalogue'])
                    tokens,quotes=resolve_tokens(kite.instruments('NSE'),catalogue)
                    _tokens.update(tokens); _instrument_symbols.update(quotes); instrument_day=day
                refresh_key=day+(' PM' if now.hour>=16 else ' AM')
                if not _market_hours(now) and hydrate_day!=refresh_key:
                    if _hydrate(kite,now): hydrate_day=refresh_key
                _refresh_quotes(kite,scanner.now_ist())
                _load_intraday(kite,scanner.now_ist())
                _persist()
            else:
                with _lock: _state['error']='Kite login required to refresh sector data; last-good cache remains available.'
        except Exception as exc:
            log.exception('Sector cache refresh failed')
            with _lock: _state['error']=str(exc)[:180]
        time.sleep(60)
