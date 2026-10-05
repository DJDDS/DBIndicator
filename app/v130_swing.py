"""V13.0 swing engine - live I/O, scheduler, spotting ledger (ADMIN ONLY, research/spotting - never places orders).

Daily cycle (IST, trading days only; every step fail-soft and retried):
  18:45-22:30  evening plan: Kite daily candles + NSE end-of-day files -> features (v130_core, research-identical)
               -> frozen model score -> gate -> filters/tags/route (v130_rules) -> plan + pending spotting positions
  09:20-09:45  entry: record the actual open, recompute target/stop from it
  10:00-11:15  options: live Kite chain -> strike + premium instruction for picks that route to a CALL
  15:35-16:30  outcomes: evaluate exits for open spotting positions (futures/cash price path, call premium path)
Storage: <V12 storage root>/v130/  (plans.jsonl, ledger.jsonl, state.json, settings.json, panel cache)
"""
from __future__ import annotations

import datetime as dt
import io
import json
import logging
import math
import threading
import time
import zipfile
from pathlib import Path

import numpy as np
import pandas as pd
import requests

from . import config, v130_core as core, v130_rules as rules
from .v130_gbm import TextGBM

log = logging.getLogger(__name__)
ASSETS = Path(__file__).with_name('v130_assets')
ROOT = Path(config.V12_STORAGE_ROOT) / 'v130'
IST = dt.timezone(dt.timedelta(hours=5, minutes=30))
UA = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36',
      'Accept': '*/*', 'Accept-Language': 'en-US,en;q=0.9', 'Referer': 'https://www.nseindia.com/'}
SECTOR_KITE = {'Nifty Financial Services': 'NIFTY FIN SERVICE', 'Nifty IT': 'NIFTY IT', 'Nifty Pharma': 'NIFTY PHARMA', 'Nifty Auto': 'NIFTY AUTO',
               'Nifty FMCG': 'NIFTY FMCG', 'Nifty Metal': 'NIFTY METAL', 'Nifty Energy': 'NIFTY ENERGY', 'Nifty Realty': 'NIFTY REALTY', 'Nifty Media': 'NIFTY MEDIA',
               'Nifty Infrastructure': 'NIFTY INFRA', 'Nifty India Consumption': 'NIFTY CONSUMPTION', 'Nifty Commodities': 'NIFTY COMMODITIES',
               'Nifty Services Sector': 'NIFTY SERV SECTOR', 'Nifty 500': 'NIFTY 500'}
INDUSTRY_MAP = {'Financial Services': 'Nifty Financial Services', 'Information Technology': 'Nifty IT', 'Healthcare': 'Nifty Pharma',
                'Automobile and Auto Components': 'Nifty Auto', 'Fast Moving Consumer Goods': 'Nifty FMCG', 'Metals & Mining': 'Nifty Metal',
                'Oil Gas & Consumable Fuels': 'Nifty Energy', 'Power': 'Nifty Energy', 'Realty': 'Nifty Realty', 'Media Entertainment & Publication': 'Nifty Media',
                'Capital Goods': 'Nifty Infrastructure', 'Construction': 'Nifty Infrastructure', 'Construction Materials': 'Nifty Infrastructure',
                'Telecommunication': 'Nifty Infrastructure', 'Consumer Durables': 'Nifty India Consumption', 'Consumer Services': 'Nifty India Consumption',
                'Chemicals': 'Nifty Commodities', 'Textiles': 'Nifty India Consumption', 'Services': 'Nifty Services Sector', 'Diversified': 'Nifty 500'}

_lock = threading.Lock()
_model = None
_status: dict = {'started': None, 'last_error': None, 'steps': {}}


def now_ist() -> dt.datetime:
    return dt.datetime.now(IST).replace(tzinfo=None)


# ------------------------------------------------------------------------------------------ storage
def _path(name: str) -> Path:
    ROOT.mkdir(parents=True, exist_ok=True)
    return ROOT / name


def _read_json(name: str, default):
    p = _path(name)
    try:
        return json.loads(p.read_text()) if p.exists() else default
    except Exception:  # noqa: BLE001
        log.exception('v130: unreadable %s', name); return default


def _write_json(name: str, obj) -> None:
    p = _path(name); tmp = p.with_suffix('.tmp')
    tmp.write_text(json.dumps(obj, indent=1, default=str)); tmp.replace(p)


def _append(name: str, obj) -> None:
    with _path(name).open('a', encoding='utf-8') as fh:
        fh.write(json.dumps(obj, default=str) + '\n')


def load_settings() -> dict:
    return rules.resolved(_read_json('settings.json', {}))


def save_settings(new: dict) -> dict:
    cur = _read_json('settings.json', {})
    allowed = set(rules.DEFAULT_SETTINGS)
    for k, v in (new or {}).items():
        if k in allowed: cur[k] = v
    _write_json('settings.json', cur); _append('ledger.jsonl', {'kind': 'SETTINGS_CHANGED', 'ts': now_ist(), 'changes': {k: v for k, v in (new or {}).items() if k in allowed}})
    return rules.resolved(cur)


def load_state() -> dict:
    return _read_json('state.json', {'positions': [], 'last_plan': None, 'last_plan_date': None})


def save_state(st: dict) -> None:
    _write_json('state.json', st)


def model() -> TextGBM:
    global _model
    if _model is None: _model = TextGBM(ASSETS / 'which_model.txt')
    return _model


def gate_ref() -> dict:
    return json.loads((ASSETS / 'gate_ref.json').read_text())


# ------------------------------------------------------------------------------------------ NSE fetch
class NSE:
    def __init__(self):
        self.s = requests.Session(); self.s.headers.update(UA); self._warm = False

    def warm(self):
        if not self._warm:
            try: self.s.get('https://www.nseindia.com/', timeout=20)
            except Exception: pass  # noqa: BLE001
            self._warm = True

    def get(self, url: str, *, json_ok=False, tries=3):
        self.warm()
        for a in range(tries):
            try:
                r = self.s.get(url, timeout=40)
                if r.status_code == 200 and r.content:
                    return r.json() if json_ok else r.content
                if r.status_code in (401, 403): self._warm = False; self.warm()
                if r.status_code == 404: return None
            except Exception:  # noqa: BLE001
                pass
            time.sleep(2 * (a + 1))
        return None

    def fo_day(self, d: dt.date) -> pd.DataFrame | None:
        urls = [f"https://nsearchives.nseindia.com/content/fo/BhavCopy_NSE_FO_0_0_0_{d:%Y%m%d}_F_0000.csv.zip"]
        for u in urls:
            raw = self.get(u)
            if raw and raw[:2] == b'PK':
                return parse_fo_bhav(raw)
        return None

    def delivery_day(self, d: dt.date) -> dict | None:
        raw = self.get(f"https://nsearchives.nseindia.com/products/content/sec_bhavdata_full_{d:%d%m%Y}.csv")
        if not raw: return None
        df = pd.read_csv(io.BytesIO(raw)); df.columns = [c.strip().upper() for c in df.columns]
        df = df[df.SERIES.astype(str).str.strip() == 'EQ']
        return {str(s).strip(): pd.to_numeric(v, errors='coerce') for s, v in zip(df.SYMBOL, df.DELIV_PER.astype(str).str.strip())}

    def ban_day(self, d: dt.date) -> set | None:
        for u in (f"https://nsearchives.nseindia.com/archives/fo/sec_ban/fo_secban_{d:%d%m%Y}.csv", f"https://archives.nseindia.com/archives/fo/sec_ban/fo_secban_{d:%d%m%Y}.csv"):
            raw = self.get(u)
            if raw:
                from .nse_mwpl import parse_secban_csv
                return parse_secban_csv(raw)
        return None

    def board_meetings(self, a: dt.date, b: dt.date) -> list:
        j = self.get(f"https://www.nseindia.com/api/corporate-board-meetings?index=equities&from_date={a:%d-%m-%Y}&to_date={b:%d-%m-%Y}", json_ok=True)
        return j if isinstance(j, list) else (j or {}).get('data', []) if isinstance(j, dict) else []

    def insider(self, a: dt.date, b: dt.date) -> list:
        j = self.get(f"https://www.nseindia.com/api/corporates-pit?index=equities&from_date={a:%d-%m-%Y}&to_date={b:%d-%m-%Y}", json_ok=True)
        return (j or {}).get('data', []) if isinstance(j, dict) else (j or [])

    def announcements(self, sym: str, a: dt.date, b: dt.date) -> list:
        j = self.get(f"https://www.nseindia.com/api/corporate-announcements?index=equities&symbol={requests.utils.quote(sym)}&from_date={a:%d-%m-%Y}&to_date={b:%d-%m-%Y}", json_ok=True)
        return j if isinstance(j, list) else (j or {}).get('data', []) if isinstance(j, dict) else []

    def holidays(self) -> set:
        from .friday_weekend_alert import NSE_HOLIDAY_API, parse_nse_fo_holidays
        return parse_nse_fo_holidays(self.get(NSE_HOLIDAY_API, json_ok=True) or {})

    def sector_map(self) -> dict:
        raw = self.get('https://nsearchives.nseindia.com/content/indices/ind_nifty500list.csv')
        if not raw: return {}
        df = pd.read_csv(io.BytesIO(raw))
        return {s: INDUSTRY_MAP.get(i) for s, i in zip(df.Symbol, df.Industry) if INDUSTRY_MAP.get(i)}


def parse_fo_bhav(raw: bytes) -> pd.DataFrame:
    """Same aggregation as the research nse_fo_oi_fetch.parse (UDiFF format)."""
    z = zipfile.ZipFile(io.BytesIO(raw)); f = pd.read_csv(z.open(z.namelist()[0])); f.columns = [c.strip() for c in f.columns]
    f = f.rename(columns={'FinInstrmTp': 'inst', 'TckrSymb': 'sym', 'XpryDt': 'exp', 'OptnTp': 'otype', 'ClsPric': 'close', 'SttlmPric': 'settle',
                          'TtlTradgVol': 'contracts', 'OpnIntrst': 'oi', 'ChngInOpnIntrst': 'doi'})
    f['inst'] = f.inst.astype(str).str.strip().map({'STF': 'FS', 'IDF': 'FI', 'STO': 'OS', 'IDO': 'OI'}); f['exp'] = pd.to_datetime(f.exp, errors='coerce')
    f['sym'] = f.sym.astype(str).str.strip(); f['otype'] = f.otype.astype(str).str.strip().str.upper()
    for c in ['close', 'settle', 'contracts', 'oi', 'doi']: f[c] = pd.to_numeric(f[c], errors='coerce')
    fut = f[f.inst.isin(['FS', 'FI'])].sort_values('exp'); g = fut.groupby('sym')
    out = pd.DataFrame({'fut_close': g.close.first(), 'near_exp': g.exp.first(), 'fut_oi': g.oi.sum(), 'fut_contracts': g.contracts.sum()})
    opt = f[f.inst.isin(['OS', 'OI'])]
    for side, lab in (('CE', 'ce'), ('PE', 'pe')):
        o = opt[opt.otype == side].groupby('sym'); out[f'{lab}_oi'] = o.oi.sum(); out[f'{lab}_contracts'] = o.contracts.sum()
    out['is_stock_fut'] = fut[fut.inst == 'FS'].groupby('sym').size().reindex(out.index).fillna(0) > 0
    return out.reset_index()


# ------------------------------------------------------------------------------------------ Kite data
def _kite_daily(kite, token: int, start: dt.date, end: dt.date) -> pd.DataFrame:
    rows = kite.historical_data(token, start, end, 'day') or []
    if not rows: return pd.DataFrame()
    df = pd.DataFrame(rows); df['date'] = pd.to_datetime(df['date']).dt.tz_localize(None).dt.normalize()
    return df.set_index('date')[['open', 'high', 'low', 'close', 'volume']]


def build_inputs(kite, nse: NSE, when: dt.date, *, lookback_cal_days: int = 420, progress=None) -> dict:
    """Everything v130_core needs for the evening of ``when`` (a trading day whose session has closed)."""
    inst = kite.instruments('NSE'); tok = {r['tradingsymbol']: r['instrument_token'] for r in inst}
    nfo = kite.instruments('NFO')
    futs = [r for r in nfo if r.get('instrument_type') == 'FUT' and r.get('segment') == 'NFO-FUT']
    from .scanner import _NON_STOCK_FNO_NAMES
    fno = sorted({r['name'] for r in futs if r.get('name') and r['name'].upper() not in _NON_STOCK_FNO_NAMES and r['name'] in tok})
    start = when - dt.timedelta(days=lookback_cal_days)
    px = {}
    for i, s in enumerate(fno):
        try: px[s] = _kite_daily(kite, tok[s], start, when)
        except Exception as exc:  # noqa: BLE001
            log.warning('v130 daily %s: %s', s, exc)
        time.sleep(0.34)
        if progress and i % 25 == 0: progress(f'daily candles {i}/{len(fno)}')
    idx = {}
    for name in ['NIFTY 50', 'INDIA VIX'] + list(SECTOR_KITE.values()):
        if name in tok:
            try: idx[name] = _kite_daily(kite, tok[name], start - dt.timedelta(days=400), when)
            except Exception as exc: log.warning('v130 index %s: %s', name, exc)  # noqa: BLE001
            time.sleep(0.34)
    nifty = idx['NIFTY 50']; days = nifty.index[nifty.index <= pd.Timestamp(when)]
    days = days[days >= pd.Timestamp(start)]
    wide = lambda col: pd.DataFrame({s: d[col] for s, d in px.items() if len(d)}).reindex(days)
    O, H, L, C, VOL = (wide(c) for c in ('open', 'high', 'low', 'close', 'volume')); VAL = VOL * C
    # NSE: last 30 trading days of F&O and delivery files, ban files for 20 days + tomorrow
    recent = list(days[-30:])
    fo_rows, dp = [], {}
    for d in recent:
        f = nse.fo_day(d.date())
        if f is not None: f['date'] = d; fo_rows.append(f)
        dd = nse.delivery_day(d.date())
        if dd: dp[d] = dd
    FOD = pd.concat(fo_rows) if fo_rows else pd.DataFrame(columns=['date', 'sym'])
    FO = {c: FOD.pivot_table(index='date', columns='sym', values=c).reindex(days) for c in ('fut_oi', 'ce_oi', 'pe_oi', 'fut_contracts', 'ce_contracts', 'pe_contracts', 'fut_close')}
    UNIV = pd.DataFrame(False, index=days, columns=C.columns)
    for d, g in FOD[FOD.get('is_stock_fut', True) == True].groupby('date'):  # noqa: E712
        UNIV.loc[d, [s for s in g.sym if s in UNIV.columns]] = True
    DP = pd.DataFrame(dp).T.reindex(index=days, columns=C.columns) if dp else pd.DataFrame(index=days, columns=C.columns, dtype=float)
    hol = nse.holidays() or set()
    nxt = next_trading_day(when, hol)
    BAN = pd.DataFrame(0.0, index=days, columns=C.columns)
    for d in days[-20:]:
        b = nse.ban_day(d.date()) or set()
        BAN.loc[d, [s for s in b if s in BAN.columns]] = 1
    ban_next = nse.ban_day(nxt) or set()
    bm = nse.board_meetings(when - dt.timedelta(days=120), when + dt.timedelta(days=45))
    bm = pd.DataFrame(bm)
    RES = pd.DataFrame(0.0, index=days, columns=C.columns); res_info = {}
    if len(bm):
        bm = bm[bm.get('bm_purpose', pd.Series('', index=bm.index)).astype(str).str.contains('Result', case=False)]
        bm['d'] = pd.to_datetime(bm.bm_date, format='%d-%b-%Y', errors='coerce'); bm['ts'] = pd.to_datetime(bm.get('bm_timestamp'), format='%d-%b-%Y %H:%M:%S', errors='coerce')
        for r in bm.dropna(subset=['d']).itertuples():
            if r.d in RES.index and r.bm_symbol in RES.columns: RES.loc[r.d, r.bm_symbol] = 1
        hold_end = trading_day_after(when, 15, hol)
        for s, g in bm.groupby('bm_symbol'):
            past = g[g.d <= pd.Timestamp(when)].d; fut_ = g[(g.d > pd.Timestamp(when)) & (g.d <= pd.Timestamp(hold_end)) & (g.ts.fillna(pd.Timestamp(when)) <= pd.Timestamp(when) + pd.Timedelta(hours=18))]
            res_info[s] = {'ago_days': int((pd.Timestamp(when) - past.max()).days) if len(past) else None, 'in_hold': bool(len(fut_))}
    ins = pd.DataFrame(nse.insider(when - dt.timedelta(days=20), when))
    INS_S = INS_B = pd.DataFrame(index=days)
    if len(ins) and {'symbol', 'date', 'personCategory', 'tdpTransactionType', 'secVal', 'acqMode'} <= set(ins.columns):
        ins['d'] = pd.to_datetime(ins.date, format='%d-%b-%Y %H:%M', errors='coerce').dt.normalize()
        ins = ins[ins.personCategory.astype(str).str.contains('Promoter', case=False) & ins.acqMode.astype(str).str.contains('Market', case=False)]
        ins['v'] = pd.to_numeric(ins.secVal, errors='coerce')
        mat = lambda k: ins[ins.tdpTransactionType.astype(str).str.lower() == k].groupby(['d', 'symbol']).v.sum().unstack() if len(ins) else pd.DataFrame(index=days)
        INS_S, INS_B = mat('sell'), mat('buy')
    sec_of = nse.sector_map()
    SP = pd.DataFrame({k: idx[v].close for k, v in SECTOR_KITE.items() if v in idx and len(idx[v])})
    M = nifty.close.astype(float); VIX = idx['INDIA VIX'].close.astype(float) if 'INDIA VIX' in idx else pd.Series(dtype=float)
    near_exp = min([r['expiry'] for r in futs if r.get('expiry') and r['expiry'] >= when], default=None)
    tdte = trading_days_between(when, near_exp, hol) if near_exp else np.nan
    TDTE = pd.Series(np.nan, index=days); TDTE.iloc[-1] = tdte
    return dict(O=O, H=H, L=L, C=C, VAL=VAL, DP=DP, FO=FO, M=M, VIX=VIX, BAN=BAN, RES=RES, INS_S=INS_S, INS_B=INS_B, UNIV=UNIV, SP=SP, SEC_OF=sec_of,
                TDTE=TDTE, ban_next=ban_next, res_info=res_info, holidays=hol, next_day=nxt, near_exp=near_exp, n_symbols=len(px))


def next_trading_day(d: dt.date, hol: set) -> dt.date:
    x = d + dt.timedelta(days=1)
    while x.weekday() >= 5 or x in hol: x += dt.timedelta(days=1)
    return x


def trading_day_after(d: dt.date, n: int, hol: set) -> dt.date:
    x = d
    for _ in range(n): x = next_trading_day(x, hol)
    return x


def trading_days_between(a: dt.date, b: dt.date, hol: set) -> int:
    n, x = 0, a
    while x < b:
        x = next_trading_day(x, hol); n += 1
    return n


# ------------------------------------------------------------------------------------------ evening job
def run_evening(kite, when: dt.date | None = None, *, nse: NSE | None = None, progress=None) -> dict:
    when = when or now_ist().date(); nse = nse or NSE(); s = load_settings()
    st0 = load_state()
    if st0.get('last_plan_date') == str(when) and st0.get('last_plan'):
        return st0['last_plan']        # already planned tonight: a server restart must never add a second plan or duplicate positions
    I = build_inputs(kite, nse, when, progress=progress)
    X = core.stock_features(I['O'], I['H'], I['L'], I['C'], I['VAL'], I['DP'], I['FO'], I['M'], I['BAN'], I['RES'], I['INS_S'], I['INS_B'], I['UNIV'], I['SP'], I['SEC_OF'], rows='last')
    X = X[X.date == pd.Timestamp(when)]
    X['ps'] = model().predict(X[core.ST].values.astype(np.float64))
    mk = core.market_features(I['C'], I['UNIV'], I['M'], I['VIX'], None, I['TDTE']); mrow = mk.iloc[-1]
    ref = gate_ref(); q = str(s['gate_quantile']); ref = {**ref, 'gate_cut': ref.get('gate_cuts', {}).get(q, ref['gate_cut'])}
    gate = core.gate_score(mrow, ref)
    st = load_state(); open_syms = [p['sym'] for p in st['positions'] if p.get('status') in ('PENDING', 'OPEN')]
    top = X.sort_values('ps', ascending=False).head(4).sym.tolist(); news = {}
    for sym in top:
        items = nse.announcements(sym, when - dt.timedelta(days=372), when)
        parsed = []
        for it in items or []:
            ts = pd.to_datetime(it.get('sort_date') or it.get('an_dt'), errors='coerce', dayfirst=False)
            if pd.notna(ts): parsed.append({'ts': ts.to_pydatetime(), 'desc': it.get('desc'), 'text': it.get('attchmntText')})
        news[sym] = rules.news_flags(parsed, dt.datetime.combine(when, dt.time(18, 0)))
    plan = rules.evening_plan(X, mrow, gate, s, open_symbols=open_syms, n_open=len(open_syms), ban_next=I['ban_next'], results=I['res_info'], news=news)
    plan.update(date=str(when), entry_day=str(I['next_day']), generated=now_ist(), n_universe=int(len(X)), n_symbols_loaded=I['n_symbols'],
                market={k: (None if pd.isna(mrow.get(k)) else round(float(mrow.get(k)), 4)) for k in core.GATE_VARS + ['m10']},
                top10=X.sort_values('ps', ascending=False).head(10)[['sym', 'ps', 'r5', 'close']].round(4).to_dict('records'), news=news)
    for p in plan['picks']:
        st['positions'].append({'id': f"{when}:{p['sym']}", 'sym': p['sym'], 'signal_date': str(when), 'entry_day': str(I['next_day']), 'status': 'PENDING',
                                'tag': p['tag'], 'instruments': p['instruments'], 'exits': p['exits'], 'sig20': p['sig20'], 'vix': plan['market'].get('vix')})
    st['last_plan'] = plan; st['last_plan_date'] = str(when); save_state(st); _append('plans.jsonl', plan)
    return plan


# ------------------------------------------------------------------------------------------ intraday jobs
def run_entry(kite, today: dt.date | None = None) -> int:
    today = today or now_ist().date(); st = load_state(); s = load_settings(); n = 0
    pend = [p for p in st['positions'] if p['status'] == 'PENDING' and p['entry_day'] == str(today)]
    if not pend: return 0
    q = kite.quote([f"NSE:{p['sym']}" for p in pend])
    for p in pend:
        o = (q.get(f"NSE:{p['sym']}") or {}).get('ohlc', {}).get('open')
        if o and o > 0:
            p['entry_price'] = float(o); p['status'] = 'OPEN'; p['exits'] = rules.price_levels(float(o), s, basis='actual open'); n += 1
            _append('ledger.jsonl', {'kind': 'ENTRY', 'ts': now_ist(), 'id': p['id'], 'sym': p['sym'], 'entry': o, 'levels': p['exits']})
    save_state(st); return n


def run_options(kite, today: dt.date | None = None) -> int:
    today = today or now_ist().date(); st = load_state(); s = load_settings(); n = 0
    need = [p for p in st['positions'] if p['status'] == 'OPEN' and p['entry_day'] == str(today)
            and any(i['type'] == 'CALL' and i.get('status') == 'CHOOSE_AT_10:00' for i in p['instruments'])]
    if not need: return 0
    nfo = kite.instruments('NFO'); hol = NSE().holidays() or set()
    for p in need:
        ce = [r for r in nfo if r.get('name') == p['sym'] and r.get('instrument_type') == 'CE']
        fut = sorted([r for r in nfo if r.get('name') == p['sym'] and r.get('instrument_type') == 'FUT'], key=lambda r: r['expiry'])
        F = (kite.quote([f"NFO:{fut[0]['tradingsymbol']}"]).get(f"NFO:{fut[0]['tradingsymbol']}") or {}).get('last_price') if fut else None
        if not F: continue
        exps = sorted({r['expiry'] for r in ce if trading_days_between(today, r['expiry'], hol) >= s['call_min_dte']})[:1]
        near = [r for r in ce if r['expiry'] in exps and abs(r['strike'] / F - 1) < 0.12]
        quotes = kite.quote([f"NFO:{r['tradingsymbol']}" for r in near]) if near else {}
        chain = []
        for r in near:
            qq = quotes.get(f"NFO:{r['tradingsymbol']}") or {}; dp = qq.get('depth') or {}
            bid = (dp.get('buy') or [{}])[0].get('price'); ask = (dp.get('sell') or [{}])[0].get('price')
            chain.append({'tradingsymbol': r['tradingsymbol'], 'strike': r['strike'], 'expiry': r['expiry'], 'bid': bid, 'ask': ask, 'oi': qq.get('oi'), 'lot_size': r.get('lot_size')})
        for ins in p['instruments']:
            if ins['type'] != 'CALL' or ins.get('status') != 'CHOOSE_AT_10:00': continue
            budget = s['capital'] * ins['premium_pct']
            out = rules.choose_call(chain, F, p['sig20'], p.get('vix') or 15.0, p['tag'], budget, s, lambda e: trading_days_between(today, e, hol))
            ins.update(out); ins['status'] = out['status']; ins['chosen_at'] = now_ist(); n += 1
            _append('ledger.jsonl', {'kind': 'OPTION_INSTRUCTION', 'ts': now_ist(), 'id': p['id'], 'sym': p['sym'], **{k: v for k, v in out.items() if k != 'evaluated'}})
    save_state(st); return n


def evaluate_exit(entry: float, closes: list, highs: list, s: dict) -> tuple[str | None, float | None, int]:
    """Research exit A on the price path since entry (index 0 = entry day). Returns (reason, exit_price, sessions_held)."""
    for k, (c, h) in enumerate(zip(closes, highs)):
        if h is not None and h >= entry * (1 + s['target']): return 'TARGET', round(entry * (1 + s['target']), 2), k + 1
        if c is None: continue
        if c <= entry * (1 - s['stop_close']): return 'STOP_CLOSE', c, k + 1
        if k >= 1 and c >= max(x for x in closes[max(0, k - 6):k + 1] if x is not None): return 'SEVEN_DAY_HIGH_CLOSE', c, k + 1
        if k + 1 >= s['max_days']: return 'TIME', c, k + 1
    return None, None, len(closes)


def run_outcomes(kite, today: dt.date | None = None) -> int:
    today = today or now_ist().date(); st = load_state(); s = load_settings(); n = 0
    inst = {r['tradingsymbol']: r['instrument_token'] for r in kite.instruments('NSE')}
    for p in st['positions']:
        if p['status'] != 'OPEN' or p['sym'] not in inst: continue
        d = _kite_daily(kite, inst[p['sym']], dt.date.fromisoformat(p['entry_day']), today)
        if not len(d): continue
        reason, px, held = evaluate_exit(p['entry_price'], d.close.tolist(), d.high.tolist(), s)
        p['last_close'] = float(d.close.iloc[-1]); p['sessions_held'] = int(held)
        p['mark_pct'] = round(p['last_close'] / p['entry_price'] - 1, 4)
        for ins in p['instruments']:
            if ins['type'] == 'CALL' and ins.get('status') == 'BUY_CALL' and not ins.get('exit_reason'):
                q = (kite.quote([f"NFO:{ins['tradingsymbol']}"]).get(f"NFO:{ins['tradingsymbol']}") or {})
                last = q.get('last_price'); hi = (q.get('ohlc') or {}).get('high')
                ins['last'] = last; ins.setdefault('sessions', 0)
                if ins.get('last_session') != str(today): ins['sessions'] += 1; ins['last_session'] = str(today)   # once per day, even after a restart
                if hi and hi >= ins['target']: ins.update(exit_reason='TARGET', exit_price=ins['target'])
                elif last and last <= ins['stop_close']: ins.update(exit_reason='STOP_CLOSE', exit_price=last)
                elif ins['sessions'] >= ins['exit_by_session']: ins.update(exit_reason='TIME', exit_price=last)
                if ins.get('exit_reason'):
                    _append('ledger.jsonl', {'kind': 'OPTION_EXIT', 'ts': now_ist(), 'id': p['id'], 'contract': ins['tradingsymbol'], 'reason': ins['exit_reason'],
                                             'entry': ins['limit'], 'exit': ins['exit_price'], 'ret_pct': round(ins['exit_price'] / ins['limit'] - 1, 4) if ins['exit_price'] else None})
        if reason:
            p.update(status='CLOSED', exit_reason=reason, exit_price=px, exit_date=str(today), ret_pct=round(px / p['entry_price'] - 1, 4)); n += 1
            _append('ledger.jsonl', {'kind': 'EXIT', 'ts': now_ist(), 'id': p['id'], 'sym': p['sym'], 'tag': p['tag'], 'reason': reason, 'entry': p['entry_price'],
                                     'exit': px, 'ret_pct': p['ret_pct'], 'held': held, 'instruments': [i['type'] for i in p['instruments']]})
    save_state(st); return n


# ------------------------------------------------------------------------------------------ scheduler
def _step(name: str, fn, *a):
    try:
        out = fn(*a); _status['steps'][name] = {'ok': True, 'at': now_ist(), 'result': out if not isinstance(out, dict) else 'plan saved'}; return True
    except Exception as exc:  # noqa: BLE001
        log.exception('v130 step %s failed', name); _status['steps'][name] = {'ok': False, 'at': now_ist(), 'error': str(exc)[:300]}; _status['last_error'] = str(exc)[:300]
        return False


def _loop(get_kite):
    done: dict = {}
    while True:
        try:
            now = now_ist(); d = now.date(); hm = now.hour * 60 + now.minute
            if d.weekday() < 5:
                kite = get_kite()
                if kite is not None:
                    key = lambda k: f'{d}:{k}'
                    if 560 <= hm <= 585 and not done.get(key('entry')): done[key('entry')] = _step('entry', run_entry, kite)
                    if 600 <= hm <= 675 and not done.get(key('options')) and hm % 15 == 0: done[key('options')] = _step('options', run_options, kite) and hm >= 660
                    if 935 <= hm <= 990 and not done.get(key('outcomes')): done[key('outcomes')] = _step('outcomes', run_outcomes, kite)
                    if 1125 <= hm <= 1350 and not done.get(key('evening')) and (hm - 1125) % 15 == 0:
                        done[key('evening')] = _step('evening', run_evening, kite)
        except Exception:  # noqa: BLE001
            log.exception('v130 scheduler tick failed')
        time.sleep(60)


def start_once(get_kite) -> None:
    with _lock:
        if _status.get('started'): return
        _status['started'] = now_ist()
    threading.Thread(target=_loop, args=(get_kite,), daemon=True, name='v130-swing').start()


def status() -> dict:
    st = load_state()
    closed = [p for p in st['positions'] if p['status'] == 'CLOSED']
    by = lambda typ: [p for p in closed if any(i['type'] == typ for i in p['instruments'])]
    summ = lambda ps: {'n': len(ps), 'win': round(np.mean([p['ret_pct'] > 0 for p in ps]), 3) if ps else None, 'avg_pct': round(float(np.mean([p['ret_pct'] for p in ps])), 4) if ps else None}
    return {'engine': 'V13.0 swing (spotting only - no orders)', 'storage': str(ROOT), 'scheduler': _status, 'settings': load_settings(),
            'last_plan_date': st.get('last_plan_date'), 'plan': st.get('last_plan'),
            'open': [p for p in st['positions'] if p['status'] in ('PENDING', 'OPEN')], 'closed_recent': closed[-30:],
            'scorecard': {'FUTURES': summ(by('FUTURES')), 'CASH': summ(by('CASH')), 'ALL': summ(closed)}}
