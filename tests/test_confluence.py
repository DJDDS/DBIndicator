import base64

import pytest

from app import chart_patterns, confluence, security, v135_hierarchy, web


def _patch(monkeypatch, pattern_rows, ind_map, results):
    monkeypatch.setattr(chart_patterns, 'load_results', lambda: {'results': []})
    monkeypatch.setattr(chart_patterns, 'public_rows', lambda payload: pattern_rows)
    monkeypatch.setattr(v135_hierarchy, 'industry_map', lambda: ind_map)
    confluence._CACHE.update(at=0.0, value=[])
    return results


# IT Services industry (4 members, meets MIN_INDUSTRY_GROUP): TCS +0.5, INFY -0.5, WIPRO +0.3, HCLTECH +0.1
# -> industry median +0.2. Plus four unmapped stocks (RELIANCE 0.0, ONGC -0.05, TATASTEEL +0.02, HINDALCO -0.02)
# -> whole-market median +0.01. Industry edge = +0.19, over the +0.15pp threshold either way.
# TCS (Long Buildup, bullish OI) sits on the side the industry is actually outperforming -> sector confirms.
# INFY (Short Buildup, bearish OI) needs the industry to be UNDERperforming to confirm a bearish call, but
# IT Services is outperforming (same +0.19pp edge TCS sees) -> sector does NOT confirm INFY.
_IND_MAP = {'TCS': 'IT Services', 'INFY': 'IT Services', 'WIPRO': 'IT Services', 'HCLTECH': 'IT Services'}
_RESULTS = [
    {'symbol': 'TCS', 'oi_structure': 'Long Buildup', 'price_chg_today_pct': 0.5},
    {'symbol': 'INFY', 'oi_structure': 'Short Buildup', 'price_chg_today_pct': -0.5},
    {'symbol': 'WIPRO', 'price_chg_today_pct': 0.3},
    {'symbol': 'HCLTECH', 'price_chg_today_pct': 0.1},
    {'symbol': 'RELIANCE', 'price_chg_today_pct': 0.0},
    {'symbol': 'ONGC', 'price_chg_today_pct': -0.05},
    {'symbol': 'TATASTEEL', 'price_chg_today_pct': 0.02},
    {'symbol': 'HINDALCO', 'price_chg_today_pct': -0.02},
]
_PATTERN_ROWS = [
    {'symbol': 'TCS', 'pattern': 'Bull Flag', 'status': 'BREAKOUT', 'direction': 'BULL', 'tier': 'A', 'tier_rank': 0},
    {'symbol': 'INFY', 'pattern': 'Head and Shoulders', 'status': 'RETEST_HOLD', 'direction': 'BEAR', 'tier': 'WATCH', 'tier_rank': 1},
    {'symbol': 'WIPRO', 'pattern': 'Ascending Triangle', 'status': 'FORMING', 'direction': 'BULL', 'tier': 'NONE', 'tier_rank': 2},
    {'symbol': 'ZEEL', 'pattern': 'Descending Triangle', 'status': 'EXTENDED', 'direction': 'BEAR', 'tier': 'NONE', 'tier_rank': 2},
]


def test_confluence_requires_oi_structure_row_to_qualify(monkeypatch):
    # WIPRO has no oi_structure field on its live scan row at all -> never reaches the confirmation stage,
    # regardless of its (FORMING, inactive) pattern.
    _patch(monkeypatch, _PATTERN_ROWS, _IND_MAP, _RESULTS)
    rows = confluence.watch(_RESULTS)
    assert not any(r['symbol'] == 'WIPRO' for r in rows)


def test_confluence_excludes_stock_with_no_live_scan_row(monkeypatch):
    # ZEEL has an EXTENDED bear pattern but isn't in the live scan results at all (no OI row, nothing to
    # confirm against) -> pattern alone is never enough to make the list.
    _patch(monkeypatch, _PATTERN_ROWS, _IND_MAP, _RESULTS)
    rows = confluence.watch(_RESULTS)
    assert not any(r['symbol'] == 'ZEEL' for r in rows)


def test_confluence_oi_and_sector_confirm_and_pattern_bonus_scores_three(monkeypatch):
    _patch(monkeypatch, _PATTERN_ROWS, _IND_MAP, _RESULTS)
    rows = confluence.watch(_RESULTS)
    assert len(rows) == 1
    tcs = rows[0]
    assert tcs['symbol'] == 'TCS'
    assert tcs['score'] == 3                       # OI + sector + matching active BREAKOUT pattern
    assert tcs['direction'] == 'UP'
    assert tcs['oi_structure'] == 'Long Buildup'
    assert tcs['industry'] == 'IT Services'
    assert tcs['industry_edge_pct'] > 0
    assert tcs['pattern'] == {'name': 'Bull Flag', 'status': 'BREAKOUT', 'tier': 'A'}


def test_confluence_oi_without_sector_backing_is_excluded(monkeypatch):
    # INFY has an agreeing bear pattern too, but with no sector backing on the bearish side it's excluded.
    _patch(monkeypatch, _PATTERN_ROWS, _IND_MAP, _RESULTS)
    rows = confluence.watch(_RESULTS)
    assert not any(r['symbol'] == 'INFY' for r in rows)


def test_confluence_non_empty_when_oi_and_sector_agree_even_with_zero_patterns(monkeypatch):
    # The direct regression test for "mostly empty": with no active patterns at all (empty pattern_rows,
    # simulating most of the trading day before the once-daily 15:45 IST scan), OI+sector alone must still
    # produce rows, scored 2/3.
    _patch(monkeypatch, [], _IND_MAP, _RESULTS)
    rows = confluence.watch(_RESULTS)
    assert len(rows) == 1
    assert rows[0]['symbol'] == 'TCS'
    assert rows[0]['score'] == 2
    assert rows[0]['pattern'] is None


# Two industries, both clear of the sector threshold on the bullish side: GrpA (modest edge) and GrpC (large
# edge), with eight very-negative unmapped filler rows to pull the whole-market median well below both so
# neither edge depends on symmetric group placement.
def _two_industry_results():
    ind_map = {'A1': 'GrpA', 'A2': 'GrpA', 'A3': 'GrpA', 'A4': 'GrpA',
               'C1': 'GrpC', 'C2': 'GrpC', 'C3': 'GrpC', 'C4': 'GrpC'}
    results = (
        [{'symbol': 'A1', 'oi_structure': 'Long Buildup', 'price_chg_today_pct': 1.0}] +
        [{'symbol': f'A{i}', 'price_chg_today_pct': 1.0} for i in (2, 3, 4)] +
        [{'symbol': 'C1', 'oi_structure': 'Long Buildup', 'price_chg_today_pct': 6.0}] +
        [{'symbol': f'C{i}', 'price_chg_today_pct': 6.0} for i in (2, 3, 4)] +
        [{'symbol': f'Z{i}', 'price_chg_today_pct': -10.0} for i in range(1, 9)]
    )
    return ind_map, results


def test_confluence_higher_score_outranks_larger_industry_edge(monkeypatch):
    ind_map, results = _two_industry_results()
    # Only A1 has a matching active pattern -> A1 scores 3/3, C1 scores 2/3, even though C1's industry
    # (GrpC, +10.5pp edge) is pulling away from the market harder than A1's (GrpA, +5.5pp edge).
    pattern_rows = [{'symbol': 'A1', 'pattern': 'Bull Flag', 'status': 'BREAKOUT', 'direction': 'BULL',
                      'tier': 'A', 'tier_rank': 0}]
    _patch(monkeypatch, pattern_rows, ind_map, results)
    rows = confluence.watch(results)
    syms = [r['symbol'] for r in rows]
    assert syms == ['A1', 'C1']
    assert rows[0]['score'] == 3 and rows[1]['score'] == 2


def test_confluence_larger_industry_edge_sorts_first_at_equal_score(monkeypatch):
    ind_map, results = _two_industry_results()
    # No patterns at all -> both A1 and C1 score 2/3; GrpC's larger sector edge sorts it first.
    _patch(monkeypatch, [], ind_map, results)
    rows = confluence.watch(results)
    syms = [r['symbol'] for r in rows]
    assert syms == ['C1', 'A1']
    assert rows[0]['score'] == rows[1]['score'] == 2


def test_confluence_larger_price_change_breaks_tie_within_same_industry(monkeypatch):
    # X1 and X2 are in the same industry, so they share the exact same industry_edge_pct; the tie is
    # broken by the bigger |price_chg_today_pct|.
    ind_map = {'X1': 'GrpX', 'X2': 'GrpX', 'X3': 'GrpX', 'X4': 'GrpX'}
    results = [
        {'symbol': 'X1', 'oi_structure': 'Long Buildup', 'price_chg_today_pct': 3.0},
        {'symbol': 'X2', 'oi_structure': 'Long Buildup', 'price_chg_today_pct': 4.0},
        {'symbol': 'X3', 'price_chg_today_pct': 3.0},
        {'symbol': 'X4', 'price_chg_today_pct': 3.0},
        {'symbol': 'Y1', 'price_chg_today_pct': 0.0},
        {'symbol': 'Y2', 'price_chg_today_pct': 0.0},
        {'symbol': 'Y3', 'price_chg_today_pct': 0.0},
        {'symbol': 'Y4', 'price_chg_today_pct': 0.0},
    ]
    _patch(monkeypatch, [], ind_map, results)
    rows = confluence.watch(results)
    syms = [r['symbol'] for r in rows]
    assert syms == ['X2', 'X1']
    assert rows[0]['industry_edge_pct'] == rows[1]['industry_edge_pct']


def test_confluence_caps_at_max_rows(monkeypatch):
    # 25 same-industry members, all qualifying, plus 30 unmapped filler rows at a far lower value so the
    # market median sits well below the group's own median -> all 25 confirm, output caps at MAX_ROWS (20).
    ind_map = {f'SYM{i}': 'BigGroup' for i in range(25)}
    results = [{'symbol': f'SYM{i}', 'oi_structure': 'Long Buildup', 'price_chg_today_pct': 5.0} for i in range(25)]
    results += [{'symbol': f'FILL{i}', 'price_chg_today_pct': 0.0} for i in range(30)]
    _patch(monkeypatch, [], ind_map, results)
    rows = confluence.watch(results)
    assert len(rows) == confluence.MAX_ROWS == 20


def test_confluence_is_cached_per_ttl_window(monkeypatch):
    calls = {'patterns': 0, 'ind': 0}

    def fake_public_rows(payload):
        calls['patterns'] += 1
        return []

    monkeypatch.setattr(chart_patterns, 'load_results', lambda: {'results': []})
    monkeypatch.setattr(chart_patterns, 'public_rows', fake_public_rows)
    monkeypatch.setattr(v135_hierarchy, 'industry_map', lambda: (calls.__setitem__('ind', calls['ind'] + 1), {})[1])
    confluence._CACHE.update(at=0.0, value=[])
    confluence.watch([])
    confluence.watch([])
    confluence.watch([])
    assert calls == {'patterns': 1, 'ind': 1}


def test_confluence_keeps_stale_value_on_error(monkeypatch):
    confluence._CACHE.update(at=0.0, value=[{'symbol': 'STALE'}])

    def boom():
        raise RuntimeError('pattern file corrupt')

    monkeypatch.setattr(chart_patterns, 'load_results', boom)
    try:
        rows = confluence.watch([])
        assert rows == [{'symbol': 'STALE'}]
    finally:
        confluence._CACHE.update(at=0.0, value=[])   # don't leak the malformed stale row into later tests


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv('DBI_OWNER_USERNAME', 'owner')
    monkeypatch.setenv('DBI_OWNER_PASSWORD', 'test-owner-confluence-password')
    monkeypatch.setattr(web, '_scanner_started', True)
    security._AUTH_FAILURES.clear()
    confluence._CACHE.update(at=0.0, value=[])   # earlier tests may have left a stale/fake cached value
    return web.app.test_client()


def _auth():
    return {'Authorization': 'Basic ' + base64.b64encode(b'owner:test-owner-confluence-password').decode()}


def test_dashboard_state_api_exposes_confluence_watch(client, monkeypatch):
    monkeypatch.setattr(web, '_confluence_watch_safe',
                         lambda results: [{'symbol': 'TCS', 'direction': 'UP', 'price_chg_today_pct': 0.52,
                                            'oi_structure': 'Long Buildup', 'industry': 'IT Services',
                                            'industry_edge_pct': 0.19,
                                            'pattern': {'name': 'Bull Flag', 'status': 'BREAKOUT', 'tier': 'A'},
                                            'score': 3}])
    r = client.get('/api/dashboard-state', headers=_auth())
    assert r.status_code == 200
    body = r.get_json()
    assert body.get('confluence_watch')[0]['symbol'] == 'TCS'


def test_dashboard_page_renders_confluence_watch_markup(client):
    r = client.get('/', headers=_auth())
    assert r.status_code == 200
    assert b'cwatch-tbody' in r.data
    assert b'oi-structure-badge' in r.data
