"""Setup A v1.1 (NIFTY-500 dip buy) - evening scanner + paper ledger on Railway (ADMIN ONLY, research/spotting - never places orders).

Port of DJ's laptop scanner_v11.py (rules frozen 3 Oct 2026). The engine (ca_factor / build / record_signals / rebuild_ledger)
is kept line-for-line identical; only file locations changed (laptop Downloads -> <V12 storage root>/v11/).

Daily cycle (IST, weekdays): 19:15-22:30 every 15 min until done -> download today's NSE CM bhavcopy + index closes
(NIFTY 50, INDIA VIX) -> rebuild indicators -> record signals -> rebuild the paper ledger -> summary for /admin/v130.
First start: bootstraps ~470 trading days of bhavcopy from NSE (about 20-30 min, resumable), and seeds the signal
history / paper ledger from the laptop CSVs in app/v11_assets/.

RULES (v1.1) - evening signal (all must hold):
  1. Liquid: top-500 NSE EQ stock by 20-day median traded value, price >= 20, 20-day median value >= Rs 5 crore
  2. Close above its 200-day average            3. Close = 7-day closing low and 5-day fall >= 6.8%
  4. NIFTY 5-day fall >= 2.5%                   5. Market stress score >= 0.468
  6. Stock 60-day daily vol < 4.59%             7. India VIX not up > 50% in 5 days
  8. No results board meeting within 30 days
 Trade: buy next open; +6% target / close 12% below entry / first 7-day closing high / day 20. 2.5% per trade, max 10 open.
"""
from __future__ import annotations

import datetime as dt
import io
import json
import logging
import re
import shutil
import threading
import time
import zipfile
from datetime import date, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

from . import config

log = logging.getLogger(__name__)
ASSETS = Path(__file__).with_name('v11_assets')
ROOT = Path(config.V12_STORAGE_ROOT) / 'v11'
IST = dt.timezone(dt.timedelta(hours=5, minutes=30))
BASE = "https://nsearchives.nseindia.com"
KEEP_DAYS = 470
BOOT_CAL_DAYS = 700            # ~470 NSE trading days
P = dict(top_n=500, min_px=20, min_val=5e7, r5=-0.068, m5=-0.025, score=0.468, mvol_cap=0.3488277, risk=0.04594714, vix_jump=0.5,
         res_days=30, tgt=0.06, stop=0.12, maxd=20, cost_bps=30, slot_pct=2.5, slots=10)
EVENING_START, EVENING_END = 19 * 60 + 15, 22 * 60 + 30

_lock = threading.Lock()
_run_lock = threading.Lock()
_status: dict = {'started': None, 'last_error': None, 'steps': {}, 'bootstrap': None}


def now_ist() -> dt.datetime:
    return dt.datetime.now(IST).replace(tzinfo=None)


def _p(name: str) -> str:
    ROOT.mkdir(parents=True, exist_ok=True)
    return str(ROOT / name)


def _exists(name: str) -> bool:
    return (ROOT / name).exists()


def _nse():
    from .v130_swing import NSE   # shared NSE session (warm-up cookie, retries)
    return NSE()


def _get(s, url, tries=4):
    b = s.get(url, tries=tries)
    return b if (b is not None and len(b) > 50) else None


# ======================================================================= data store
def fetch_day(s, d):
    b = _get(s, f"{BASE}/content/cm/BhavCopy_NSE_CM_0_0_0_{d:%Y%m%d}_F_0000.csv.zip")
    if b is None: return None
    z = zipfile.ZipFile(io.BytesIO(b)); df = pd.read_csv(z.open(z.namelist()[0])); df.columns = [c.strip() for c in df.columns]
    bh = pd.DataFrame({'date': pd.Timestamp(d), 'symbol': df.TckrSymb.str.strip(), 'series': df.SctySrs.str.strip(), 'open': df.OpnPric, 'high': df.HghPric,
                       'low': df.LwPric, 'close': df.ClsPric, 'value': df.TtlTrfVal})
    bh = bh[bh.series == 'EQ'].drop(columns='series')
    ix = _get(s, f"{BASE}/content/indices/ind_close_all_{d:%d%m%Y}.csv"); nf = vx = None
    if ix:
        t = pd.read_csv(io.BytesIO(ix)); t.columns = [c.strip() for c in t.columns]; nm = t['Index Name'].astype(str).str.strip().str.upper()
        r0 = t[nm == 'NIFTY 50']
        if len(r0): nf = pd.DataFrame([{'date': pd.Timestamp(d), 'open': float(r0.iloc[0]['Open Index Value']), 'close': float(r0.iloc[0]['Closing Index Value'])}])
        r1 = t[nm == 'INDIA VIX']
        if len(r1): vx = pd.DataFrame([{'date': pd.Timestamp(d), 'vix': float(r1.iloc[0]['Closing Index Value'])}])
    return bh, nf, vx


def refresh_calendar(s, force=False):
    """Weekly: NSE corporate actions (splits/bonus, last 2 years) and board meetings (results dates, 15 days back .. 60 ahead)."""
    f = _p('calendar_stamp.txt')
    if not force and Path(f).exists() and time.time() - Path(f).stat().st_mtime < 6 * 86400: return 'cached'
    api = lambda u: _get(s, u, 3)
    t0 = now_ist().date()
    rows = []
    for a in range(0, 730, 180):
        fr, to = t0 - timedelta(days=a + 180), t0 - timedelta(days=a)
        b = api(f"https://www.nseindia.com/api/corporates-corporateActions?index=equities&from_date={fr:%d-%m-%Y}&to_date={to:%d-%m-%Y}")
        if b and b.strip()[:1] in (b'[', b'{'):
            j = json.loads(b); rows += j if isinstance(j, list) else j.get('data', [])
        time.sleep(0.8)
    if rows:
        new = pd.DataFrame(rows); old = pd.read_csv(_p('corp_actions.csv')) if _exists('corp_actions.csv') else pd.DataFrame()
        pd.concat([old, new]).drop_duplicates(subset=[c for c in ('symbol', 'exDate', 'subject') if c in new.columns]).to_csv(_p('corp_actions.csv'), index=False)
    b = api(f"https://www.nseindia.com/api/corporate-board-meetings?index=equities&from_date={t0 - timedelta(days=15):%d-%m-%Y}&to_date={t0 + timedelta(days=60):%d-%m-%Y}")
    nbm = 0
    if b and b.strip()[:1] in (b'[', b'{'):
        j = json.loads(b); j = j if isinstance(j, list) else j.get('data', [])
        if j:
            new = pd.DataFrame(j)[['bm_symbol', 'bm_date', 'bm_purpose']]; nbm = len(new)
            old = pd.read_csv(_p('board_meetings.csv')) if _exists('board_meetings.csv') else pd.DataFrame()
            pd.concat([old, new]).drop_duplicates().to_csv(_p('board_meetings.csv'), index=False)
    eq = _get(s, f"{BASE}/content/equities/EQUITY_L.csv", 3)
    if eq: Path(_p('EQUITY_L.csv')).write_bytes(eq)
    fo = _get(s, f"{BASE}/content/fo/fo_mktlots.csv", 3)
    if fo: Path(_p('fo_mktlots.csv')).write_bytes(fo)
    # only stamp when the results calendar actually arrived, so a failed refresh is retried the next evening
    if nbm: Path(f).write_text(str(t0))
    return f'corp actions {len(rows)}, board meetings {nbm}, EQUITY_L {"ok" if eq else "missing"}, F&O list {"ok" if fo else "missing"}'


def _load():
    return pd.read_pickle(_p('bhav.pkl')), pd.read_pickle(_p('nifty.pkl')), pd.read_pickle(_p('vix.pkl'))


def _save(bh, nf, vx):
    bh = bh.drop_duplicates(['date', 'symbol'], keep='last'); nf = nf.drop_duplicates('date', keep='last').sort_values('date')
    vx = vx.drop_duplicates('date', keep='last').sort_values('date')
    u = sorted(bh.date.unique()); bh = bh[bh.date >= u[-min(KEEP_DAYS, len(u))]]
    for df, name in ((bh, 'bhav'), (nf, 'nifty'), (vx, 'vix')):
        tmp = _p(name + '.tmp.pkl'); df.to_pickle(tmp); Path(tmp).replace(_p(name + '.pkl'))
    return bh, nf, vx


def bootstrapped() -> bool:
    return _exists('bhav.pkl') and _exists('nifty.pkl') and _exists('vix.pkl')


def bootstrap(s=None, progress=None, start: date | None = None) -> str:
    """One-time: download ~470 trading days of NSE bhavcopy + index closes. Resumable (saves every 20 days)."""
    s = s or _nse()
    if bootstrapped(): return 'already bootstrapped'
    part = Path(_p('boot_partial.pkl')); partn = Path(_p('boot_partial_nifty.pkl')); partv = Path(_p('boot_partial_vix.pkl'))
    bhs, nfs, vxs = [], [], []; d = start or (now_ist().date() - timedelta(days=BOOT_CAL_DAYS))
    if part.exists() and partn.exists():
        bhs.append(pd.read_pickle(part)); nfs.append(pd.read_pickle(partn))
        if partv.exists(): vxs.append(pd.read_pickle(partv))
        d = max(bhs[0].date.max().date(), nfs[0].date.max().date()) + timedelta(days=1)
    today = now_ist().date(); n = 0; miss = 0

    def flush():
        pd.concat(bhs).to_pickle(part); pd.concat(nfs).to_pickle(partn)
        if vxs: pd.concat(vxs).to_pickle(partv)

    while d <= today:
        if d.weekday() < 5:
            got = fetch_day(s, d)
            if got is not None:
                b, n_, v_ = got
                if n_ is not None:
                    bhs.append(b); nfs.append(n_); n += 1
                    if v_ is not None: vxs.append(v_)
                    if n % 20 == 0:
                        flush(); _status['bootstrap'] = f'downloading: {n} days, at {d}'
                        if progress: progress(n, d)
                else: miss += 1
            time.sleep(0.4)
        d += timedelta(days=1)
    if not bhs or not nfs: raise RuntimeError('bootstrap: no NSE files downloaded (NSE unreachable?)')
    bh = pd.concat(bhs); nf = pd.concat(nfs); vx = pd.concat(vxs) if vxs else pd.DataFrame(columns=['date', 'vix'])
    nd = bh.date.nunique()
    if nd < 260: flush(); raise RuntimeError(f'bootstrap: only {nd} trading days downloaded so far - will resume')
    _save(bh, nf, vx)
    for f in (part, partn, partv):
        try: f.unlink()
        except FileNotFoundError: pass
    try: refresh_calendar(s, force=True)
    except Exception as e: log.warning('v11 calendar refresh failed: %s', e)  # noqa: BLE001
    _status['bootstrap'] = f'done: {nd} trading days ({bh.date.min().date()} .. {bh.date.max().date()})'
    return _status['bootstrap']


def update_store(s=None):
    s = s or _nse()
    bh, nf, vx = _load()
    last = min(bh.date.max(), nf.date.max()).date(); d = last + timedelta(days=1); added = 0; note = ''
    today = now_ist().date()
    while d <= today:
        if d.weekday() < 5:
            got = fetch_day(s, d)
            if got is not None:
                b, n_, v_ = got
                if n_ is None: note = f'{d}: NIFTY close not on NSE yet'; break
                bh = pd.concat([bh, b]); nf = pd.concat([nf, n_]); added += 1
                if v_ is not None: vx = pd.concat([vx, v_])
        d += timedelta(days=1); time.sleep(0.4)
    try: cal = refresh_calendar(s)
    except Exception as e: cal = f'calendar refresh failed (using cached): {e}'  # noqa: BLE001
    bh, nf, vx = _save(bh, nf, vx)
    return bh, nf, vx, dict(added=added, note=note, calendar=cal)


# ======================================================================= v1.1 engine (identical to scanner_v11.py)
def ca_factor(subj):
    subj = str(subj); f = 1.0; hit = False
    m = re.search(r'Bonus\s*(\d+)\s*:\s*(\d+)', subj, re.I)
    if m: a_, b_ = float(m.group(1)), float(m.group(2)); f *= b_ / (a_ + b_); hit = True
    m = re.search(r'(?:Split|Sub-?Division|Consolidation).*?Rs\.?\s*([\d.]+).*?R[es]\.?\s*([\d.]+)', subj, re.I)
    if m:
        old, new_ = float(m.group(1)), float(m.group(2))
        if old > 0 and new_ > 0: f *= new_ / old; hit = True
    return f if hit else np.nan


def build(bh, nf, vx):
    days = pd.DatetimeIndex(sorted(set(bh.date) & set(nf.date))); bh = bh[bh.date.isin(days)]
    pv = lambda c: bh.pivot(index='date', columns='symbol', values=c).reindex(days)
    Cdf = pv('close'); syms = np.array(Cdf.columns)
    O, H, L, C, VAL = (pv(c).to_numpy(float) for c in ('open', 'high', 'low', 'close', 'value'))
    T, S = C.shape; adj = np.ones_like(C); sidx = {x: i for i, x in enumerate(syms)}; didx = {d: i for i, d in enumerate(days)}
    if _exists('corp_actions.csv'):
        ca = pd.read_csv(_p('corp_actions.csv')); ca['exDate'] = pd.to_datetime(ca.exDate, format='%d-%b-%Y', errors='coerce')
        ca = ca.dropna(subset=['exDate']); ca = ca[ca.symbol.isin(sidx) & ca.exDate.isin(didx)]
        done = set()
        for r in ca.itertuples():
            j, t0 = sidx[r.symbol], didx[r.exDate]
            if t0 == 0 or (r.symbol, r.exDate) in done: continue
            obs = O[t0, j] / C[t0 - 1, j] if np.isfinite(O[t0, j]) and np.isfinite(C[t0 - 1, j]) else np.nan
            f = ca_factor(r.subject)
            if np.isfinite(f): f_ = obs if (np.isfinite(obs) and abs(np.log(obs / f)) > 0.25) else f
            elif np.isfinite(obs) and abs(np.log(obs)) > 0.15: f_ = obs           # demerger / rights etc.: observed gap
            else: continue
            adj[:t0, j] *= f_; done.add((r.symbol, r.exDate))
    O, H, L, C = O * adj, H * adj, L * adj, C * adj
    M = nf.set_index('date').close.reindex(days).astype(float)
    V = vx.set_index('date').vix.reindex(days).ffill(limit=3).astype(float)
    lag = lambda a, k=1: np.vstack([np.full((k, S), np.nan), a[:-k]])
    roll = lambda a, n, f, mp=None: getattr(pd.DataFrame(a).rolling(n, min_periods=mp or n), f)().to_numpy()
    medval = roll(VAL, 20, 'median', 10); rk = pd.DataFrame(lag(medval)).rank(axis=1, ascending=False).to_numpy()
    liq = (rk <= P['top_n']) & (C >= P['min_px'])
    S200 = roll(C, 200, 'mean'); CL7 = roll(C, 7, 'min'); CH7 = roll(C, 7, 'max')
    r1 = np.log(C / lag(C)); sig60 = roll(r1, 60, 'std', 40); R5 = np.log(C / lag(C, 5))
    mr = np.log(M).diff(); m5 = np.log(M / M.shift(5)).to_numpy(); m20 = np.log(M / M.shift(20)).to_numpy()
    mvol = (mr.rolling(20).std() * np.sqrt(250)).to_numpy(); vchg = np.log(V / V.shift(5)).to_numpy()
    base = liq & (C > S200) & (C <= CL7)
    ncand = base.sum(1) / np.maximum(liq.sum(1), 1)
    score = np.maximum(m20, np.abs(np.minimum(ncand, 2 * m20)) + ncand) + np.minimum(P['mvol_cap'], mvol)
    # results within 30 days
    res = np.zeros_like(C, dtype=bool)
    if _exists('board_meetings.csv'):
        bm = pd.read_csv(_p('board_meetings.csv')); bm = bm[bm.bm_purpose.astype(str).str.contains('Result', case=False, na=False)]
        bm['d'] = pd.to_datetime(bm.bm_date, format='%d-%b-%Y', errors='coerce'); bm = bm.dropna(subset=['d'])
        for sym, g in bm.groupby('bm_symbol'):
            if sym not in sidx: continue
            j = sidx[sym]; ds = np.sort(g.d.unique())
            i = np.searchsorted(ds, days.values); ok = i < len(ds)
            nxt = np.where(ok, ds[np.minimum(i, len(ds) - 1)], np.datetime64('NaT'))
            res[:, j] = ok & ((nxt - days.values) <= np.timedelta64(P['res_days'], 'D'))
    # companies only (ETFs such as GOLDBEES / NIFTYBEES are not trades for this setup)
    co = None
    if _exists('EQUITY_L.csv'):
        try:
            t_ = pd.read_csv(_p('EQUITY_L.csv')); t_.columns = [c.strip() for c in t_.columns]; co = set(t_.SYMBOL.astype(str).str.strip())
        except Exception: co = None  # noqa: BLE001
    etf = re.compile(r'BEES$|ETF|IETF|^GOLD1$|GOLDCASE|SILVERCASE|^SETF|AXISGOLD|HDFCGOLD|GOLDSHARE|LIQUID|MON100|MAFANG|NASDAQ|HNGSNG|BHARAT22|^CPSE|SENSEX|^NIFTY|MID150|NEXT50|^MOM\d')
    is_co = np.array([(x in co) if co else not etf.search(x) for x in syms])
    sig = (is_co[None, :] & base & (R5 <= P['r5']) & (m5[:, None] <= P['m5']) & (score[:, None] >= P['score']) & (sig60 < P['risk'])
           & ~(vchg[:, None] > P['vix_jump']) & (medval >= P['min_val']) & ~res)
    return dict(days=days, syms=syms, O=O, H=H, L=L, C=C, CH7=CH7, R5=R5, sig=sig, score=score, ncand=ncand, m5=m5, m20=m20, mvol=mvol, vchg=vchg,
                vix=V.to_numpy(), nifty=M.to_numpy(), base=base, liq=liq, sig60=sig60, medval=medval, res=res, adj=adj)


# ======================================================================= paper ledger
LCOLS = ['signal_date', 'symbol', 'source', 'signal_close', 'r5_pct', 'entry_date', 'entry_px', 'exit_date', 'exit_px', 'exit_reason',
         'days_held', 'ret_bps', 'win', 'status', 'last_close', 'open_ret_bps', 'my_entry', 'my_exit', 'note']
HIST, LEDGER = 'v11_signals_history.csv', 'v11_paper_ledger.csv'


def seed_files() -> None:
    """First start on Railway: carry over the laptop's signal history and paper ledger (incl. any my_entry / note columns)."""
    for f in (HIST, LEDGER):
        if not _exists(f) and (ASSETS / f).exists(): shutil.copyfile(ASSETS / f, _p(f))


def record_signals(E, t_list, source):
    f = _p(HIST)
    H_ = pd.read_csv(f, parse_dates=['signal_date']) if Path(f).exists() else pd.DataFrame(columns=['signal_date', 'symbol', 'source', 'signal_close', 'r5_pct', 'score'])
    rows = []
    for t in t_list:
        for j in np.where(E['sig'][t])[0]:
            rows.append(dict(signal_date=E['days'][t], symbol=E['syms'][j], source=source, signal_close=round(E['C'][t, j] / E['adj'][t, j], 2),
                             r5_pct=round(E['R5'][t, j] * 100, 1), score=round(E['score'][t], 3)))
    if rows:
        N = pd.DataFrame(rows)
        key = lambda d: [f"{pd.Timestamp(a).date()}|{b}" for a, b in zip(d.signal_date, d.symbol)]
        seen = set(key(H_)); H_ = pd.concat([H_, N[[k not in seen for k in key(N)]]], ignore_index=True)
    H_['signal_date'] = pd.to_datetime(H_.signal_date)
    H_.sort_values(['signal_date', 'r5_pct']).to_csv(f, index=False); return H_


def rebuild_ledger(E, Hs):
    """Deterministic paper ledger from the signal history: entry next open, exits per rules, max 10 open (biggest 5-day fall first)."""
    f = _p(LEDGER)
    old = pd.read_csv(f, parse_dates=['signal_date']) if Path(f).exists() else pd.DataFrame(columns=LCOLS)
    mine = {(str(r.signal_date.date()), r.symbol): (r.my_entry, r.my_exit, r.note) for r in old.itertuples()} if len(old) else {}
    days = list(E['days']); sidx = {x: i for i, x in enumerate(E['syms'])}; T = len(days)
    open_until = []; out = []
    for d, g in Hs.sort_values(['signal_date', 'r5_pct']).groupby('signal_date', sort=True):
        if d not in days: continue
        t0 = days.index(d); open_until = [x for x in open_until if x > t0]
        for r in g.itertuples():
            rec = dict(signal_date=d, symbol=r.symbol, source=r.source, signal_close=r.signal_close, r5_pct=r.r5_pct, status='skipped (10 open)')
            if r.symbol not in sidx: continue
            j = sidx[r.symbol]
            if len(open_until) >= P['slots']: out.append(rec); continue
            if t0 + 1 >= T:
                rec['status'] = 'BUY at next open'; out.append(rec); open_until.append(10 ** 9); continue
            adj = E['adj'][:, j]; ea = E['O'][t0 + 1, j]; rec.update(entry_date=days[t0 + 1].date(), entry_px=round(ea / adj[t0 + 1], 2))
            ex = None
            for k in range(1, P['maxd'] + 1):
                i = t0 + k
                if i >= T: break
                c = E['C'][i, j]
                if not np.isfinite(c): continue
                if E['H'][i, j] >= ea * (1 + P['tgt']): ex = (i, ea * (1 + P['tgt']), f"target +{P['tgt']:.0%}"); break
                if c <= ea * (1 - P['stop']): ex = (i, c, f"stop -{P['stop']:.0%}"); break
                if c >= E['CH7'][i, j] - 1e-9: ex = (i, c, '7-day closing high'); break
                if k == P['maxd']: ex = (i, c, 'day 20'); break
            if ex:
                i, px, why = ex; ret = np.log(px / ea) * 1e4 - P['cost_bps']
                rec.update(exit_date=days[i].date(), exit_px=round(px / adj[i], 2), exit_reason=why, days_held=i - t0, ret_bps=round(ret), win=int(ret > 0), status='closed')
                open_until.append(i)
            else:
                lc = np.nanmax(np.where(np.isfinite(E['C'][t0 + 1:, j]), np.arange(t0 + 1, T), -1)); lcp = E['C'][lc, j]
                rec.update(status=f'OPEN day {T - 1 - t0}', last_close=round(lcp / adj[lc], 2), open_ret_bps=round(np.log(lcp / ea) * 1e4 - P['cost_bps']))
                open_until.append(10 ** 9)
            out.append(rec)
    L = pd.DataFrame(out, columns=LCOLS)
    for k, r in L.iterrows():
        m = mine.get((str(pd.Timestamp(r.signal_date).date()), r.symbol))
        if m: L.loc[k, ['my_entry', 'my_exit', 'note']] = m
    L.to_csv(f, index=False); return L


def fo_set():
    if not _exists('fo_mktlots.csv'): return set()
    try:
        t = pd.read_csv(_p('fo_mktlots.csv')); t.columns = [c.strip() for c in t.columns]; col = [c for c in t.columns if 'SYMBOL' in c.upper()][0]
        return set(t[col].astype(str).str.strip())
    except Exception: return set()  # noqa: BLE001


# ======================================================================= evening run
def _f(x, nd=4):
    try:
        x = float(x); return None if not np.isfinite(x) else round(x, nd)
    except (TypeError, ValueError):
        return None


def summarize(E, L, Hs) -> dict:
    t = len(E['days']) - 1; day = E['days'][t]; fo = fo_set()
    sc, m5, vchg = _f(E['score'][t]), _f(E['m5'][t]), _f(E['vchg'][t])
    tonight = Hs[pd.to_datetime(Hs.signal_date) == day]
    conds = [
        dict(name='Market stress score', value=sc, need=f">= {P['score']}", ok=sc is not None and sc >= P['score']),
        dict(name='NIFTY 5-day change', value=m5, need=f"<= {P['m5']:.1%}", ok=m5 is not None and m5 <= P['m5'], pct=True),
        dict(name='India VIX 5-day change', value=vchg, need=f"<= +{P['vix_jump']:.0%}", ok=not (vchg is not None and vchg > P['vix_jump']), pct=True),
        dict(name='Stocks at a 7-day low above 200-DMA', value=_f(E['ncand'][t]), need='share of liquid 500', ok=None, pct=True),
    ]
    C_ = L[L.status == 'closed']
    st = lambda d: dict(n=int(len(d)), win=_f(d.win.astype(float).mean(), 3) if len(d) else None, avg_bps=_f(d.ret_bps.astype(float).mean(), 0) if len(d) else None)
    last30 = C_.sort_values('exit_date').tail(30); h30 = last30.win.astype(float).mean() if len(last30) else None
    rec = lambda df: json.loads(df.to_json(orient='records', date_format='iso'))
    open_ = L[L.status.astype(str).str.startswith(('OPEN', 'BUY'))].copy()
    open_['fo'] = open_.symbol.isin(fo)
    tn = tonight.copy(); tn['fo'] = tn.symbol.isin(fo)
    eq = C_.sort_values('exit_date').assign(pnl=lambda d: d.ret_bps.astype(float) / 1e4 * P['slot_pct'])
    return dict(date=str(day.date()), computed_at=str(now_ist().replace(microsecond=0)), score=sc, m5=m5, m20=_f(E['m20'][t]), mvol=_f(E['mvol'][t]),
                vchg=vchg, vix=_f(E['vix'][t], 2), nifty=_f(E['nifty'][t], 2), ncand=_f(E['ncand'][t]), conditions=conds,
                market_ok=all(c['ok'] for c in conds if c['ok'] is not None), n_liquid=int(E['liq'][t].sum()), n_base=int(E['base'][t].sum()),
                tonight=rec(tn), open=rec(open_), closed=rec(C_.sort_values('exit_date', ascending=False).head(40)),
                score_all=st(C_), score_live=st(C_[C_.source == 'live']), score_replay=st(C_[C_.source != 'live']),
                hit30=_f(h30, 3), flag='Watch' if (len(last30) >= 15 and h30 is not None and h30 < .65) else 'Normal',
                equity_pct=_f(eq.pnl.sum(), 3) if len(eq) else 0.0, equity=[_f(x, 3) for x in eq.pnl.cumsum()],
                skipped=int((L.status.astype(str).str.startswith('skipped')).sum()), days_in_store=len(E['days']),
                store_from=str(E['days'][0].date()))


def _read_summary() -> dict | None:
    try:
        return json.loads(Path(_p('summary.json')).read_text()) if _exists('summary.json') else None
    except Exception: return None  # noqa: BLE001


def run_evening(s=None, when: date | None = None, *, require_today=True) -> str:
    """Download today's NSE files, scan, update the paper ledger. Idempotent per date; raises (-> retried) until today's files are out."""
    with _run_lock:
        today = when or now_ist().date()
        prev = _read_summary()
        if require_today and prev and prev.get('date') == str(today): return f'already done for {today}'
        s = s or _nse()
        if not bootstrapped(): bootstrap(s)
        seed_files()
        bh, nf, vx, info = update_store(s)
        E = build(bh, nf, vx); t = len(E['days']) - 1; day = E['days'][t].date()
        if require_today and day < today:
            raise RuntimeError(f'NSE files for {today} not out yet (store ends {day}){" - " + info["note"] if info["note"] else ""}')
        # record every evening since the last scanned one (catches up a missed evening, max 5 days)
        last = pd.Timestamp(prev['date']) if prev and prev.get('date') else E['days'][t]
        ts = [i for i in range(max(260, t - 4), t + 1) if E['days'][i] > last] or [t]
        Hs = record_signals(E, ts, 'live')
        L = rebuild_ledger(E, Hs)
        summ = summarize(E, L, Hs); summ['update'] = info; summ['scanned_days'] = [str(E['days'][i].date()) for i in ts]
        tmp = Path(_p('summary.tmp')); tmp.write_text(json.dumps(summ, indent=1, default=str)); tmp.replace(_p('summary.json'))
        with open(_p('runs.jsonl'), 'a', encoding='utf-8') as fh:
            fh.write(json.dumps(dict(at=summ['computed_at'], date=summ['date'], score=summ['score'], m5=summ['m5'], vchg=summ['vchg'],
                                     signals=[r['symbol'] for r in summ['tonight']], update=info), default=str) + '\n')
        return f"{day}: stress {summ['score']}, NIFTY 5d {summ['m5']}, signals {len(summ['tonight'])}"


# ======================================================================= scheduler
def _step(name: str, fn, *a, **k):
    try:
        out = fn(*a, **k); _status['steps'][name] = {'ok': True, 'at': str(now_ist().replace(microsecond=0)), 'result': str(out)[:300]}; return True
    except Exception as exc:  # noqa: BLE001
        log.warning('v11 step %s: %s', name, exc)
        _status['steps'][name] = {'ok': False, 'at': str(now_ist().replace(microsecond=0)), 'error': str(exc)[:300]}; _status['last_error'] = str(exc)[:300]
        return False


def _loop():
    done: dict = {}; boot_next = 0.0
    while True:
        try:
            now = now_ist(); d = now.date(); hm = now.hour * 60 + now.minute
            in_market = d.weekday() < 5 and 9 * 60 <= hm <= 15 * 60 + 40
            if not bootstrapped() and not in_market and time.time() >= boot_next:
                if not _step('bootstrap', bootstrap): boot_next = time.time() + 1800
            elif d.weekday() < 5 and EVENING_START <= hm <= EVENING_END and not done.get(str(d)) and (hm - EVENING_START) % 15 == 0:
                done[str(d)] = _step('evening', run_evening)
        except Exception:  # noqa: BLE001
            log.exception('v11 scheduler tick failed')
        time.sleep(60)


def start_once() -> None:
    import os
    if os.getenv('PYTEST_CURRENT_TEST') or os.getenv('DBI_V11_DISABLED') == '1': return   # never hit NSE from tests
    with _lock:
        if _status.get('started'): return
        _status['started'] = str(now_ist().replace(microsecond=0))
    threading.Thread(target=_loop, daemon=True, name='v11-setup-a').start()


def run_now_async() -> str:
    """Owner 'Run now' button: same evening run in a background thread (any time; uses the latest NSE day available)."""
    if _run_lock.locked(): return 'a run is already in progress'
    threading.Thread(target=lambda: _step('manual', run_evening, require_today=False), daemon=True, name='v11-manual').start()
    return 'started'


def status() -> dict:
    s = _read_summary()
    nxt = 'tonight 19:15' if now_ist().hour * 60 + now_ist().minute < EVENING_START else 'next weekday 19:15'
    return dict(engine='Setup A v1.1 dip-buy (paper, spotting only - no orders)', storage=str(ROOT), scheduler=_status,
                bootstrapped=bootstrapped(), summary=s, params=P, next_run=nxt, running=_run_lock.locked())
