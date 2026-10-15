"""V13.5 Market > Industry > Stock hierarchy desk (V4.7 model; RESEARCH / NOT VALIDATED; paper only, spotting only; never places orders).

Rule (exactly as in the V4.7 review, from the order-flow polls the direction desk already records; no extra Kite calls), checked at 09:45:
  r15      = stock move 09:30 -> 09:45 (%);   eff = |net move| / path travelled over 09:30/09:35/09:40/09:45;   volrel = 09:30-09:45 volume / its median of the prior 20 days
  market   = median r15 of all F&O stocks (NOT the NIFTY index);   industry = median r15 of the stock's industry (needs >= 4 stocks)
  side     = UP if r15 >= 0 else DOWN
  eligible = side*industry > 0 and side*market >= 0 and side*(r15 - industry) > 0 and industry group >= 4; anything missing = NOT ELIGIBLE (never zero-filled)
  score    = side*(r15 - industry) * clip(eff,0,1) * clip(volrel,0,4); the highest score is the pick, the next ones are OBSERVE only.
Our own replication on 2019-26 (1,900 days, point-in-time F&O membership): the exact rule wins 46.9% over +30 min (UP 42.9%, DOWN 50.7%), the review's 57.8% was a 64-day result.
The only variant that held in a walk-forward split (rules fixed on 2019-23, tested 2024-26) is DOWN-only held to 15:15: about 55% / +24 bps gross in-sample, 52% / +16 bps out-of-sample,
borderline against a 12 bps cost. It records +3/+5/+15/+30/+60 min and 15:15 results with MAE/MFE for every candidate.
V13.6: both sides are always evaluated. side_mode 'auto' (default) lets the hierarchy lab (v136_hierarchy_lab, walk-forward UP vs DOWN replay)
decide which side is ACTIVE and with which gates/exit; a side that is not active still records its best stock as SHADOW (never a pick),
so UP builds a live record instead of being discarded. Before the lab has run, auto = DOWN active, UP shadow (the earlier research).
"""
from __future__ import annotations

import datetime as dt
import json
import logging
import math
import threading
from pathlib import Path

import numpy as np

from . import v133_direction as d
from .v134_relstrength import _poll_at, _px

log = logging.getLogger(__name__)
FILE = 'v47_days.jsonl'
VOL_FILE = 'v47_volhist.json'
SIDE_MODES = ('auto', 'both', 'down', 'up')
DEFAULT_SETTINGS = {'enabled': True, 'side_mode': 'auto', 'down_only': False, 'min_group': 4, 'observe_n': 3, 'min_stocks': 100, 'min_vol_days': 10,
                    'exit_at': '15:15', 'cost_bps': 12.0, 'min_eff': 0.0, 'min_volrel': 0.0}
HORIZONS = (3, 5, 15, 30, 60)
BOOK_PCT = 1.0
_lock = threading.Lock()
_ind_cache: dict = {}


def load_settings() -> dict:
    s = dict(DEFAULT_SETTINGS)
    try:
        s.update({k: v for k, v in (d._read_json('v47_settings.json', {}) or {}).items() if k in s})
        s['enabled'] = bool(s['enabled']); s['down_only'] = bool(s['down_only'])
        if s['side_mode'] not in SIDE_MODES: s['side_mode'] = 'auto'
        s['min_group'] = int(min(15, max(2, int(s['min_group'])))); s['observe_n'] = int(min(10, max(0, int(s['observe_n']))))
        s['min_stocks'] = int(max(20, int(s['min_stocks']))); s['min_vol_days'] = int(min(20, max(3, int(s['min_vol_days']))))
        s['cost_bps'] = float(min(100.0, max(0.0, float(s['cost_bps'])))); s['min_eff'] = float(min(1.0, max(0.0, float(s['min_eff'])))); s['min_volrel'] = float(min(4.0, max(0.0, float(s['min_volrel']))))
    except Exception:  # noqa: BLE001
        log.exception('v135: bad settings; using defaults'); return dict(DEFAULT_SETTINGS)
    return s


def side_plan(s: dict, lab: dict | None = None) -> dict:
    """side -> {'active', 'min_eff', 'min_volrel', 'min_mkt', 'exit', 'source'}: which sides may pick today and with which gates."""
    base = {'min_eff': s['min_eff'], 'min_volrel': s['min_volrel'], 'min_mkt': 0.0, 'exit': 'exit'}
    mode = s['side_mode']; plan = {}
    for side in ('UP', 'DOWN'):
        p = dict(base, active=mode == 'both' or mode == side.lower(), source='settings')
        if mode == 'auto':
            v = (lab or {}).get(side)
            if v and v.get('status'):
                p['active'] = v['status'] == 'ACTIVE'; p['source'] = f"lab {v['status'].lower()}"
                if p['active'] and v.get('cfg'): p.update({k: v['cfg'][k] for k in ('min_eff', 'min_volrel', 'min_mkt', 'exit') if k in v['cfg']})
            else:
                p['active'] = side == 'DOWN'; p['source'] = 'no lab yet (earlier research: DOWN)'
        plan[side] = p
    return plan


def _lab_verdicts() -> dict:
    try:
        from . import v136_hierarchy_lab; return v136_hierarchy_lab.verdicts()
    except Exception:  # noqa: BLE001
        log.exception('v135: lab verdicts unreadable'); return {}


def industry_map() -> dict:
    """symbol -> industry from the shipped sector catalogue (present-day taxonomy, a stated limitation of the review)."""
    if _ind_cache: return _ind_cache
    try:
        p = Path(__file__).resolve().parent / 'data' / 'sector_catalogue.json'
        for sct in json.loads(p.read_text()):
            for m in sct.get('members') or []:
                if m.get('symbol') and m.get('industry'): _ind_cache.setdefault(m['symbol'], m['industry'])
    except Exception:  # noqa: BLE001
        log.exception('v135: sector catalogue unreadable')
    return _ind_cache


def _v15_from_polls(polls: list) -> dict:
    """symbol -> volume traded 09:30-09:45 (cumulative day volume difference)."""
    a, b = _poll_at(polls, '09:30', 3), _poll_at(polls, '09:45', 3)
    out = {}
    if not a or not b: return out
    for sym, row in (b.get('q') or {}).items():
        try:
            v1, v0 = float(row[1]), float((a.get('q') or {}).get(sym)[1])
            if math.isfinite(v1) and math.isfinite(v0) and v1 - v0 > 0: out[sym] = v1 - v0
        except (TypeError, ValueError, IndexError):
            continue
    return out


def _vol_hist() -> dict:
    return d._read_json(VOL_FILE, {}) or {}


def bootstrap_vol_hist(today: str, max_back: int = 45) -> dict:
    """Build the 20-day volume baseline from earlier days' recorded polls (once, when the file is missing)."""
    hist: dict = {}
    day = dt.date.fromisoformat(today)
    got = []
    for i in range(1, max_back + 1):
        dd = day - dt.timedelta(days=i)
        if dd.weekday() >= 5: continue
        try: polls = d._read_of(str(dd))
        except Exception: polls = []  # noqa: BLE001
        if not polls: continue
        v = _v15_from_polls(polls)
        if len(v) >= 50: got.append((str(dd), v))
        if len(got) >= 20: break
    for dd, v in sorted(got):
        for sym, x in v.items(): hist.setdefault(sym, []).append([dd, x])
    return hist


def compute(polls: list, hist: dict, ind: dict, s: dict, plan: dict | None = None) -> dict | None:
    """Pure: the 09:45 hierarchy evaluation for both sides. Returns candidates (ranked per side), the rejected-candidate audit and the day's context.
    plan: side_plan(); per side the gates, the exit rule and whether its top stock is a PICK (active) or a SHADOW record."""
    plan = plan or side_plan(s)
    p0, p5, p10, p15 = (_poll_at(polls, h, 3) for h in ('09:30', '09:35', '09:40', '09:45'))
    if not (p0 and p15): return None
    v15 = _v15_from_polls(polls)
    rows = {}
    for sym, row in (p15.get('q') or {}).items():
        a = _px((p0.get('q') or {}).get(sym), 0); b = _px(row, 0)
        if a and b: rows[sym] = (a, b, (b / a - 1) * 100)
    if len(rows) < s['min_stocks']:
        return {'at': '09:45', 'poll': p15['t'], 'n': len(rows), 'picks': [], 'rejected': {}, 'note': 'too few quotes'}
    mkt = float(np.median([r[2] for r in rows.values()]))
    groups: dict = {}
    for sym, r in rows.items():
        g = ind.get(sym)
        if g: groups.setdefault(g, []).append(r[2])
    gmed = {g: (float(np.median(v)), len(v)) for g, v in groups.items()}
    cands, rej, audit = [], {}, []
    base_have = 0

    def reject(sym, side, code, extra=None):
        rej[code] = rej.get(code, 0) + 1
        if len(audit) < 400: audit.append({'symbol': sym, 'side': side, 'why': code, **(extra or {})})

    for sym, (a, b, r15) in rows.items():
        side = 1 if r15 >= 0 else -1; nm = 'UP' if side > 0 else 'DOWN'; sp = plan[nm]
        g = ind.get(sym)
        if not g: reject(sym, nm, 'NO_INDUSTRY'); continue
        im, cnt = gmed[g]
        if cnt < s['min_group']: reject(sym, nm, 'GROUP_TOO_SMALL'); continue
        qs = [_px((p.get('q') or {}).get(sym), 0) if p else None for p in (p0, p5, p10, p15)]
        if any(x is None for x in qs): reject(sym, nm, 'MISSING_BAR'); continue
        path = sum(abs(qs[i + 1] - qs[i]) for i in range(3))
        if not path > 0: reject(sym, nm, 'NO_PATH'); continue
        eff = min(1.0, abs(qs[3] - qs[0]) / path)
        base = [x[1] for x in (hist.get(sym) or [])][-20:]; base_have = max(base_have, len(base))
        vr = None
        if len(base) >= s['min_vol_days'] and sym in v15 and np.median(base) > 0: vr = float(v15[sym] / np.median(base))
        if vr is None: reject(sym, nm, 'NO_VOLUME_BASELINE'); continue
        if not side * im > 0: reject(sym, nm, 'INDUSTRY_NOT_ALIGNED', {'r15': round(r15, 2), 'industry': round(im, 2)}); continue
        if not side * mkt >= 0: reject(sym, nm, 'MARKET_AGAINST', {'r15': round(r15, 2), 'market': round(mkt, 2)}); continue
        resid = side * (r15 - im)
        if not resid > 0: reject(sym, nm, 'NO_RESIDUAL_LEADERSHIP', {'r15': round(r15, 2), 'industry': round(im, 2)}); continue
        if side * mkt < sp['min_mkt']: reject(sym, nm, 'MARKET_TOO_WEAK', {'market': round(mkt, 2)}); continue
        if eff < sp['min_eff']: reject(sym, nm, 'LOW_EFFICIENCY', {'eff': round(eff, 2)}); continue
        if vr < sp['min_volrel']: reject(sym, nm, 'LOW_VOLUME', {'volrel': round(vr, 2)}); continue
        score = resid * eff * min(4.0, max(0.0, vr))
        cands.append({'symbol': sym, 'side': nm, 'industry': g, 'r15': round(r15, 3), 'industry_pct': round(im, 3), 'market_pct': round(mkt, 3),
                      'resid': round(resid, 3), 'eff': round(eff, 3), 'volrel': round(vr, 2), 'score': round(score, 4), 'entry': b,
                      'entry_poll': p15['t'], 'fresh_s': None, 'exit_rule': sp['exit'], 'reasons': ['INDUSTRY_ALIGNED', 'MARKET_NOT_AGAINST', 'RESIDUAL_LEADER']})
    keep = []
    for nm in ('DOWN', 'UP'):
        sc = sorted((c for c in cands if c['side'] == nm), key=lambda c: -c['score'])[:1 + s['observe_n']]
        for i, c in enumerate(sc): c['role'] = ('PICK' if plan[nm]['active'] else 'SHADOW') if i == 0 else 'OBSERVE'
        keep += sc
    order = {'PICK': 0, 'SHADOW': 1, 'OBSERVE': 2}
    keep.sort(key=lambda c: (order[c['role']], -c['score']))
    return {'at': '09:45', 'poll': p15['t'], 'n': len(rows), 'market_pct': round(mkt, 3), 'eligible': len(cands), 'picks': keep,
            'eligible_by_side': {nm: sum(c['side'] == nm for c in cands) for nm in ('UP', 'DOWN')},
            'rejected': rej, 'rejected_sample': audit[:40], 'baseline_days_min': s['min_vol_days'], 'baseline_days_have': base_have,
            'plan': plan}


def settle(rec: dict, polls: list, s: dict) -> bool:
    """Fill +3/+5/+15/+30/+60 min and exit-time results (signed %, gross) plus MAE/MFE over the elapsed window. True when something changed."""
    changed = False
    t0 = None
    for c in rec.get('picks', []):
        t0 = t0 or dt.datetime.fromisoformat(c['entry_poll'])
        sg = 1 if c['side'] == 'UP' else -1
        out = c.setdefault('out', {})
        path = []
        for p in polls:
            try: t = dt.datetime.fromisoformat(p['t'])
            except Exception: continue  # noqa: BLE001
            if t < t0: continue
            px = _px((p.get('q') or {}).get(c['symbol']), 0)
            if px: path.append((t, sg * (px / c['entry'] - 1) * 100))
        if not path: continue
        for h in HORIZONS:
            if str(h) in out: continue
            tgt = t0 + dt.timedelta(minutes=h)
            cand = [(abs((t - tgt).total_seconds()), t, r) for t, r in path if abs((t - tgt).total_seconds()) <= 150]
            if cand and path[-1][0] >= tgt - dt.timedelta(seconds=150):
                _, t, r = min(cand, key=lambda x: x[0]); out[str(h)] = round(r, 3); changed = True
        ex = _poll_at(polls, s['exit_at'], 8)
        if ex and 'exit' not in out:
            px = _px((ex.get('q') or {}).get(c['symbol']), 0)
            if px:
                out['exit'] = round(sg * (px / c['entry'] - 1) * 100, 3); changed = True
                rr = [r for t, r in path if t <= dt.datetime.fromisoformat(ex['t'])] or [out['exit']]
                c['mae_pct'] = round(min(0.0, min(rr)), 3); c['mfe_pct'] = round(max(0.0, max(rr)), 3); c['settled'] = True
                out['book1'] = BOOK_PCT if c['mfe_pct'] >= BOOK_PCT else out['exit']   # book at +1% (first touch), else held to the exit
    return changed


def tick(now: dt.datetime | None = None) -> None:
    """Called after each order-flow poll: evaluate once at 09:45 (window 09:45-10:10), refresh outcomes every slow poll, settle at the exit time."""
    s = load_settings()
    if not s['enabled']: return
    now = now or d.now_ist(); hm = now.strftime('%H:%M'); today = str(now.date())
    if now.weekday() >= 5 or not ('09:45' <= hm <= '15:25'): return
    with _lock:
        days = d._read_jsonl(FILE); rec = next((x for x in days if x.get('date') == today), None)
        if rec is None and hm > '10:10': return
        if rec is not None and all(c.get('settled') for c in rec.get('picks', [])) and rec.get('picks') is not None and rec.get('done'): return
        polls = d.lite_polls(today)                                       # price + volume only, read incrementally
        if not polls: return
        if rec is None:
            hist = _vol_hist()
            if not hist: hist = bootstrap_vol_hist(today)
            res = compute(polls, hist, industry_map(), s, side_plan(s, _lab_verdicts()))
            if res is None: return
            rec = {'date': today, 'status': 'RESEARCH / NOT VALIDATED', 'settings': {k: s[k] for k in ('side_mode', 'min_group', 'exit_at', 'min_eff', 'min_volrel', 'min_vol_days')}, **res}
            days.append(rec)
            v = _v15_from_polls(polls)                                    # today's 09:30-09:45 volumes extend the baseline, once per day
            for sym, x in v.items():
                L = [e for e in hist.get(sym, []) if e[0] != today]; L.append([today, x]); hist[sym] = L[-25:]
            d._write_json(VOL_FILE, hist)
        settle(rec, polls, s)
        if hm >= s['exit_at'] and (not rec.get('picks') or all(c.get('settled') for c in rec['picks'])): rec['done'] = True
        d._rewrite_jsonl(FILE, days)


def _score(rows: list, key: str | None, cost: float) -> dict:
    """key None = each candidate's own exit rule (exit_rule, default the 15:15 exit)."""
    r = [(c.get('out') or {}).get(key or c.get('exit_rule') or 'exit') for c in rows]
    r = [x for x in r if x is not None]
    if not r: return {'n': 0}
    n = len(r); k = sum(1 for x in r if x > 0); z = 1.96; p = k / n; den = 1 + z * z / n
    lo = ((p + z * z / 2 / n) - z * math.sqrt(p * (1 - p) / n + z * z / 4 / n / n)) / den
    return {'n': n, 'win': round(100 * p, 1), 'lo': round(100 * lo, 1), 'gross_bps': round(float(np.mean(r)) * 100, 1), 'net_bps': round(float(np.mean(r)) * 100 - cost, 1)}


def status() -> dict:
    s = load_settings(); days = d._read_jsonl(FILE); today = str(d.now_ist().date())
    allc = [c for x in days for c in x.get('picks', [])]
    picks = [c for c in allc if c.get('role') == 'PICK']
    top = [c for c in allc if c.get('role') in ('PICK', 'SHADOW')]          # each side's best stock of the day, picked or not
    obs = [c for c in allc if c.get('role') == 'OBSERVE']
    cost = s['cost_bps']
    lab = None
    try:
        from . import v136_hierarchy_lab; lab = v136_hierarchy_lab.status()
    except Exception:  # noqa: BLE001
        log.exception('v135: lab status failed')
    return {'settings': s, 'today': next((x for x in days if x.get('date') == today), None), 'recent': list(reversed(days))[:10], 'days': len(days),
            'plan': side_plan(s, _lab_verdicts()), 'lab': lab,
            'score': {'pick': {k: _score(picks, k, cost) for k in ('exit', '30', '60', '15')},
                      'pick_rule': _score(picks, None, cost),
                      'pick_down': _score([c for c in picks if c['side'] == 'DOWN'], None, cost),
                      'pick_up': _score([c for c in picks if c['side'] == 'UP'], None, cost),
                      'top_down': _score([c for c in top if c['side'] == 'DOWN'], 'exit', cost),
                      'top_up': _score([c for c in top if c['side'] == 'UP'], 'exit', cost),
                      'observe': _score(obs, 'exit', cost)}}
