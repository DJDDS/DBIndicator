import json

from app import config


def test_base_oi_screener_keeps_valid_2plus_tier_rows_even_when_z_is_quiet():
    from app.oi_view import select_oi_screener_rows

    rows = [
        {"symbol": "QUIET", "param_tier": 3, "oi": 1000, "oi_z": 0.2,
         "oi_chg_60m_pct": 1.2, "oi_acceleration": 0.4, "oi_chg_30m_pct": 0.8, "oi_day_chg_pct": 2.0},
        {"symbol": "UNUSUAL", "param_tier": 2, "oi": 2000, "oi_z": 2.5,
         "oi_chg_60m_pct": 0.4, "oi_acceleration": 0.1, "oi_chg_30m_pct": 0.3, "oi_day_chg_pct": 1.0},
        {"symbol": "ONE", "param_tier": 1, "oi": 3000, "oi_z": 4.0},
        {"symbol": "NOOI", "param_tier": 4, "oi": None, "oi_z": None},
    ]

    selected = select_oi_screener_rows(rows, unusual_only=False, min_tier=2, z_threshold=1.5)
    assert [r["symbol"] for r in selected] == ["QUIET", "UNUSUAL"]


def test_unusual_only_is_an_optional_filter_not_the_base_universe():
    from app.oi_view import select_oi_screener_rows

    rows = [
        {"symbol": "QUIET", "param_tier": 3, "oi": 1000, "oi_z": 0.2},
        {"symbol": "UNUSUAL", "param_tier": 2, "oi": 2000, "oi_z": -2.5},
    ]
    selected = select_oi_screener_rows(rows, unusual_only=True, min_tier=2, z_threshold=1.5)
    assert [r["symbol"] for r in selected] == ["UNUSUAL"]


def test_oi_screener_default_sort_prefers_recent_60m_then_acceleration():
    from app.oi_view import select_oi_screener_rows

    rows = [
        {"symbol": "HIGHZ", "param_tier": 4, "oi": 1000, "oi_z": 5.0,
         "oi_chg_60m_pct": 0.5, "oi_acceleration": 3.0, "oi_chg_30m_pct": 3.0, "oi_day_chg_pct": 10.0},
        {"symbol": "RECENT", "param_tier": 3, "oi": 1000, "oi_z": 0.1,
         "oi_chg_60m_pct": -2.0, "oi_acceleration": 0.1, "oi_chg_30m_pct": 0.2, "oi_day_chg_pct": 0.3},
    ]
    selected = select_oi_screener_rows(rows, unusual_only=False, min_tier=2, z_threshold=1.5)
    assert [r["symbol"] for r in selected] == ["RECENT", "HIGHZ"]


def test_oi_history_readiness_reports_partial_rolling_history():
    from app.oi_view import oi_history_readiness

    rows = [
        {"symbol": "A", "param_tier": 3, "oi": 100, "oi_chg_30m_pct": 0.4, "oi_chg_60m_pct": None},
        {"symbol": "B", "param_tier": 2, "oi": 200, "oi_chg_30m_pct": 0.5, "oi_chg_60m_pct": 0.8},
    ]
    status = oi_history_readiness(rows)
    assert status == {"eligible_with_oi": 2, "ready_30m": 2, "ready_60m": 1, "warming_up": True}


def test_legacy_strict_4of4_setting_migrates_once_to_3of4(tmp_path, monkeypatch):
    settings_file = tmp_path / "scanner_settings.json"
    settings_file.write_text(json.dumps({"MIN_REQUIRED": 4}))
    monkeypatch.setattr(config, "SETTINGS_FILE", str(settings_file))

    s = config.Settings()
    assert s.MIN_REQUIRED == 3
    saved = json.loads(settings_file.read_text())
    assert saved["_schema_version"] == config.SETTINGS_SCHEMA_VERSION

    # Once migrated, a user can deliberately choose strict 4-of-4 again.
    saved["MIN_REQUIRED"] = 4
    settings_file.write_text(json.dumps(saved))
    s2 = config.Settings()
    assert s2.MIN_REQUIRED == 4


def test_oi_screener_template_has_optional_unusual_filter_and_warmup_status():
    text = open('app/templates/oi_screener.html', encoding='utf-8').read()
    assert 'id="f-unusual"' in text
    assert 'id="oi-history-status"' in text
    assert 'Unusual OI only' in text


def test_oi_screener_endpoint_uses_base_selector_instead_of_hard_z_gate():
    text = open('app/web.py', encoding='utf-8').read()
    assert 'select_oi_screener_rows' in text
    assert 'oi_history_readiness' in text
    assert 'abs(r["oi_z"]) >= threshold' not in text


def test_oi_api_row_is_compact_json_safe_and_numeric_strings_are_normalized():
    import numpy as np
    from app.oi_view import serialize_oi_screener_row

    row = {
        'symbol': 'ABC', 'close': '123.45', 'price_chg_today_pct': np.float64(1.25),
        'oi_total': np.int64(123456), 'oi_day_chg_pct': '2.50',
        'oi_chg_15m_pct': '-0.75', 'oi_chg_30m_pct': np.float32(1.5),
        'oi_chg_60m_pct': None, 'oi_acceleration': '0.40',
        'oi_accel_label': 'Stable', 'oi_structure': 'Long Buildup',
        'vol_multiple': '1.80', 'direction': 'Bullish', 'oi_z': np.float64(1.7),
        'param_tier': np.int64(3), 'irrelevant_dataframe_like_field': object(),
    }
    got = serialize_oi_screener_row(row)
    assert got == {
        'symbol': 'ABC', 'close': 123.45, 'price_chg_today_pct': 1.25,
        'oi_total': 123456.0, 'oi_day_chg_pct': 2.5,
        'oi_chg_15m_pct': -0.75, 'oi_chg_30m_pct': 1.5,
        'oi_chg_60m_pct': None, 'oi_acceleration': 0.4,
        'oi_accel_label': 'Stable', 'oi_structure': 'Long Buildup',
        'vol_multiple': 1.8, 'direction': 'Bullish', 'oi_z': 1.7,
        'param_tier': 3.0,
    }


def test_oi_screener_frontend_defensively_converts_restored_numeric_strings():
    text = open('app/templates/oi_screener.html', encoding='utf-8').read()
    assert 'function oiNumber(value)' in text
    assert 'var n = oiNumber(v);' in text
    assert "fmtNum(r.close)" in text
    assert "if (!r.ok) throw new Error('OI screener failed: ' + r.status);" in text


def test_oi_endpoint_serializes_selected_rows_instead_of_returning_full_scanner_rows():
    text = open('app/web.py', encoding='utf-8').read()
    assert 'serialize_oi_screener_row' in text
    assert 'results = [serialize_oi_screener_row(row) for row in selected]' in text


def test_serialized_oi_row_is_strict_json_safe():
    import json
    import numpy as np
    from app.oi_view import serialize_oi_screener_row
    got = serialize_oi_screener_row({'symbol':'ABC', 'oi':np.int64(12), 'close':'101.2', 'oi_z':float('nan')})
    encoded = json.dumps(got, allow_nan=False)
    assert 'ABC' in encoded
    assert got['oi_total'] == 12.0
    assert got['oi_z'] is None
