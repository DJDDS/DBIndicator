import datetime as dt
from pathlib import Path

IST = dt.timezone(dt.timedelta(hours=5, minutes=30))


def _args(tmp_path):
    return dict(
        now=dt.datetime(2026, 10, 8, 15, 10, tzinfo=IST),
        option_snapshot_file=tmp_path/"snap.jsonl",
        option_state_file=tmp_path/"opt_state.json",
        earnings_state_file=tmp_path/"earn_state.json",
    )


def test_trial25_processing_runs_after_normal_v12_recorder(monkeypatch, tmp_path):
    from app import v12_live
    calls = []
    monkeypatch.setattr(v12_live.v12_option_recorder, "record_snapshot", lambda *a, **k: calls.append("v12") or {"status":"NOT_DUE"})
    monkeypatch.setattr(v12_live, "_trial25_process", lambda *a, **k: calls.append("trial25") or {"status":"STAGE_D_COLLECTING","completed":0,"target":40})
    out = v12_live.process_live_scan(
        object(), [], {"bullish": [], "bearish": []}, {"1D": {}, "2D": {}},
        **_args(tmp_path),
    )
    assert calls == ["v12", "trial25"]
    assert out["trial25_shadow"]["status"] == "STAGE_D_COLLECTING"


def test_trial25_failure_never_breaks_existing_v12_surface(monkeypatch, tmp_path):
    from app import v12_live
    monkeypatch.setattr(v12_live.v12_option_recorder, "record_snapshot", lambda *a, **k: {"status":"CAPTURED"})
    monkeypatch.setattr(v12_live, "_trial25_process", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("trial25 down")))
    out = v12_live.process_live_scan(
        object(), [], {"bullish": [], "bearish": []}, {"1D": {}, "2D": {}},
        **_args(tmp_path),
    )
    assert out["recorder"]["status"] == "CAPTURED"
    assert out["trial25_shadow"]["status"] == "ERROR"
    assert "trial25 down" in out["trial25_shadow"]["error"]


def test_trial25_receives_current_live_fno_symbols(monkeypatch, tmp_path):
    from app import v12_live
    seen = {}
    monkeypatch.setattr(v12_live.v12_option_recorder, "record_snapshot", lambda *a, **k: {"status":"NOT_DUE"})
    def fake(*a, **kw):
        seen["symbols"] = set(kw["current_fno_symbols"])
        return {"status":"PREREGISTERED_WAITING_EVENTS","completed":0,"target":40}
    monkeypatch.setattr(v12_live, "_trial25_process", fake)
    v12_live.process_live_scan(
        object(), [{"symbol":"OLD"}, {"symbol":"NEWCO"}],
        {"bullish": [], "bearish": []}, {"1D": {}, "2D": {}},
        current_fno_symbols={"OLD","NEWCO"},
        **_args(tmp_path),
    )
    assert seen["symbols"] == {"OLD","NEWCO"}


def test_background_has_no_separate_trial25_thread():
    source = (Path(__file__).parents[1]/"app"/"background.py").read_text(encoding="utf-8")
    assert "trial25-shadow-thread" not in source
    assert "Thread(target=trial25" not in source


def test_post_cas_time_is_not_a_trial25_capture_slot():
    from app import trial25_shadow
    assert trial25_shadow.capture_kind_due(dt.datetime(2026, 10, 8, 15, 37, tzinfo=IST), "2026-10-08", "2026-10-12") is None
    assert trial25_shadow.capture_kind_due(dt.datetime(2026, 10, 8, 15, 10, tzinfo=IST), "2026-10-08", "2026-10-12") == "ENTRY"
    assert trial25_shadow.capture_kind_due(dt.datetime(2026, 10, 12, 9, 30, tzinfo=IST), "2026-10-08", "2026-10-12") == "EXIT"


def test_trial25_preentry_window_forces_fresh_calendar_observation(monkeypatch, tmp_path):
    from app import v12_live
    state_file = tmp_path / "earn_state.json"
    ledger_file = tmp_path / "earn_ledger.jsonl"
    calls = []

    def fake_fetch(session, symbols, start, end, timeout=25):
        calls.append((start, end))
        return {"status": "OK", "events": [], "error": None}

    monkeypatch.setattr(v12_live.v12_earnings_calendar, "fetch_upcoming_earnings", fake_fetch)
    morning = dt.datetime(2026, 10, 8, 9, 20, tzinfo=IST)
    preentry = dt.datetime(2026, 10, 8, 15, 5, tzinfo=IST)

    v12_live.refresh_earnings_calendar(
        {"ABC"}, now=morning, state_file=state_file, ledger_file=ledger_file,
        session_factory=lambda: object(),
    )
    assert v12_live.trial25_preentry_calendar_refresh_due(preentry) is True
    v12_live.refresh_earnings_calendar(
        {"ABC"}, now=preentry, state_file=state_file, ledger_file=ledger_file,
        session_factory=lambda: object(), force=True,
    )
    assert len(calls) == 2


def test_trial25_preentry_calendar_refresh_window_is_narrow():
    from app import v12_live
    assert v12_live.trial25_preentry_calendar_refresh_due(
        dt.datetime(2026, 10, 8, 14, 59, tzinfo=IST)
    ) is False
    assert v12_live.trial25_preentry_calendar_refresh_due(
        dt.datetime(2026, 10, 8, 15, 0, tzinfo=IST)
    ) is True
    assert v12_live.trial25_preentry_calendar_refresh_due(
        dt.datetime(2026, 10, 8, 15, 17, tzinfo=IST)
    ) is True
    assert v12_live.trial25_preentry_calendar_refresh_due(
        dt.datetime(2026, 10, 8, 15, 18, tzinfo=IST)
    ) is False


def test_trial25_process_persists_live_fno_reconciliation_and_reuses_contract_map(monkeypatch, tmp_path):
    from app import v12_live
    contracts = {"OLD": [{"instrument_type": "CE"}], "NEWCO": [{"instrument_type": "PE"}]}
    seen = {}

    monkeypatch.setattr(v12_live.trial25_shadow, "load_frozen_symbols", lambda path: ({"OLD"}, "OK"))
    monkeypatch.setattr(v12_live.derivative_intelligence, "get_option_contracts_map", lambda kite: contracts)

    def fake_update(path, frozen, contracts_map, now, **kwargs):
        seen["universe_args"] = (path, set(frozen), contracts_map, now)
        return {
            "asof": now.isoformat(timespec="seconds"),
            "cohort_a_live": ["OLD"],
            "cohort_a_missing_contracts": [],
            "new_fno_onboarding": ["NEWCO"],
        }

    def fake_process(kite, **kwargs):
        seen["process_contracts"] = kwargs["contracts_map"]
        return {
            "status": "PREREGISTERED_WAITING_EVENTS",
            "completed": 0,
            "target": 40,
            "frozen_cohort_size": 1,
        }

    monkeypatch.setattr(v12_live.trial25_universe, "update_universe_state", fake_update)
    monkeypatch.setattr(v12_live.trial25_shadow, "process_due_events", fake_process)
    monkeypatch.setattr(v12_live.config, "TRIAL25_UNIVERSE_STATE_FILE", tmp_path / "universe_state.json")
    monkeypatch.setattr(v12_live.config, "TRIAL25_UNIVERSE_LEDGER_FILE", tmp_path / "universe_ledger.jsonl")

    now = dt.datetime(2026, 9, 30, 9, 20, tzinfo=IST)
    out = v12_live._trial25_process(
        object(), now=now, earnings_state={}, current_fno_symbols={"OLD", "NEWCO"}
    )
    assert seen["process_contracts"] is contracts
    assert seen["universe_args"][1] == {"OLD"}
    assert seen["universe_args"][2] is contracts
    assert out["cohort_a_live"] == 1
    assert out["new_fno_onboarding"] == 1
    assert out["cohort_a_missing_contracts"] == 0
