"""V13.0 swing engine - decision rules (pure functions, unit-tested).

Everything here follows the research ledger (Rounds 46-70). Defaults come from ``v130_assets/constants.json``
and the admin settings; every number is a setting so the owner can change it without a code change.
"""
from __future__ import annotations

import datetime as dt
import math
import re
from typing import Iterable

import numpy as np
import pandas as pd

DEFAULT_SETTINGS = {
    'mode': 'G',                     # A = gate open 40% of evenings, top-2 | G = gate 50%, top-2 + closed-gate top-1 if BIG | H = G + 3rd pick in expiry week
    'gate_quantile': None,           # None -> from mode (A: 0.6, G/H: 0.5); else 0.4/0.5/0.6/0.7
    'picks_open_gate': 2,
    'closed_gate_big_pick': None,    # None -> from mode
    'expiry_third_pick': None,       # None -> from mode
    'max_open': 10,
    'quiet_r5': 0.0378,
    'res_after_days': 7, 'res_hold_tdays': 15,
    'news_veto': True, 'news_abn': 3.0,
    'low52_band': 1.05,
    'crash_m10': -0.08, 'crash_size': 0.5,
    'target': 0.06, 'stop_close': 0.12, 'max_days': 15,
    'normal_instrument': 'FUTURES',  # FUTURES | CASH
    'capital': 1000000.0,
    'fut_notional_pct': {'EXPIRY': 0.20, 'BIG': 0.0, 'NORMAL': 0.10},
    'call_premium_pct': {'EXPIRY': 0.010, 'BIG': 0.020, 'NORMAL': 0.0},
    'call_target': 0.50, 'call_stop': 0.50, 'call_days': 8, 'call_min_dte': 15,
    'call_max_spread_pct': 2.5, 'call_max_ivrv': 1.3, 'call_min_oi': 50000,
    'big_cut': -0.5677, 'opt_value_cut': 0.2681,
}


def resolved(settings: dict | None) -> dict:
    s = {**DEFAULT_SETTINGS, **(settings or {})}
    mode = str(s.get('mode') or 'G').upper()
    if s.get('gate_quantile') is None: s['gate_quantile'] = 0.6 if mode == 'A' else 0.5
    if s.get('closed_gate_big_pick') is None: s['closed_gate_big_pick'] = mode in ('G', 'H')
    if s.get('expiry_third_pick') is None: s['expiry_third_pick'] = mode == 'H'
    s['mode'] = mode
    return s


# ---------------------------------------------------------------- news (NSE announcements) ----
_CATS = [
    ('bad_reg', r'action\(s\)|litigation|default|insolvency|strike|lockout|suspension|disturbance',
     r'penalt|show cause|sebi order|search and seizure|search operation|raid|warning letter|form 483|import alert|fraud|forensic|enforcement directorate|income tax search|gst demand'),
    ('mgmt', r'resignation|cessation|change in management|change in auditor|demise|change in company secretary', r'resign'),
    ('rating', r'credit rating', None),
    ('rumour', r'news verification|clarification|rumour|price movement|spurt in volume', None),
    ('promoter', r'takeover|sast|insider trading|insider', r'pledge|encumbr'),
    ('results', r'financial result|outcome of board meeting|limited review|integrated filing- financial', None),
    ('business', r'bagging|order|acquisition|agreement|memorandum|monthly business|press release|investor presentation|analyst|con\. call|diversification|business update', None),
    ('capital', r'fund|qualified institutional|preferential|allotment|buy ?back|bonus|split|scheme of arrangement|amalgamation|merger|open offer|issue of securities|raising', None),
]


def categorize(desc: str, text: str) -> str:
    d = (desc or '').lower(); t = (text or '').lower()
    for name, dre, tre in _CATS:
        if re.search(dre, d) or (tre and re.search(tre, t)):
            return name
    return 'routine'


def news_flags(items: Iterable[dict], when: dt.datetime) -> dict:
    """items: dicts with keys desc, text, ts (datetime). Same definitions as research news_feats.py (cut-off 18:00 of the signal day)."""
    cut = when.replace(hour=18, minute=0, second=0, microsecond=0)
    rows = [(i['ts'], categorize(i.get('desc'), i.get('text'))) for i in items if i.get('ts') is not None and i['ts'] <= cut]
    rows = [r for r in rows if r[1] != 'routine']
    n3 = lambda c: sum(1 for ts, k in rows if k == c and ts > cut - dt.timedelta(days=3))
    n7 = sum(1 for ts, _ in rows if ts > cut - dt.timedelta(days=7))
    prev = sum(1 for ts, _ in rows if cut - dt.timedelta(days=372) < ts <= cut - dt.timedelta(days=7))
    abn = n7 / (prev / 52.0 + 0.5)
    return {'n3_promoter': n3('promoter'), 'n3_bad_reg': n3('bad_reg'), 'n7_all': n7, 'news_abn': round(abn, 3)}


def news_veto(flags: dict | None, abn_cut: float) -> str | None:
    if not flags:
        return None
    if flags.get('n3_promoter', 0) > 0: return 'promoter/insider/takeover filing in last 3 days'
    if flags.get('n3_bad_reg', 0) > 0: return 'legal/regulatory trouble in last 3 days'
    if flags.get('news_abn', 0) >= abn_cut: return f"news flow {flags['news_abn']:.1f}x normal"
    return None


# ---------------------------------------------------------------- evening plan ----
def evening_plan(X: pd.DataFrame, mk_row: pd.Series, gate: dict, settings: dict, *, open_symbols: Iterable[str] = (), n_open: int = 0,
                 ban_next: Iterable[str] = (), results: dict | None = None, news: dict | None = None) -> dict:
    """X: last-day features + 'ps' (model score). gate: {'G', 'cut', 'open'} at the chosen quantile.
    results: sym -> {'ago_days': calendar days since last results (or None), 'in_hold': bool}; news: sym -> news_flags()."""
    s = resolved(settings); results = results or {}; news = news or {}
    ban_next = set(ban_next); open_symbols = set(open_symbols)
    X = X.dropna(subset=['ps']).sort_values('ps', ascending=False).reset_index(drop=True); X['rank'] = np.arange(1, len(X) + 1)
    tdte = float(mk_row.get('tdte', np.nan)); expiry_week = math.isfinite(tdte) and tdte <= 3
    gate_open = bool(gate.get('open'))
    k = (int(s['picks_open_gate']) + (1 if (expiry_week and s['expiry_third_pick']) else 0)) if gate_open else (1 if s['closed_gate_big_pick'] else 0)
    crash = float(mk_row.get('m10', 0) or 0) < s['crash_m10']
    size_mult = s['crash_size'] if crash else 1.0
    vix = float(mk_row.get('vix', np.nan))
    picks, rejected = [], []
    slots = max(0, int(s['max_open']) - int(n_open))
    for r in X.head(k).itertuples():
        bigscore = math.log(vix * r.sig20) if (vix > 0 and r.sig20 and r.sig20 > 0) else float('nan')
        is_big = math.isfinite(bigscore) and bigscore >= s['big_cut']
        tag = 'EXPIRY' if expiry_week else ('BIG' if is_big else 'NORMAL')
        reasons = []
        if not gate_open and not is_big: reasons.append('gate closed and not a BIG pick')
        if r.sym in ban_next: reasons.append('in F&O ban tomorrow')
        if abs(r.r5) < s['quiet_r5']: reasons.append(f"quiet stock (5-day move {r.r5:+.1%})")
        rr = results.get(r.sym) or {}
        if rr.get('in_hold'): reasons.append('results inside the holding period')
        if rr.get('ago_days') is not None and rr['ago_days'] <= s['res_after_days']: reasons.append(f"results {rr['ago_days']} days ago")
        if s['news_veto']:
            nv = news_veto(news.get(r.sym), s['news_abn'])
            if nv: reasons.append('bad news: ' + nv)
        if getattr(r, 'low52_band', np.nan) <= s['low52_band'] and bool(mk_row.get('nifty_up200', False)): reasons.append('near 52-week low while Nifty above 200-DMA')
        if r.sym in open_symbols: reasons.append('already holding this stock')
        if not reasons and len(picks) >= slots: reasons.append('max open positions reached')
        item = dict(sym=r.sym, rank=int(r.rank), score=round(float(r.ps), 2), tag=tag, is_big=bool(is_big), bigscore=round(bigscore, 4) if math.isfinite(bigscore) else None,
                    r5=round(float(r.r5), 4), sig20=round(float(r.sig20), 5), close=round(float(getattr(r, 'close', float('nan'))), 2) if math.isfinite(float(getattr(r, 'close', float('nan')))) else None)
        if reasons:
            rejected.append({**item, 'reasons': reasons}); continue
        fut_pct = s['fut_notional_pct'].get(tag, 0.0) * size_mult; call_pct = s['call_premium_pct'].get(tag, 0.0) * size_mult
        instr = []
        if tag == 'NORMAL' and s['normal_instrument'] == 'CASH':
            instr.append({'type': 'CASH', 'notional_pct': fut_pct})
        elif fut_pct > 0:
            instr.append({'type': 'FUTURES', 'notional_pct': fut_pct})
        if call_pct > 0:
            instr.append({'type': 'CALL', 'premium_pct': call_pct, 'status': 'CHOOSE_AT_10:00', 'required': tag == 'BIG'})
        if tag == 'EXPIRY' and is_big and s['call_premium_pct'].get('BIG', 0) > call_pct:
            instr = [i for i in instr if i['type'] != 'CALL'] + [{'type': 'CALL', 'premium_pct': s['call_premium_pct']['BIG'] * size_mult, 'status': 'CHOOSE_AT_10:00', 'required': True}]
        item.update(instruments=instr, exits=price_levels(item['close'], s))
        picks.append(item)
    return {'gate': gate, 'expiry_week': expiry_week, 'tdte': tdte, 'crash_brake': crash, 'size_mult': size_mult, 'k': k,
            'picks': picks, 'rejected': rejected, 'mode': s['mode'], 'settings_used': {k_: s[k_] for k_ in ('mode', 'gate_quantile', 'picks_open_gate', 'closed_gate_big_pick', 'expiry_third_pick')}}


def price_levels(ref_price: float | None, settings: dict, basis: str = 'evening close (provisional - recomputed from the actual open)') -> dict:
    """Concrete futures/cash levels. Research exits: +6% target (limit order), stop when a CLOSE is 12% below entry,
    early exit at the first close that is the highest close of the last 7 sessions, time exit after 15 sessions."""
    s = resolved(settings)
    out = {'reference': ref_price, 'reference_basis': basis, 'target_pct': s['target'], 'stop_close_pct': -s['stop_close'],
           'early_exit': 'first close at a 7-day closing high', 'max_days': s['max_days']}
    if ref_price and ref_price > 0:
        out.update(target_price=round(ref_price * (1 + s['target']), 2), stop_close_price=round(ref_price * (1 - s['stop_close']), 2))
    return out


# ---------------------------------------------------------------- option instruction ----
def _ncdf(x: float) -> float:
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


def bs_call(F: float, K: float, T: float, v: float) -> float:
    if T <= 0 or v <= 0: return max(F - K, 0.0)
    s = v * math.sqrt(T); d1 = (math.log(F / K) + 0.5 * s * s) / s
    return F * _ncdf(d1) - K * _ncdf(d1 - s)


def implied_vol(price: float, F: float, K: float, T: float) -> float | None:
    if not (price > 0 and F > 0 and K > 0 and T > 0) or price <= max(F - K, 0) + 1e-9: return None
    lo, hi = 0.01, 4.0
    if bs_call(F, K, T, hi) < price: return None
    for _ in range(80):
        mid = (lo + hi) / 2
        if bs_call(F, K, T, mid) > price: hi = mid
        else: lo = mid
    return (lo + hi) / 2


def choose_call(chain: list[dict], F: float, sig20: float, vix: float, tag: str, budget_rupees: float, settings: dict, trading_days_to) -> dict:
    """chain: [{'tradingsymbol','strike','expiry'(date),'bid','ask','oi','lot_size'}] for CE contracts.
    trading_days_to(expiry_date) -> trading sessions from today to expiry. Returns the instruction or a reason why none."""
    s = resolved(settings)
    ok_exp = sorted({c['expiry'] for c in chain if trading_days_to(c['expiry']) >= s['call_min_dte']})
    if not ok_exp: return {'status': 'NO_CALL', 'reason': f"no expiry with >= {s['call_min_dte']} trading days"}
    exp = ok_exp[0]; T = trading_days_to(exp) / 252.0
    strikes = sorted({c['strike'] for c in chain if c['expiry'] == exp})
    atm_i = int(np.argmin([abs(k - F) for k in strikes]))
    cand = [(lab, strikes[i]) for lab, i in (('ATM', atm_i), ('OTM1', atm_i + 1), ('OTM2', atm_i + 2)) if 0 <= i < len(strikes)]
    rv = sig20 * math.sqrt(252); evals = []
    for lab, K in cand:
        q = next((c for c in chain if c['expiry'] == exp and c['strike'] == K), None)
        if not q or not (q.get('bid') and q.get('ask') and q['ask'] >= q['bid'] > 0):
            evals.append({'label': lab, 'strike': K, 'ok': False, 'why': 'no two-sided quote'}); continue
        mid = (q['bid'] + q['ask']) / 2; spr = (q['ask'] - q['bid']) / mid * 100; iv = implied_vol(mid, F, K, T)
        value = (math.log(vix * sig20) - math.log(iv)) if iv else float('nan')
        why = []
        if spr > s['call_max_spread_pct']: why.append(f"spread {spr:.1f}%")
        if (q.get('oi') or 0) < s['call_min_oi']: why.append('low OI')
        if iv is None: why.append('IV not computable')
        elif iv / rv > s['call_max_ivrv']: why.append(f"IV/RV {iv / rv:.2f}")
        evals.append({'label': lab, 'strike': K, 'tradingsymbol': q.get('tradingsymbol'), 'mid': round(mid, 2), 'spread_pct': round(spr, 2), 'iv': round(iv, 4) if iv else None,
                      'value_score': round(value, 4) if math.isfinite(value) else None, 'oi': q.get('oi'), 'lot_size': q.get('lot_size'), 'ok': not why, 'why': ', '.join(why)})
    good = [e for e in evals if e['ok']]
    if not good: return {'status': 'NO_CALL', 'reason': 'no liquid strike', 'evaluated': evals, 'fallback': 'use FUTURES'}
    best = max(good, key=lambda e: e['value_score'])
    otm1 = next((e for e in good if e['label'] == 'OTM1'), None)
    if otm1 is not None and otm1['value_score'] >= best['value_score'] - 0.02:  # research: OTM1 slightly better on real prices
        best = otm1
    if tag != 'BIG' and (best['value_score'] is None or best['value_score'] < s['opt_value_cut']):
        return {'status': 'NO_CALL', 'reason': f"value score {best['value_score']} below {s['opt_value_cut']:.3f} (only BIG picks buy calls regardless)", 'evaluated': evals}
    tick = 0.05; lim = round(math.floor(best['mid'] / tick + 1e-9) * tick, 2)
    q = next(c for c in chain if c['expiry'] == exp and c['strike'] == best['strike'])
    max_px = round(best['mid'] + 0.25 * (q['ask'] - q['bid']), 2)
    lot = int(best.get('lot_size') or 1); cost1 = lim * lot; lots = int(budget_rupees // cost1) if cost1 > 0 else 0
    return {'status': 'BUY_CALL', 'tradingsymbol': best['tradingsymbol'], 'strike': best['strike'], 'expiry': str(exp), 'label': best['label'],
            'limit': round(lim, 2), 'max_price': max_px, 'target': round(lim * (1 + s['call_target']), 2), 'stop_close': round(lim * (1 - s['call_stop']), 2),
            'exit_by_session': s['call_days'], 'lots': max(lots, 0), 'lot_size': lot, 'premium_per_lot': round(cost1, 2),
            'budget_note': None if lots >= 1 else f"1 lot costs Rs {cost1:,.0f} > budget Rs {budget_rupees:,.0f}",
            'iv': best['iv'], 'value_score': best['value_score'], 'spread_pct': best['spread_pct'], 'evaluated': evals}
