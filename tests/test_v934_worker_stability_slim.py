from pathlib import Path

import pytest

from app import backtest

ROOT = Path(__file__).resolve().parents[1]


def test_v934_v93_daily_oi_is_fetched_per_symbol_not_as_full_universe_sweep():
    text = (ROOT / 'app/backtest.py').read_text(encoding='utf-8')
    assert 'fetch_oi_history(\n                kite, symbols, timeframe="day"' not in text
    assert 'kite, [symbol], timeframe="day"' in text


def test_v934_live_scanner_yields_while_research_job_owns_heavy_runtime():
    text = (ROOT / 'app/background.py').read_text(encoding='utf-8')
    assert 'research_runtime.is_research_active()' in text
    assert 'research_runtime.live_scan_slot()' in text


def test_v934_research_job_uses_exclusive_runtime_and_worker_telemetry():
    text = (ROOT / 'app/backtest.py').read_text(encoding='utf-8')
    assert 'research_runtime.begin_research(' in text
    assert 'research_runtime.research_slot()' in text
    assert 'research_runtime.end_research()' in text
    assert 'research_runtime.snapshot()' in text


def test_v934_restart_message_only_promises_resume_when_checkpoint_exists(tmp_path, monkeypatch):
    state_path = tmp_path / 'state.json'
    run_dir = tmp_path / 'run'
    state_path.write_text(
        '{"status":"running","progress":{"done":5,"total":10},"params":{"resume_run_dir":"%s"}}'
        % str(run_dir).replace('\\', '\\\\'),
        encoding='utf-8',
    )
    monkeypatch.setattr(backtest, '_EARLY_RESEARCH_STATE_PATH', state_path)

    state = backtest._load_early_research_state()
    assert state['status'] == 'error'
    assert 'no durable checkpoint' in state['error'].lower()
    assert 'resume' not in state['error'].lower()

    run_dir.mkdir()
    (run_dir / '0000-AAA.pkl').write_bytes(b'not-a-real-shard')
    # A ranked checkpoint is a sufficient durable resume marker and does not
    # require deserialising the deliberately fake symbol shard above.
    (run_dir / 'v91-ranked-events.pkl').write_bytes(b'x')
    state_path.write_text(
        '{"status":"running","progress":{"done":5,"total":10},"params":{"resume_run_dir":"%s"}}'
        % str(run_dir).replace('\\', '\\\\'),
        encoding='utf-8',
    )
    state = backtest._load_early_research_state()
    assert 'resume' in state['error'].lower()
    assert 'checkpoint' in state['error'].lower()


def test_v934_journal_is_removed_from_public_and_background_surfaces():
    web = (ROOT / 'app/web.py').read_text(encoding='utf-8')
    background = (ROOT / 'app/background.py').read_text(encoding='utf-8')
    index = (ROOT / 'app/templates/index.html').read_text(encoding='utf-8')
    settings = (ROOT / 'app/templates/settings.html').read_text(encoding='utf-8')

    assert 'journal,' not in web.split('\n', 20)[0:20].__str__()
    assert '@app.route("/journal")' not in web
    assert '/api/journal/log' not in web
    assert '/journal/export.csv' not in web
    assert 'journal.resolve_open_trades' not in background
    assert 'journal_confidence' not in index
    assert 'journal-log-link' not in index
    assert 'href="/journal"' not in index
    assert 'Signal Journal' not in settings


def test_v934_research_root_is_configurable_for_durable_volume():
    text = (ROOT / 'app/backtest.py').read_text(encoding='utf-8')
    env = (ROOT / '.env.example').read_text(encoding='utf-8')
    assert 'RESEARCH_STATE_DIR' in text
    assert 'RESEARCH_STATE_DIR' in env


def test_v934_research_api_surface_is_single_and_slim():
    web = (ROOT / 'app/web.py').read_text(encoding='utf-8')
    backtest_html = (ROOT / 'app/templates/backtest.html').read_text(encoding='utf-8')
    assert web.count('@app.route("/api/early-research/start"') == 1
    assert web.count('@app.route("/api/early-research/status"') == 1
    assert '/api/ablation/start' not in web
    assert '/api/ablation/status' not in web
    assert '/api/backtest/start' not in web
    assert '/api/backtest/status' not in web
    assert 'Custom backtest' not in backtest_html
    assert 'id="bt-form"' not in backtest_html


def test_v934_live_slot_is_released_before_scan_interval_wait():
    text = (ROOT / 'app/background.py').read_text(encoding='utf-8')
    release = text.index('research_runtime.exit_live_scan()')
    wait = text.index('_rescan_event.wait(timeout=wait_seconds)', release)
    assert release < wait


def test_v934_research_runtime_prioritizes_research_over_new_live_scans():
    import threading
    import time
    from app import research_runtime

    # Start from a known idle state even if another test touched this singleton.
    research_runtime.end_research()
    research_runtime.exit_live_scan()
    assert research_runtime.live_scan_slot() is True

    research_runtime.begin_research('v93_lab')
    # Once research is requested, another live scan must not enter even though
    # the existing live scan still owns the heavy lock.
    assert research_runtime.live_scan_slot() is False

    acquired = threading.Event()

    def research_job():
        with research_runtime.research_slot():
            acquired.set()

    thread = threading.Thread(target=research_job)
    thread.start()
    time.sleep(0.02)
    assert not acquired.is_set()

    research_runtime.exit_live_scan()
    assert acquired.wait(1.0)
    thread.join(timeout=1.0)
    research_runtime.end_research()


def test_v934_oi_acceleration_alerts_are_removed_but_feature_data_remains():
    alerts_py = (ROOT / 'app/alerts.py').read_text(encoding='utf-8')
    background = (ROOT / 'app/background.py').read_text(encoding='utf-8')
    web = (ROOT / 'app/web.py').read_text(encoding='utf-8')
    index = (ROOT / 'app/templates/index.html').read_text(encoding='utf-8')
    oi_page = (ROOT / 'app/templates/oi_screener.html').read_text(encoding='utf-8')
    oi_view = (ROOT / 'app/oi_view.py').read_text(encoding='utf-8')

    # Keep OI acceleration as research/screener evidence, but never alert on it.
    assert 'oi_acceleration' in oi_view
    assert 'def process_oi_events' not in alerts_py
    assert 'def get_recent_oi' not in alerts_py
    assert '_recent_oi' not in alerts_py
    assert 'alerts.process_oi_events' not in background
    assert '/api/alerts/oi_recent' not in web
    assert '/api/alerts/oi_recent' not in index
    assert '/api/alerts/oi_recent' not in oi_page
    assert 'Active Alerts' not in oi_page


def test_v934_research_priority_flag_is_set_before_worker_thread_starts():
    text = (ROOT / 'app/backtest.py').read_text(encoding='utf-8')
    anchor = text.index('def start_early_movement_research(')
    tail = text[anchor:text.index('# --------------------------------------------------------------------------', anchor)]
    begin = tail.rindex('research_runtime.begin_research(')
    start = tail.rindex('threading.Thread(target=_job, daemon=True).start()')
    assert begin < start
