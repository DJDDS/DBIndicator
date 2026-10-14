"""Desks at a glance: one small read-only payload for the main dashboard strip (owner only). Nothing here places orders or writes anything.

Every field comes from files/caches the desks already keep, so the strip never adds Kite calls:
  pod  - today's Pick of the day (the Daily Pool's #1 DOWN pick) with its grade, and the paper record of the last settled picks
  rs   - the Relative-Strength desk's latest refresh today
  ng   - the natural gas playbook's current signal (cached status, may be empty until its first background run)
  rec  - recorder health from the recorder monitor
  kite - whether the broker login is valid today
"""
from __future__ import annotations


def _pct(x):
    return None if not isinstance(x, (int, float)) else round(float(x), 2)


def pod_block(days: list, today: str, last_n: int = 30) -> dict:
    """days: direction-desk day records (oldest first)."""
    out = {'today': None, 'record': None}
    d = next((x for x in reversed(days[-8:]) if x.get('date') == today), None)
    L = ((d or {}).get('lists') or {}).get('hc')
    if L and L.get('picks'):
        picks = L['picks']; p = picks[0]
        out['today'] = {'symbol': p.get('symbol'), 'side': p.get('side'), 'grade': p.get('grade'), 'conf': _pct(p.get('conf')), 'entry': p.get('entry'),
                        'status': p.get('status'), 'ret_pct': _pct(p.get('stop_ret_pct', p.get('ret_pct'))), 'win': p.get('win'), 'lean': L.get('lean'),
                        'strong': bool(L.get('strong')), 'trades': sum(1 for q in picks if q.get('status') == 'OPEN'), 'exit_at': L.get('exit_at')}
    done = [x['lists']['hc']['picks'][0] for x in days if (x.get('lists') or {}).get('hc') and x['lists']['hc'].get('picks')
            and isinstance(x['lists']['hc']['picks'][0].get('win'), bool)][-last_n:]
    if done:
        a = [p for p in done if p.get('grade') == 'A']
        out['record'] = {'n': len(done), 'win': round(100 * sum(1 for p in done if p['win']) / len(done), 1),
                         'a_n': len(a), 'a_win': round(100 * sum(1 for p in a if p['win']) / len(a), 1) if a else None}
    return out


def rs_block(days: list, today: str) -> dict | None:
    d = next((x for x in reversed(days[-8:]) if x.get('date') == today), None)
    slots = (d or {}).get('slots') or {}
    if not slots: return None
    at = sorted(slots)[-1]; sl = slots[at]; picks = sl.get('picks') or []
    top = max(picks, key=lambda p: abs(p.get('z', 0))) if picks else None
    done = [p for s in slots.values() for p in s.get('picks', []) if 'ret_pct' in p]
    return {'at': at, 'n': len(picks), 'regime': sl.get('regime'), 'market_pct': sl.get('market_pct'),
            'top': None if not top else {'symbol': top['symbol'], 'side': top['side'], 'z': top.get('z'), 'grade': top.get('grade'), 'reserve': bool(top.get('reserve'))},
            'settled': len(done), 'wins': sum(1 for p in done if p.get('win'))}


def ng_block(st: dict | None) -> dict | None:
    if not st: return None
    sig = st.get('signal') or None
    return {'signal': None if not sig else {'side': sig.get('side'), 'leg': sig.get('leg'), 'hold': sig.get('hold'), 'provisional': sig.get('provisional')},
            'next_check': st.get('next_check'), 'open': (st.get('session') or {}).get('is_open'), 'error': st.get('error')}


def glance(days: list, rs_days: list, today: str, ng_status: dict | None, rec_summary: dict | None, kite_ok: bool) -> dict:
    return {'today': today, 'kite': bool(kite_ok), 'pod': pod_block(days, today), 'rs': rs_block(rs_days, today), 'ng': ng_block(ng_status), 'rec': rec_summary}
