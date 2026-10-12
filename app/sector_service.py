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
import os
import threading
import time
from pathlib import Path
import pandas as pd
from . import config, kite_auth, scanner, news, research_runtime
from . import sector_analytics as a
from . import sector_data as data
from . import sector_industry as ind

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
_bases={}           # (name, live) -> (key, summary_base): history math done once per day, not every quote cycle
_overview_cache={'key':None,'at':0.0,'value':None}
OVERVIEW_TTL=20     # seconds an overview payload is reused while its inputs are unchanged
_state={'histories':{},'quotes':{},'catalogue':data.load_json(SEED,[]),'fo':None,'running':False,'done':0,'total':0,'error':None,'updated_at':None,'quote_at':None}


# ---- F&O focus (adjustable) ----
FNO_ONLY=True            # sector members = F&O stocks only (the stocks you can trade); the sector index itself is unchanged
MIN_FNO_MEMBERS=3        # hide a sector with fewer F&O stocks than this
DUPLICATE_JACCARD=0.8    # two sectors whose F&O members overlap this much are one sector: keep the larger index
HIDDEN_SECTORS=()        # sector ids to hide by hand, e.g. ('nifty-media',)
_view_cache={'key':None,'value':None}


def fo_symbols(nfo, nse):
    """F&O stocks = NSE equities that have NFO stock futures."""
    names={i['name'] for i in nfo if i.get('instrument_type')=='FUT' and i.get('segment')=='NFO-FUT'}
    return sorted(i['tradingsymbol'] for i in nse if i.get('segment')=='NSE' and i.get('tradingsymbol') in names)


def trim_catalogue(catalogue, fo, *, fno_only=None, min_members=None, jaccard=None, hidden=None):
    """The sectors shown: members restricted to F&O stocks, sectors with too few F&O stocks dropped, near-duplicate
    sectors (same F&O stocks, e.g. Financial Services vs Financial Services 25/50) merged into the larger index.
    Without an F&O list (Kite not yet read) the official catalogue is returned unchanged."""
    fno_only=FNO_ONLY if fno_only is None else fno_only; min_members=MIN_FNO_MEMBERS if min_members is None else min_members
    jaccard=DUPLICATE_JACCARD if jaccard is None else jaccard; hidden=set(HIDDEN_SECTORS if hidden is None else hidden)
    if not fno_only or not fo: return [e for e in catalogue if e['id'] not in hidden]
    fo=set(fo); kept=[]
    for e in sorted(catalogue,key=lambda e:-len(e.get('members',[]))):
        if e['id'] in hidden: continue
        mem=[m for m in e.get('members',[]) if m['symbol'] in fo]
        if len(mem)<min_members: continue
        syms={m['symbol'] for m in mem}
        if any(len(syms&k['_s'])/len(syms|k['_s'])>=jaccard for k in kept): continue
        kept.append(dict(e,members=mem,official_members=len(e.get('members',[])),_s=syms))
    order={e['id']:i for i,e in enumerate(catalogue)}
    return [{k:v for k,v in e.items() if k!='_s'} for e in sorted(kept,key=lambda e:order[e['id']])]


def _view():
    """Trimmed catalogue (cached until the catalogue or F&O list changes). Callers must not mutate it."""
    with _lock:
        cat,fo=_state['catalogue'],_state.get('fo')
        key=(id(cat),len(cat),tuple(fo) if fo else None)
        if _view_cache['key']!=key: _view_cache.update(key=key,value=trim_catalogue(cat,fo))
        return _view_cache['value']


def _path(symbol):
    return ROOT/'daily'/(hashlib.sha256(symbol.encode()).hexdigest()[:24]+'.json')


def _history(symbol):
    with _lock:
        seeded=_state.get('histories',{}).get(symbol)
    return seeded if seeded is not None else data.load_json(_path(symbol),{}).get('candles',[])


def _history_stamp(symbol):
    with _lock:
        seeded=_state.get('histories',{}).get(symbol)
    if seeded is not None: return ('seed',id(seeded),len(seeded))
    try: return os.stat(_path(symbol)).st_mtime_ns
    except OSError: return None


def _base(symbol, now, live):
    """summary_base for `symbol`, recomputed only when its history file changes or the session/day boundary moves."""
    key=(now.date(),now.hour>=16,_history_stamp(symbol))
    with _lock: hit=_bases.get((symbol,live))
    if hit and hit[0]==key: return hit[1]
    b=a.summary_base(_history(symbol),now,live)
    with _lock: _bases[(symbol,live)]=(key,b)
    return b


def _chart_bars(name, now):
    """Daily history plus today's still-forming candle from the latest fresh quote (charts only)."""
    bars=_history(name)
    with _lock:
        stored=copy.deepcopy(_state['quotes'].get(name))
    if not stored or not _fresh_timestamp(stored.get('timestamp'),now,final_quote=True):
        return bars
    q=stored.get('data') or {}
    last=a.finite(q.get('last_price'))
    if last is None:
        return bars
    ohlc=q.get('ohlc') or {}
    o=a.finite(ohlc.get('open')) or last
    h=max(v for v in (a.finite(ohlc.get('high')),last,o) if v is not None)
    l=min(v for v in (a.finite(ohlc.get('low')),last,o) if v is not None and v>0)
    today=now.date().isoformat()
    live={'date':today+' 00:00:00+05:30','open':o,'high':h,'low':l,'close':last,'volume':a.finite(q.get('volume')) or 0}
    return [b for b in bars if str(b.get('date'))[:10]!=today]+[live]


def _trim_chart(ch, n, close_only=False):
    """Keep what the page draws: the last n daily bars, SMA 20/50/200 on those bars, no EMA; 2-decimal prices.
    5 years of OHLC + 8 average series for sector and NIFTY was ~1 MB per sector open."""
    candles=(ch or {}).get('candles',[])[-n:]
    if not candles: return {'candles':[],'sma':{}}
    t0=candles[0]['time']
    r=lambda v: round(v,2) if isinstance(v,(int,float)) else v
    if close_only:
        return {'candles':[{'time':c['time'],'close':r(c['close'])} for c in candles]}
    out={'candles':[{k:r(c[k]) for k in ('time','open','high','low','close')} for c in candles],'sma':{}}
    for k in ('20','50','200'):
        out['sma'][k]=[{'time':p['time'],'value':r(p['value'])} for p in (ch.get('sma') or {}).get(k,[]) if p['time']>=t0]
    return out


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
        r=a.finish_summary(_base(symbol,now,False))
    elif r.get('quote_fresh') and not _fresh_timestamp(r.get('quote_timestamp'),now,final_quote=True):
        metadata={k:r.get(k) for k in ('quote_timestamp','quote_retrieved_at')}
        r=r.get('_historical') or a.finish_summary(_base(symbol,now,False))
        r.update(metadata,quote_fresh=False)
    r.pop('_historical',None)
    r['symbol']=symbol
    return r


def _breadth(members,kind,n):
    valid=[r for r in members if r.get(kind,{}).get(str(n)) is not None and r.get('price') is not None]
    return {'pct':sum(r['price']>r[kind][str(n)] for r in valid)/len(valid)*100 if valid else None,'covered':len(valid)}


COMPARE=('today','1W','1M','3M')      # the windows every leader/laggard is compared on
MOVERS_N=2
LOG_DAYS=60                            # sessions kept in movers_log.json


def tech_read(m):
    """Plain-language technical read of one stock from its completed daily bars + live price. Descriptive, not a forecast:
    trend vs 20/50/200-day averages, 52-week high/low proximity, 20-day breakout/breakdown, stretch from the 20-day average."""
    px=a.finite(m.get('price')); sma=m.get('sma') or {}; lv=m.get('levels') or {}
    if px is None: return {'bias':None,'notes':[]}
    notes=[]; score=0
    s20,s50,s200=(a.finite(sma.get(k)) for k in ('20','50','200'))
    if None not in (s20,s50,s200):
        if px>s20 and px>s50 and px>s200: notes.append('uptrend: above 20/50/200-day averages'); score+=1
        elif px<s20 and px<s50 and px<s200: notes.append('downtrend: below 20/50/200-day averages'); score-=1
        elif px>s200 and px<s20: notes.append('pullback inside an uptrend (above 200D, below 20D)')
        elif px<s200 and px>s20: notes.append('bounce inside a downtrend (below 200D, above 20D)')
    hi,lo=a.finite(lv.get('swing_high')),a.finite(lv.get('swing_low'))
    if hi and px>hi: notes.append(f'breaking out above its 20-day high {hi:.2f}'); score+=1
    elif lo and px<lo: notes.append(f'breaking down below its 20-day low {lo:.2f}'); score-=1
    bh,al=a.finite(m.get('below_high52')),a.finite(m.get('above_low52'))
    if bh is not None and bh>=-3: notes.append(f'near 52-week high ({bh:+.1f}%)'); score+=1
    elif al is not None and al<=3: notes.append(f'near 52-week low ({al:+.1f}% above it)'); score-=1
    ext=a.finite(m.get('extension_atr'))
    if ext is not None and ext>=2.5: notes.append(f'stretched {ext:+.1f} ATR above its 20-day average - pullback risk')
    elif ext is not None and ext<=-2.5: notes.append(f'stretched {ext:+.1f} ATR below its 20-day average - bounce risk')
    return {'bias':'bullish' if score>=2 else 'bearish' if score<=-2 else 'mixed','notes':notes[:3]}


def movers(members, n=MOVERS_N):
    """Top n and bottom n stocks of one sector for every period, each with its today/1W/1M/3M returns and its rank (1 = best)
    in each of those windows, so a leader that is also strong over week/month/quarter stands out from a one-day pop."""
    ranks={}
    for q in COMPARE:
        v=sorted((m for m in members if a.finite((m.get('returns') or {}).get(q)) is not None),key=lambda m:-m['returns'][q])
        ranks[q]={m['symbol']:i+1 for i,m in enumerate(v)}
    out={}
    for p in a.PERIODS:
        v=sorted((m for m in members if a.finite((m.get('returns') or {}).get(p)) is not None),key=lambda m:-m['returns'][p])
        k=min(n,max(1,len(v)//2))                      # small groups (2-3 stocks): 1 leader + 1 laggard, never a loser listed as a leader
        top=v[:k]; bottom=[m for m in reversed(v) if m not in top][:k]
        pick=lambda L:[{'symbol':m['symbol'],'returns':{q:a.finite(m['returns'].get(q)) for q in dict.fromkeys(COMPARE+(p,))},
                        'ranks':{q:ranks[q].get(m['symbol']) for q in COMPARE},'tech':tech_read(m)} for m in L]
        out[p]={'top':pick(top),'bottom':pick(bottom),'n':len(v)}
    return out


def _movers_log():
    return data.load_json(ROOT/'movers_log.json',{}) or {}


def log_movers(rows, day):
    """Once per session (after the close): remember each sector's today top/bottom n, so the page can show how often a stock has led."""
    log_=_movers_log()
    if day in log_: return False
    log_[day]={r['id']:{'top':[x['symbol'] for x in (r.get('movers') or {}).get('today',{}).get('top',[])],
                       'bottom':[x['symbol'] for x in (r.get('movers') or {}).get('today',{}).get('bottom',[])]} for r in rows if r.get('movers')}
    for d in sorted(log_)[:-LOG_DAYS]: log_.pop(d)
    data.save_json(ROOT/'movers_log.json',log_)
    return True


def _attach_history(rows, last=20):
    """'seen' on every mover: in how many of the last `last` logged sessions it was its sector's top-n / bottom-n."""
    log_=_movers_log(); days=sorted(log_)[-last:]
    for r in rows:
        cnt={}
        for d in days:
            s=log_[d].get(r['id']) or {}
            for side in ('top','bottom'):
                for sym in s.get(side,[]): cnt[(sym,side)]=cnt.get((sym,side),0)+1
        for per in (r.get('movers') or {}).values():
            for side in ('top','bottom'):
                for x in per[side]: x['seen']={'top':cnt.get((x['symbol'],'top'),0),'bottom':cnt.get((x['symbol'],'bottom'),0),'of':len(days)}


def industry_groups(summaries, cls):
    """One row per NSE industry group (basic industry, else broad industry) with >= 2 stocks: median return per period,
    share of stocks up, and its top/bottom 2 stocks compared on Today/1W/1M/3M (same shape as a sector row's movers)."""
    by={}; parents={}
    for sym,r in summaries.items():
        g=ind.group_of(cls.get(sym))
        if g:
            by.setdefault(g[0],[]).append(r)
            if g[1]: parents.setdefault(g[0],g[1])
    out=[]
    for name,mem in by.items():
        parent=parents.get(name,'')
        if len(mem)<ind.MIN_GROUP: continue
        rets={}
        for p in a.PERIODS:
            v=[x['returns'].get(p) for x in mem if a.finite((x.get('returns') or {}).get(p)) is not None]
            rets[p]=a.finite(pd.Series(v,dtype=float).median()) if v else None
        today=[x['returns'].get('today') for x in mem if a.finite((x.get('returns') or {}).get('today')) is not None]
        slug=''.join(c if c.isalnum() else '-' for c in name.lower()).strip('-')
        out.append({'id':'ind-'+slug,'name':name,'parent':parent,'n':len(mem),'returns':rets,
                    'advancing_pct':sum(v>0 for v in today)/len(today)*100 if today else None,
                    'symbols':sorted(x['symbol'] for x in mem),'movers':movers(mem)})
    return sorted(out,key=lambda r:r['name'])


def _sector_row(entry, benchmark, now):
    row=_summary(entry['name'],now)
    row.update({k:copy.deepcopy(entry.get(k)) for k in ('id','name','source','member_source','retrieved_at','membership_error')})
    members=[_summary(m['symbol'],now) for m in entry.get('members',[])]
    row['coverage']={'available':sum(r['bars']>0 for r in members),'total':len(members)}
    row['breadth']={str(n):_breadth(members,'sma',n) for n in a.MA_PERIODS}
    row['ema_breadth']={str(n):_breadth(members,'ema',n) for n in a.MA_PERIODS}
    valid=[r['returns']['today'] for r in members if r['returns']['today'] is not None]
    row['advance_decline']=a.advance_decline(members)
    row['advancing_pct']=sum(v>0 for v in valid)/len(valid)*100 if valid else None
    row['median_return']=a.finite(pd.Series(valid,dtype=float).median()) if valid else None
    row['vs_nifty']=a.relative_returns(row,benchmark)
    row['vwap']=None
    row['movers']=movers(members)
    # Light member list so the page can search stocks and show sector membership without a detail call.
    row['official_members']=entry.get('official_members',len(entry.get('members',[])))
    row['members_list']=[{'symbol':m['symbol'],'name':m.get('name') or m['symbol']} for m in entry.get('members',[])]
    row['members_mini']=[_mini(m,now) for m in members]
    return row


def _day_ohlc(symbol, now):
    """Today's open/high/low and previous close from the latest fresh quote (None when the quote is stale)."""
    with _lock:
        stored=_state['quotes'].get(symbol)
        q=copy.deepcopy((stored or {}).get('data') or {})
        stamp=(stored or {}).get('timestamp')
    if not q or not _fresh_timestamp(stamp,now,final_quote=True): return {}
    o=q.get('ohlc') or {}
    r=lambda v: round(v,2) if a.finite(v) is not None else None
    return {'o':r(o.get('open')),'h':r(o.get('high')),'l':r(o.get('low')),'pc':r(o.get('close'))}


def _mini(m, now):
    """Compact per-stock row for the sector pulse donut: price, Today/1W/1M/3M, today's OHLC, technical bias."""
    rt=m.get('returns') or {}
    r=lambda v: round(v,2) if a.finite(v) is not None else None
    t=tech_read(m)
    return {'s':m['symbol'],'p':r(m.get('price')),'r':{q:r(rt.get(q)) for q in COMPARE},'d':_day_ohlc(m['symbol'],now),
            'bias':t.get('bias'),'note':(t.get('notes') or [None])[0]}


EXTRA_INDICES=('INDIA VIX',)       # quoted with the sectors for the dashboard index cards
DASH_INDICES=(('NIFTY 50','NIFTY 50'),('NIFTY BANK','BANK NIFTY'),('NIFTY FINANCIAL SERVICES','FIN NIFTY'),('INDIA VIX','INDIA VIX'),('NIFTY IT','NIFTY IT'))


def dashboard_summary():
    """Light payload for the main dashboard: index cards, sector performance and a per-stock price map (watchlist),
    all derived from the cached sector overview (no extra Kite calls)."""
    ov=overview(); now=scanner.now_ist()
    r2=lambda v: round(v,2) if a.finite(v) is not None else None
    idx=[]
    for name,label in DASH_INDICES:
        sm=_summary(name,now)
        if sm.get('price') is None: continue
        idx.append({'name':name,'label':label,'price':r2(sm.get('price')),'r':{q:r2((sm.get('returns') or {}).get(q)) for q in COMPARE},
                    'd':_day_ohlc(name,now),'id':next((x['id'] for x in ov['sectors'] if x['name']==name),None)})
    sectors=[]; stocks={}
    for x in ov['sectors']:
        mm=x.get('members_mini') or []
        sectors.append({'id':x['id'],'name':x['name'],'r':{q:r2((x.get('returns') or {}).get(q)) for q in COMPARE},'n':len(mm),
                        'up':sum(1 for m in mm if (m['r'].get('today') or 0)>0),'dn':sum(1 for m in mm if (m['r'].get('today') or 0)<0)})
        for m in mm:
            stocks.setdefault(m['s'],dict(m,sector=x['id'],sector_name=x['name']))
    return {'as_of':ov.get('as_of'),'status':ov.get('status'),'indices':idx,'sectors':sectors,'stocks':stocks}


def _overview_key():
    def mt(p):
        try: return os.stat(p).st_mtime_ns
        except OSError: return None
    with _lock:
        return (_state.get('quote_at'),_state.get('updated_at'),_state.get('done'),_state.get('running'),_state.get('error'),
                id(_state['catalogue']),tuple(_state.get('fo') or ()),len(_summaries),mt(ROOT/'industry.json'),mt(ROOT/'movers_log.json'))


def overview():
    """Cached for OVERVIEW_TTL s while nothing it depends on changed: the page polls, the data changes once per quote cycle."""
    key=_overview_key(); t=time.time()
    with _lock:
        c=_overview_cache
        if c['value'] is not None and c['key']==key and t-c['at']<OVERVIEW_TTL: return c['value']
    v=_overview()
    with _lock: _overview_cache.update(key=key,at=t,value=v)
    return v


def _overview():
    now=scanner.now_ist()
    catalogue=copy.deepcopy(_view())
    benchmark=_summary('NIFTY 50',now)
    rows=[_sector_row(e,benchmark,now) for e in catalogue]
    _attach_history(rows)
    syms=sorted({m['symbol'] for e in catalogue for m in e.get('members',[])})
    industries=industry_groups({s:_summary(s,now) for s in syms},ind.load(ROOT))
    return {'sectors':rows,'industries':industries,'benchmark':benchmark,'movers_compare':list(COMPARE),'movers_log_days':len(_movers_log()),
            'status':_status(),'periods':list(a.PERIODS),'as_of':now.isoformat(timespec='seconds'),
            'methodology':{'membership':('F&O focus: members are the F&O stocks of each official index; sectors with fewer than %d F&O stocks or the same F&O stocks as a larger index are hidden. '%MIN_FNO_MEMBERS if FNO_ONLY and _state.get('fo') else '')+'Official current constituents; dated cache. Historical breadth uses current membership.',
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


def detail(sector_id, window=20, lite=False):
    """lite=True: what the sector page shows (members, returns, charts) - skips rotation for every sector,
    the breadth history and per-stock news evidence, which together made each sector open take ~3 s."""
    now=scanner.now_ist()
    with _lock:
        entry=next((copy.deepcopy(e) for e in _view() if e['id']==sector_id),None)
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
        r['evidence']=None if lite else _evidence(r,sector,benchmark)
        event=(calendar.get('events') or {}).get(m['symbol'])
        if event and str(event.get('meeting_date','')) >= now.date().isoformat():
            r['event']=event
            r['event_observed_at']=calendar.get('last_refresh_at')
        members.append(r)
    # Prior daily snapshots establish breadth acceleration with valid denominators.
    breadth=[] if lite else a.breadth_series([_history(m['symbol']) for m in members])
    sector['breadth_change']={str(n):(breadth[-1][str(n)]-breadth[-6][str(n)] if len(breadth)>=6 and breadth[-1][str(n)] is not None and breadth[-6][str(n)] is not None else None) for n in (20,50,200)}
    sector['evidence']=None if lite else _evidence(sector,sector,benchmark)
    rotations={}
    if not lite:
        for entry2 in overview()['sectors']:
            rotations[entry2['id']]=a.rotation_series(_history(entry2['name']),_history('NIFTY 50'),window)
    weights_available=bool(members) and all(a.finite(m.get('weight')) is not None for m in members)
    contributions=[]
    if weights_available:
        for m in members:
            contributions.append({'symbol':m['symbol'],'value':m['weight']*m['returns']['today']/100 if m['returns']['today'] is not None else None})
    chart=a.chart_series(_chart_bars(sector['name'],now)); bench=a.chart_series(_chart_bars('NIFTY 50',now))
    if lite:
        chart=_trim_chart(chart,760); bench=_trim_chart(bench,760,close_only=True)   # 3Y is the longest range drawn
    return {'sector':sector,'members':members,'chart':chart,
            'benchmark_chart':bench,'breadth_series':breadth,
            'rotation':rotations,'rotation_window':window,'contributions':contributions,
            'contribution_note':'Current-weight approximation; not exact index attribution.' if weights_available else 'Unavailable: official constituent CSV does not supply dated index weights. Equal weights are not substituted.',
            'weight_date':entry.get('retrieved_at') if weights_available else None,'status':_status()}


def stock_chart(symbol):
    with _lock:
        allowed={m['symbol'] for e in _view() for m in e.get('members',[])}
        if symbol not in allowed: return None
        # Queue, never fetch on a Flask request thread.
        if len(_requested_intraday)<20: _requested_intraday.add(symbol)
        intraday=copy.deepcopy(_intraday.get(symbol,{}))
    now=scanner.now_ist()
    intraday['fresh']=_fresh_timestamp(intraday.get('bar_asof') or intraday.get('as_of'),now,max_age=600)
    if not intraday['fresh']:
        intraday['vwap']=None
        intraday['volume_ratio']=None
    return {'symbol':symbol,'chart':_trim_chart(a.chart_series(_chart_bars(symbol,now)),260),'intraday':intraday,
            'status':_status(),'vwap_note':'Approximate 5-minute session VWAP; appears after queued history refresh.'}


def sector_intraday(sector_id):
    """Today's 5-minute candles for a sector index and NIFTY 50 (the sector chart's 1D view).
    Queued for the background worker like stock charts; never fetched on the request thread."""
    with _lock:
        entry=next((e for e in _view() if e['id']==sector_id),None)
        if entry is None: return None
        out={}
        for name in (entry['name'],'NIFTY 50'):
            if len(_requested_intraday)<20: _requested_intraday.add(name)
            d=_intraday.get(name,{}); cs=d.get('candles',[])
            day=str(cs[-1]['date'])[:10] if cs else None          # only the latest session is drawn
            cs=[{k:(round(v,2) if isinstance(v,float) else v) for k,v in c.items() if k in ('date','open','high','low','close')} for c in cs if str(c['date'])[:10]==day]
            out[name]={'candles':cs,'as_of':d.get('as_of'),'bar_asof':d.get('bar_asof')}
    return {'sector':entry['name'],'series':out,'note':'Completed 5-minute bars; refreshed by the background worker about once a minute.'}


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
    names={'NIFTY 50'}|set(EXTRA_INDICES)|{e['name'] for e in catalogue}|{m['symbol'] for e in catalogue for m in e.get('members',[])}
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
    historical=a.finish_summary(_base(name,now,False))
    r=a.finish_summary(_base(name,now,bool(q.get('last_price'))),q) if acceptable else copy.deepcopy(historical)
    r.update(quote_timestamp=stamp,quote_fresh=acceptable,quote_retrieved_at=stored.get('retrieved_at'),_historical=historical)
    with _lock: _summaries[name]=r


def _quote_window(now):
    """Live quote polling: weekdays 09:00-15:45 IST (15:30 close plus a short window to capture the final quote)."""
    return now.weekday()<5 and dt.time(9,0)<=now.time()<=dt.time(15,45)


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
    instrument_day=membership_day=hydrate_day=industry_day=None
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
                    nse=kite.instruments('NSE')
                    try:
                        fo=fo_symbols(kite.instruments('NFO'),nse)
                        if fo:
                            with _lock: _state['fo']=fo
                    except Exception:  # noqa: BLE001
                        log.exception('F&O list for the sector page unavailable; keeping the last one')
                    tokens,quotes=resolve_tokens(nse,_view())
                    _tokens.clear(); _instrument_symbols.clear()            # only the shown (F&O) names are quoted and hydrated
                    _tokens.update(tokens); _instrument_symbols.update(quotes); instrument_day=day
                refresh_key=day+(' PM' if now.hour>=16 else ' AM')
                if not _market_hours(now) and hydrate_day!=refresh_key:
                    if _hydrate(kite,now): hydrate_day=refresh_key
                if not _market_hours(scanner.now_ist()) and industry_day!=day:
                    syms={m['symbol'] for e in _view() for m in e.get('members',[])}
                    try:
                        if syms and ind.needs_refresh(ROOT,syms,now.date()):
                            from .v130_swing import NSE
                            ind.refresh(ROOT,syms,NSE(),now.date())
                        industry_day=day
                    except Exception:  # noqa: BLE001
                        log.exception('Industry classification refresh failed')
                if _quote_window(scanner.now_ist()):
                    _refresh_quotes(kite,scanner.now_ist())
                _load_intraday(kite,scanner.now_ist())
                _persist()
                t=scanner.now_ist()
                if t.weekday()<5 and dt.time(15,32)<=t.time()<=dt.time(23,0) and (_state.get('quote_at') or '')[:10]==t.date().isoformat():
                    try:
                        log_movers(_overview()['sectors'],t.date().isoformat())
                    except Exception:  # noqa: BLE001
                        log.exception('Sector movers log failed')
            else:
                with _lock: _state['error']='Kite login required to refresh sector data; last-good cache remains available.'
        except Exception as exc:
            log.exception('Sector cache refresh failed')
            with _lock: _state['error']=str(exc)[:180]
        time.sleep(60)
