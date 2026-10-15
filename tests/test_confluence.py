import base64

import pytest

from app import chart_patterns, confluence, security, v135_hierarchy, web


def _patch(monkeypatch, pattern_rows, ind_map, results):
    monkeypatch.setattr(chart_patterns, 'load_results', lambda: {'results': []})
    monkeypatch.setattr(chart_patterns, 'public_rows', lambda payload: pattern_rows)
    monkeypatch.setattr(v135_hierarchy, 'industry_map', lambda: ind_map)
    confluence._CACHE.update(at=0.0, value=[])
    return results


def test_confluence_sector_floor_excludes_small_industries(monkeypatch):
    # TinyGroup has only 3 F&O members (below MIN_INDUSTRY_GROUP=4), so it never becomes a trusted median
    # and can never reach the leaderboard, however extreme its today-return is.
    ind_map = {'T1': 'TinyGroup', 'T2': 'TinyGroup', 'T3': 'TinyGroup',
               'B1': 'BigGroup', 'B2': 'BigGroup', 'B3': 'BigGroup', 'B4': 'BigGroup'}
    results = [
        {'symbol': 'T1', 'oi_structure': 'Long Buildup', 'price_chg_today_pct': 20.0},
        {'symbol': 'T2', 'price_chg_today_pct': 20.0},
        {'symbol': 'T3', 'price_chg_today_pct': 20.0},
        {'symbol': 'B1', 'oi_structure': 'Long Buildup', 'price_chg_today_pct': 2.0},
        {'symbol': 'B2', 'price_chg_today_pct': 2.0},
        {'symbol': 'B3', 'price_chg_today_pct': 2.0},
        {'symbol': 'B4', 'price_chg_today_pct': 2.0},
    ]
    _patch(monkeypatch, [], ind_map, results)
    rows = confluence.watch(results)
    assert not any(r['symbol'] == 'T1' for r in rows)
    assert any(r['symbol'] == 'B1' for r in rows)


# Three industries, each exactly at the MIN_INDUSTRY_GROUP floor, spread well apart so the overall market
# median sits cleanly in the middle one. With TOP_N_SECTORS patched to 1, only the top (hi) and bottom (lo)
# industries make the leaderboard; the middle (mid) industry is dropped entirely, regardless of any OI bias
# its members carry.
def _hi_mid_lo_results():
    ind_map = {}
    results = []
    for prefix, chg in (('H', 5.0), ('M', 0.0), ('L', -5.0)):
        for i in range(1, 5):
            sym = f'{prefix}{i}'
            ind_map[sym] = {'H': 'Hi', 'M': 'Mid', 'L': 'Lo'}[prefix]
            results.append({'symbol': sym, 'price_chg_today_pct': chg})
    # overwrite the first two members of Hi/Mid/Lo with explicit OI reads for the tests below
    by_sym = {r['symbol']: r for r in results}
    by_sym['H1']['oi_structure'] = 'Long Buildup'    # bullish OI, in the GAINER sector -> agrees
    by_sym['H2']['oi_structure'] = 'Short Buildup'   # bearish OI, in the GAINER sector -> contra
    by_sym['M1']['oi_structure'] = 'Long Buildup'    # bullish OI, but Mid never makes the leaderboard
    by_sym['L1']['oi_structure'] = 'Short Buildup'   # bearish OI, in the LOSER sector -> agrees
    by_sym['L2']['oi_structure'] = 'Long Buildup'    # bullish OI, in the LOSER sector -> contra
    return ind_map, results


def test_confluence_classifies_gainer_and_loser_sectors_and_drops_the_middle(monkeypatch):
    monkeypatch.setattr(confluence, 'TOP_N_SECTORS', 1)
    ind_map, results = _hi_mid_lo_results()
    _patch(monkeypatch, [], ind_map, results)
    rows = confluence.watch(results)
    syms = {r['symbol'] for r in rows}
    assert 'H1' in syms and 'H2' in syms
    assert 'L1' in syms and 'L2' in syms
    assert 'M1' not in syms                           # Mid is neither today's best nor worst -> dropped
    assert all(r['industry'] != 'Mid' for r in rows)
    h1 = next(r for r in rows if r['symbol'] == 'H1')
    l1 = next(r for r in rows if r['symbol'] == 'L1')
    assert h1['sector_trend'] == 'GAINER'
    assert l1['sector_trend'] == 'LOSER'


def test_confluence_agree_flag_reflects_oi_direction_vs_sector_trend(monkeypatch):
    monkeypatch.setattr(confluence, 'TOP_N_SECTORS', 1)
    ind_map, results = _hi_mid_lo_results()
    _patch(monkeypatch, [], ind_map, results)
    rows = confluence.watch(results)
    by_sym = {r['symbol']: r for r in rows}
    assert by_sym['H1']['agree'] is True and by_sym['H1']['score'] == 2     # bullish OI, gainer sector
    assert by_sym['H2']['agree'] is False and by_sym['H2']['score'] == 1    # bearish OI, gainer sector
    assert by_sym['L1']['agree'] is True and by_sym['L1']['score'] == 2     # bearish OI, loser sector
    assert by_sym['L2']['agree'] is False and by_sym['L2']['score'] == 1    # bullish OI, loser sector


def test_confluence_pattern_bonus_only_applies_when_oi_agrees_with_sector(monkeypatch):
    monkeypatch.setattr(confluence, 'TOP_N_SECTORS', 1)
    ind_map, results = _hi_mid_lo_results()
    pattern_rows = [
        {'symbol': 'H1', 'pattern': 'Bull Flag', 'status': 'BREAKOUT', 'direction': 'BULL', 'tier': 'A', 'tier_rank': 0},
        # H2's own OI read is bearish and its pattern agrees with THAT (not with the sector) - since H2 is
        # contra (agree=False), the bonus must not apply even though the pattern matches its OI direction.
        {'symbol': 'H2', 'pattern': 'Head and Shoulders', 'status': 'RETEST_HOLD', 'direction': 'BEAR', 'tier': 'A', 'tier_rank': 0},
    ]
    _patch(monkeypatch, pattern_rows, ind_map, results)
    rows = confluence.watch(results)
    by_sym = {r['symbol']: r for r in rows}
    assert by_sym['H1']['score'] == 3
    assert by_sym['H1']['pattern'] == {'name': 'Bull Flag', 'status': 'BREAKOUT', 'tier': 'A'}
    assert by_sym['H2']['score'] == 1                 # still contra -> no pattern bonus
    assert by_sym['H2']['pattern'] is None


# Two gainer-side industries (TOP_N_SECTORS defaults to 4, and with only 2 industries total both end up
# GAINER after the top/bottom dedup) with eight very-negative unmapped filler rows pulling the market
# median well below both, so neither edge depends on symmetric group placement.
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
    _patch(monkeypatch, [], ind_map, results)   # no patterns -> both A1 and C1 score 2/3
    rows = confluence.watch(results)
    syms = [r['symbol'] for r in rows]
    assert syms == ['C1', 'A1']
    assert rows[0]['score'] == rows[1]['score'] == 2


def test_confluence_larger_price_change_breaks_tie_within_same_sector(monkeypatch):
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
    # 35 same-industry members, all qualifying, plus 40 unmapped filler rows at a far lower value (more
    # filler than group members, so the market median lands in the filler block) -> all 35 confirm, output
    # caps at MAX_ROWS (30).
    ind_map = {f'SYM{i}': 'BigGroup' for i in range(35)}
    results = [{'symbol': f'SYM{i}', 'oi_structure': 'Long Buildup', 'price_chg_today_pct': 5.0} for i in range(35)]
    results += [{'symbol': f'FILL{i}', 'price_chg_today_pct': 0.0} for i in range(40)]
    _patch(monkeypatch, [], ind_map, results)
    rows = confluence.watch(results)
    assert len(rows) == confluence.MAX_ROWS == 30


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


def test_dashboard_shows_market_pulse_instead_of_confluence_watch(client):
    """Confluence Watch was replaced on the dashboard by Market Pulse (15-Oct-2026); the confluence module stays for reuse."""
    r = client.get('/', headers=_auth())
    assert r.status_code == 200
    assert b'id="market-pulse"' in r.data and b'cwatch-table' not in r.data
    r = client.get('/api/market-pulse', headers=_auth())
    assert r.status_code == 200 and r.is_json
