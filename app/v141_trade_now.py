"""V14.1 "Trade now" panel for the intraday desk (ADMIN ONLY, paper - never places orders).

One place that answers "what do I trade right now, at what price, with what stop and target - and if nothing, why not".
Pure: it only reads the status dicts the desks already publish (v133 direction + v137 pool watch, v135 hierarchy, v132 movers, v140 ACD).

  TAKE   - rules with a tested edge: the daily pool's traded picks, the movement-predictor bracket plan and the NIFTY ACD short.
  PAPER  - research desks that are recorded but not validated (hierarchy PICK). Shown, clearly marked, never mixed with TAKE.
  NOT TODAY - every candidate that was considered and left out, with the reason in plain words.
"""
from __future__ import annotations

REJECT_WORDS = {'NO_VOLUME_BASELINE': 'volume history still filling', 'NO_INDUSTRY': 'no industry group', 'GROUP_TOO_SMALL': 'industry group too small',
                'MISSING_BAR': 'missing prices', 'NO_PATH': 'did not move', 'INDUSTRY_NOT_ALIGNED': 'its industry moved the other way',
                'MARKET_AGAINST': 'market moved against it', 'NO_RESIDUAL_LEADERSHIP': 'not leading its industry', 'MARKET_TOO_WEAK': 'market too flat',
                'LOW_EFF': 'choppy path', 'LOW_VOLUME': 'volume too low'}


def _side_word(side: str) -> str:
    return 'BUY' if side == 'UP' else 'SELL'


def _pool(direction: dict, out: dict) -> None:
    w = ((direction or {}).get('watch') or {}); wt = w.get('today') or {}; ws = w.get('settings') or {}
    today = (direction or {}).get('today') or {}; st = today.get('settings') or {}
    hc = ((today.get('lists') or {}).get('hc') or {})
    book = float(ws.get('book_pct') or st.get('hc_book_pct') or 1.0)
    a_conf, ec_min = st.get('grade_a_conf', 0.14), st.get('ec_min_score', 3)
    if not today:
        out['notes'].append('Daily pool: the 09:25 list appears at about 09:26.'); return
    picks = {p['symbol']: p for p in hc.get('picks') or []}
    best = max((p.get('conf') or 0 for p in picks.values()), default=None)
    out['grade_rule'] = (f"Grade A = model confidence ≥ {a_conf} and entry check ≥ {ec_min}/4."
                         + (f" Today's best confidence was {best:.3f}" + (' - no A grade today.' if best < a_conf else '.') if best is not None else ''))
    for it in (wt.get('items') or []):
        if it.get('source') != '09:25 pool': continue
        p = picks.get(it['symbol'], {}); k = it.get('track') or {}; ec = p.get('entry_check') or {}
        sg = 1 if it['side'] == 'UP' else -1; e = float(it['entry'])
        row = {'desk': 'Daily pool', 'symbol': it['symbol'], 'side': it['side'], 'action': _side_word(it['side']), 'entry': e,
               'entry_at': str(it.get('added', ''))[11:16], 'grade': p.get('grade'), 'pod': bool(p.get('pod')),
               'target': round(e * (1 + sg * book / 100), 2), 'target_note': f'book +{book:g}%', 'stop': k.get('stop_px'),
               'exit': it.get('exit_at') or ws.get('exit_at') or '15:15', 'now_pct': k.get('now_pct')}
        if k.get('closed'): row['state'] = f"closed {'%+.2f' % k['ret_pct']}%" if k.get('ret_pct') is not None else 'closed'
        elif k.get('stopped'): row['state'] = f"stopped at {k.get('stopped_at')}"
        elif k.get('booked'): row['state'] = f"target hit {k.get('booked_at')} - rest held"
        else: row['state'] = 'open'
        if it.get('trade'):
            out['take'].append(row); continue
        if ec.get('label') == 'WEAK':
            why = (f"entry check WEAK {ec.get('score')}/4 - the stock is already stretched (RSI / range / Bollinger / 50-day average "
                   f"at an extreme); such entries earned about 0 in research")
        elif p.get('status') == 'WATCH':
            why = 'lower-ranked pick - watch only (only grade-A picks add trades beyond the first)'
        else:
            why = 'not traded by the pool rules'
        out['skip'].append(dict(row, why=why))


def _hierarchy(direction: dict, out: dict) -> None:
    hz = ((direction or {}).get('v47') or {}); rec = hz.get('today')
    if not rec:
        out['notes'].append('Hierarchy desk: evaluated at 09:45.'); return
    for c in rec.get('picks') or []:
        if c.get('role') != 'PICK': continue
        out['paper'].append({'desk': 'Hierarchy (research)', 'symbol': c['symbol'], 'side': c['side'], 'action': _side_word(c['side']), 'entry': c.get('entry'),
                             'entry_at': str(c.get('entry_poll', ''))[11:16], 'exit': c.get('exit_at') or (hz.get('settings') or {}).get('exit_at') or '15:15',
                             'why': f"leads {c.get('industry')} by {c.get('resid')}%", 'state': 'closed' if c.get('settled') else 'open'})
    if not any(c.get('role') == 'PICK' for c in rec.get('picks') or []):
        rej = rec.get('rejected') or {}
        have, need = rec.get('baseline_days_have'), rec.get('baseline_days_min')
        top = sorted(rej.items(), key=lambda x: -x[1])[:3]
        msg = 'Hierarchy desk: no stock passed today'
        if top: msg += ' (' + ', '.join(f"{n} {REJECT_WORDS.get(k, k.lower())}" for k, n in top) + ')'
        if have is not None and need and have < need:
            msg += f". Volume history has {have} of {need} days - it now fills itself from Kite before the open, so picks can start next session."
        out['notes'].append(msg if msg.endswith('.') else msg + '.')


def _movers(movers: dict, out: dict) -> None:
    mt = (movers or {}).get('today') or {}
    pl = mt.get('plan') or {}
    if not mt:
        out['notes'].append('Movement predictor: top pick at 09:16; bracket levels at about 09:21 on high-confidence days.'); return
    if pl.get('status') == 'NO_PLAN' or (not pl and not mt.get('confident')):
        out['notes'].append(f"Movement predictor: normal-confidence day ({(mt.get('picks') or [{}])[0].get('symbol', '')}) - no bracket trade."); return
    if not pl.get('buy_above'):
        out['notes'].append('Movement predictor: high-confidence day - bracket levels at about 09:21.'); return
    st = pl.get('status'); row = {'desk': 'Movement bracket', 'symbol': pl.get('symbol'), 'exit': '15:15', 'bracket': True,
                                  'buy_above': pl['buy_above'], 'sell_below': pl['sell_below'], 'long_target': pl['long_target'], 'short_target': pl['short_target'],
                                  'long_stop': pl['long_stop'], 'short_stop': pl['short_stop']}
    if st == 'WAITING': row['state'] = 'waiting - place both orders, cancel the other when one fills (no new entry after 12:00)'
    elif st == 'OPEN': row.update(side='UP' if pl['side'] == 'LONG' else 'DOWN', action='BUY' if pl['side'] == 'LONG' else 'SELL', entry=pl['entry'],
                                  entry_at=pl['entry_at'], stop=pl['stop'], target=pl['target'], state='open')
    elif st in ('TARGET', 'STOP', 'CLOSED'): row.update(side='UP' if pl['side'] == 'LONG' else 'DOWN', action='BUY' if pl['side'] == 'LONG' else 'SELL',
                                                         entry=pl['entry'], entry_at=pl['entry_at'], stop=pl['stop'], target=pl['target'],
                                                         state=f"{ {'TARGET': 'target hit', 'STOP': 'stopped', 'CLOSED': 'closed 15:15'}[st] } {pl.get('ret_bps', 0):+.0f} bps")
    else:
        out['notes'].append(f"Movement predictor: {pl.get('note') or st}."); return
    out['take'].append(row)


def _acd(acd: dict, out: dict) -> None:
    day = (acd or {}).get('today') or {}
    if not day:
        out['notes'].append('NIFTY ACD: levels are prepared before 09:15; the watch starts at 09:45.'); return
    s, t, o = day.get('signal') or {}, day.get('trade') or {}, day.get('or') or {}
    fut = (day.get('instruments') or {}).get('fut_symbol') or 'NIFTY FUT'
    if t:
        res = t.get('points') if t.get('state') == 'closed' else t.get('live_points')
        out['take'].append({'desk': 'NIFTY ACD', 'symbol': fut, 'side': 'DOWN', 'action': 'SELL', 'entry': t.get('entry'), 'entry_at': t.get('entry_time'),
                            'stop': t.get('stop_level'), 'target': None, 'target_note': 'no target', 'exit': '15:20',
                            'state': (f"closed {t.get('exit_time')} ({t.get('reason')}) {res:+.1f} pts" if t.get('state') == 'closed'
                                      else f"open {res:+.1f} pts" if isinstance(res, (int, float)) else 'open')})
        return
    if s.get('skipped_week_cap'): out['notes'].append(f"NIFTY ACD: {s.get('side')} signal at {s.get('bar')} skipped - 2 trades already this week."); return
    if s.get('ignored_long'): out['notes'].append(f"NIFTY ACD: first signal was LONG at {s.get('bar')} - shorts-only desk, so no trade today."); return
    if day.get('final'): out['notes'].append('NIFTY ACD: no signal today.'); return
    if (acd or {}).get('week_used', 0) >= (acd or {}).get('week_cap', 2):
        out['notes'].append('NIFTY ACD: 2 trades already this week - no more until Monday.'); return
    lv = day.get('levels') or {}
    if o.get('a_down'):
        out['notes'].append(f"NIFTY ACD: watching until 14:30 - SELL {fut} if NIFTY closes 15 one-minute bars in a row below {o['a_down']} "
                            f"and below the pivot range {lv.get('pr_bot')} (last {(day.get('last') or {}).get('close')}).")
    else:
        out['notes'].append('NIFTY ACD: opening range 09:15-09:44 forming; the watch starts at 09:45.')


def build(direction: dict | None, movers: dict | None, acd: dict | None = None) -> dict:
    out = {'take': [], 'paper': [], 'skip': [], 'notes': [], 'grade_rule': None}
    for fn, src in ((_pool, direction), (_acd, acd), (_hierarchy, direction), (_movers, movers)):
        try:
            fn(src or {}, out)
        except Exception as exc:  # noqa: BLE001 - one broken desk must not hide the others
            out['notes'].append(f'{fn.__name__.strip("_")}: unavailable ({str(exc)[:80]})')
    out['live'] = sum(1 for r in out['take'] if str(r.get('state', '')).startswith(('open', 'waiting', 'target hit')))
    return out
