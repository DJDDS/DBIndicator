import datetime as dt


def test_recorder_operational_health_is_read_only_and_unprotected(monkeypatch, tmp_path):
    from app import config, web

    monkeypatch.setattr(web, "start_background_scanner", lambda: None)
    monkeypatch.setattr(web, "_scanner_started", True)
    monkeypatch.setattr(web.scanner, "now_ist", lambda: dt.datetime(2026, 9, 16, 15, 30))

    root = tmp_path / "index"
    root.mkdir()
    v12_state = tmp_path / "v12_state.json"
    v121_state = tmp_path / "v121_state.json"
    backup_state = tmp_path / "backup_state.json"
    snapshot = tmp_path / "snapshots.jsonl"
    v12_state.write_text('{"quote_error_count": 0}', encoding="utf-8")
    v121_state.write_text('{"current_day":"2026-09-16","status":"RECORDING","token_count":53}', encoding="utf-8")
    backup_state.write_text('{"status":"OFF-BOX BACKUP NOT CONFIGURED"}', encoding="utf-8")
    snapshot.write_text("{}\n", encoding="utf-8")

    monkeypatch.setattr(config, "V12_OPTION_SNAPSHOT_FILE", str(snapshot))
    monkeypatch.setattr(config, "V12_OPTION_STATE_FILE", str(v12_state))
    monkeypatch.setattr(config, "V121_INDEX_VOL_ROOT", str(root))
    monkeypatch.setattr(config, "V121_INDEX_VOL_STATE_FILE", str(v121_state))
    monkeypatch.setattr(config, "V121_INDEX_VOL_BACKUP_STATE_FILE", str(backup_state))
    monkeypatch.setattr(config, "V12_STORAGE_MODE", "persistent")
    monkeypatch.setattr(config, "DASHBOARD_PASSWORD", "secret")

    response = web.app.test_client().get("/api/recorder-operational-health")

    assert response.status_code == 200
    payload = response.get_json()
    assert set(payload) == {"observed_at", "storage_mode", "v12", "v121", "backup", "connection"}
    assert payload["v121"]["status"] == "RECORDING"
    assert payload["v121"]["token_count"] == 53
    assert payload["backup"]["status"] == "OFF-BOX BACKUP NOT CONFIGURED"
    assert "password" not in response.get_data(as_text=True).lower()
