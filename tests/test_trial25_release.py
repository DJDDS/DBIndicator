from pathlib import Path

from app import v12_storage


ROOT = Path(__file__).parents[1]
TRIAL25_MODULES = (
    "trial25_calendar.py",
    "trial25_execution.py",
    "trial25_shadow.py",
    "trial25_stage_d.py",
    "trial25_universe.py",
)

EXPECTED_FROZEN_HASHES = {
    "v12_option_state_10d_2026-09-21.json": "4cb686ec834627c83103611229bf0924526655c36a44d9376abda131f32c33f4",
    "v12_feasibility_code_10d_2026-09-21.py": "949f7c08c1ecfea9ff130468ef50a3687ae5241c94043513efd3809120f352d7",
    "v12_feasibility_10d_2026-09-21.json": "d14361328e8ed09a5ecb81071e55ceff9d890f76dac3c5154e38e5dc32871260",
}


def _trial25_source():
    return "\n".join(
        (ROOT / "app" / name).read_text(encoding="utf-8")
        for name in TRIAL25_MODULES
    ).lower()


def test_trial25_has_no_order_or_alert_surface():
    source = _trial25_source()
    assert "place_order" not in source
    assert "telegram" not in source
    assert "send_alert" not in source
    assert "send_message" not in source


def test_trial25_has_no_raw_quote_export_route():
    web = (ROOT / "app" / "web.py").read_text(encoding="utf-8").lower()
    assert "/api/trial25-raw" not in web
    assert "trial25_raw_quotes_file" not in web


def test_trial25_runtime_files_resolve_under_persistent_v12_volume():
    resolved = v12_storage.resolve_v12_storage({"RAILWAY_VOLUME_MOUNT_PATH": "/data"})
    assert resolved["persistent"] is True
    for key in (
        "trial25_state", "trial25_ledger", "trial25_raw_quotes",
        "trial25_stage_d", "trial25_stage_d_hash", "trial25_universe_state",
        "trial25_universe_ledger",
    ):
        assert resolved[key].startswith("/data/v12/trial25/")


def test_release_carries_exact_production_freeze_hash_contract():
    assert EXPECTED_FROZEN_HASHES == {
        "v12_option_state_10d_2026-09-21.json": "4cb686ec834627c83103611229bf0924526655c36a44d9376abda131f32c33f4",
        "v12_feasibility_code_10d_2026-09-21.py": "949f7c08c1ecfea9ff130468ef50a3687ae5241c94043513efd3809120f352d7",
        "v12_feasibility_10d_2026-09-21.json": "d14361328e8ed09a5ecb81071e55ceff9d890f76dac3c5154e38e5dc32871260",
    }


def test_trial25_preregistration_keeps_post_freeze_fno_additions_out():
    text = (ROOT / "TRIAL25_PREREGISTRATION.md").read_text(encoding="utf-8")
    assert "No later NSE F&O addition may enter Trial 25" in text
    assert "currently listed qualifying OPTSTK expiry at entry" in text
