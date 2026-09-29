import datetime as dt
import gzip
import json

import numpy as np

from app import v123_quant_shadow
from app.v123_quant_regime import GaussianRegimeHMM, model_snapshot, posterior_lifecycle_step


def _payload(price=101.0, nifty=25000.0, symbol="ABC", participation=1.2):
    return {
        "status": "STREAMING",
        "nifty": {"live_price": nifty},
        "quant_rows": [{
            "symbol": symbol,
            "live_price": price,
            "volume_rate_accel": participation,
            "sector": "TEST",
        }],
    }


def _write_training_session(root, day, shift):
    path = root / f"features_{day.isoformat()}.jsonl.gz"
    with gzip.open(path, "wt", encoding="utf-8") as fh:
        for symbol, sign in (("UP", 1.0), ("DOWN", -1.0), ("FLAT", 0.0)):
            for i in range(24):
                x = [
                    sign * (1.5 + 0.01 * i) + shift,
                    sign * 1.3,
                    sign * 0.1,
                    0.2,
                ]
                fh.write(json.dumps({
                    "ts": f"{day.isoformat()}T10:{i:02d}:00",
                    "trade_date": day.isoformat(),
                    "symbol": symbol,
                    "price": 100 + i,
                    "market_return": 0.0,
                    "sector_return": None,
                    "participation": 1.0,
                    "x": x,
                }) + "\n")


def test_shadow_collects_whole_universe_features_without_model(tmp_path):
    recorder = v123_quant_shadow.QuantRegimeShadowRecorder(
        tmp_path, sample_seconds=60, min_training_sessions=2
    )
    now = dt.datetime(2026, 9, 23, 10, 0)
    status = recorder.process(_payload(), now=now)
    assert status["status"] == "COLLECTING_BASELINE"
    assert status["production_controls"] is False
    assert status["tracked_symbols"] == 1
    assert status["actionable_count"] == 0
    assert (tmp_path / "features_2026-09-23.jsonl.gz").exists()


def test_shadow_fits_only_prior_sessions_and_never_current_day(tmp_path):
    _write_training_session(tmp_path, dt.date(2026, 9, 23), 0.00)
    _write_training_session(tmp_path, dt.date(2026, 9, 24), 0.02)

    recorder = v123_quant_shadow.QuantRegimeShadowRecorder(
        tmp_path, sample_seconds=60, min_training_sessions=2
    )
    now = dt.datetime(2026, 9, 25, 10, 0)
    status = recorder.process(_payload(), now=now)

    assert status["status"] == "ACTIVE_SHADOW"
    assert status["training_sessions"] == ["2026-09-23", "2026-09-24"]
    model = json.loads((tmp_path / "model_for_2026-09-25.json").read_text())
    assert model["fit_for_trade_date"] == "2026-09-25"
    assert model["production_controls"] is False
    assert "2026-09-25" not in model["training_sessions"]


def test_shadow_ignores_weekends_and_outside_session(tmp_path):
    recorder = v123_quant_shadow.QuantRegimeShadowRecorder(tmp_path)
    sunday = dt.datetime(2026, 9, 27, 10, 0)
    status = recorder.process(_payload(), now=sunday)
    assert status["trade_date"] is None
    assert not list(tmp_path.glob("features_*.jsonl.gz"))

    monday_early = dt.datetime(2026, 9, 28, 8, 30)
    recorder.process(_payload(), now=monday_early)
    assert not list(tmp_path.glob("features_*.jsonl.gz"))


def test_shadow_restart_replays_same_day_feature_history(tmp_path):
    recorder = v123_quant_shadow.QuantRegimeShadowRecorder(
        tmp_path, sample_seconds=60, min_training_sessions=2
    )
    t0 = dt.datetime(2026, 9, 23, 10, 0)
    recorder.process(_payload(price=100.0), now=t0)
    recorder.process(_payload(price=100.3, nifty=25010.0), now=t0 + dt.timedelta(minutes=1))

    restarted = v123_quant_shadow.QuantRegimeShadowRecorder(
        tmp_path, sample_seconds=60, min_training_sessions=2
    )
    restarted.process(_payload(price=100.6, nifty=25020.0), now=t0 + dt.timedelta(minutes=2))
    assert "ABC" in restarted._features
    assert restarted._features["ABC"].last_price == 100.6


def test_posterior_lifecycle_is_timer_free_and_probability_driven():
    state = posterior_lifecycle_step(
        "CLOSED", {"DOWN": 0.05, "FLAT": 0.15, "UP": 0.80}
    )
    assert state == "ACTIONABLE_UP"
    for _ in range(10000):
        state = posterior_lifecycle_step(
            state, {"DOWN": 0.04, "FLAT": 0.12, "UP": 0.84}
        )
    assert state == "ACTIONABLE_UP"

    state = posterior_lifecycle_step(
        state, {"DOWN": 0.10, "FLAT": 0.70, "UP": 0.20}
    )
    assert state == "DECAYING_UP"


def test_fit_sequences_does_not_create_cross_symbol_transition():
    up = np.tile(np.array([[2.0, 1.8, 0.1, 0.2]]), (30, 1))
    down = np.tile(np.array([[-2.0, -1.8, -0.1, 0.2]]), (30, 1))
    flat = np.tile(np.array([[0.0, 0.0, 0.0, 0.0]]), (30, 1))
    model = GaussianRegimeHMM(max_iter=20).fit_sequences([up, down, flat])
    snap = model_snapshot(model)
    assert snap["production_controls"] is False
    assert model.transition.shape == (3, 3)
    assert np.allclose(model.transition.sum(axis=1), 1.0)


def test_shadow_artifact_manifest_is_read_only_and_traversal_safe(tmp_path):
    (tmp_path / "shadow_state.json").write_text("{}", encoding="utf-8")
    (tmp_path / "secret.env").write_text("no", encoding="utf-8")
    rows = v123_quant_shadow.list_shadow_artifacts(tmp_path)
    assert [row["name"] for row in rows] == ["shadow_state.json"]
    assert v123_quant_shadow.resolve_shadow_artifact(tmp_path, "shadow_state.json") is not None
    assert v123_quant_shadow.resolve_shadow_artifact(tmp_path, "../secret.env") is None
    assert v123_quant_shadow.resolve_shadow_artifact(tmp_path, "secret.env") is None


def test_shadow_uses_same_sector_return_for_all_symbols_in_sector(tmp_path):
    recorder = v123_quant_shadow.QuantRegimeShadowRecorder(
        tmp_path, sample_seconds=60, min_training_sessions=2
    )
    t0 = dt.datetime(2026, 9, 29, 10, 0)
    first = {
        "status": "STREAMING",
        "nifty": {"live_price": 22500.0},
        "sector_contexts": {"NIFTY AUTO": {"live_price": 25300.0}},
        "quant_rows": [
            {"symbol": "HEROMOTOCO", "live_price": 540.0, "volume_rate_accel": 1.1, "sector_index": "NIFTY AUTO"},
            {"symbol": "MARUTI", "live_price": 16000.0, "volume_rate_accel": 1.2, "sector_index": "NIFTY AUTO"},
        ],
    }
    second = {
        "status": "STREAMING",
        "nifty": {"live_price": 22450.0},
        "sector_contexts": {"NIFTY AUTO": {"live_price": 25100.0}},
        "quant_rows": [
            {"symbol": "HEROMOTOCO", "live_price": 541.0, "volume_rate_accel": 1.3, "sector_index": "NIFTY AUTO"},
            {"symbol": "MARUTI", "live_price": 15980.0, "volume_rate_accel": 1.1, "sector_index": "NIFTY AUTO"},
        ],
    }

    recorder.process(first, now=t0)
    status = recorder.process(second, now=t0 + dt.timedelta(minutes=1))
    assert status["sector_factor_status"] == "ACTIVE"
    assert status["sector_factor_coverage"] == 1.0

    with gzip.open(tmp_path / "features_2026-09-29.jsonl.gz", "rt", encoding="utf-8") as fh:
        rows = [json.loads(line) for line in fh]
    latest = [row for row in rows if row["ts"].startswith("2026-09-29T10:01")]
    assert len(latest) == 2
    assert latest[0]["sector_return"] < 0
    assert latest[1]["sector_return"] == latest[0]["sector_return"]
    assert latest[0]["sector_price"] == 25100.0
    assert latest[1]["sector_price"] == 25100.0


def test_shadow_restart_restores_sector_price_for_next_return(tmp_path):
    recorder = v123_quant_shadow.QuantRegimeShadowRecorder(
        tmp_path, sample_seconds=60, min_training_sessions=2
    )
    t0 = dt.datetime(2026, 9, 29, 10, 0)
    payload = {
        "status": "STREAMING",
        "nifty": {"live_price": 22500.0},
        "sector_contexts": {"NIFTY AUTO": {"live_price": 25300.0}},
        "quant_rows": [{
            "symbol": "HEROMOTOCO",
            "live_price": 540.0,
            "volume_rate_accel": 1.1,
            "sector_index": "NIFTY AUTO",
        }],
    }
    recorder.process(payload, now=t0)

    restarted = v123_quant_shadow.QuantRegimeShadowRecorder(
        tmp_path, sample_seconds=60, min_training_sessions=2
    )
    payload["nifty"]["live_price"] = 22480.0
    payload["sector_contexts"]["NIFTY AUTO"]["live_price"] = 25200.0
    payload["quant_rows"][0]["live_price"] = 541.0
    restarted.process(payload, now=t0 + dt.timedelta(minutes=1))

    with gzip.open(tmp_path / "features_2026-09-29.jsonl.gz", "rt", encoding="utf-8") as fh:
        rows = [json.loads(line) for line in fh]
    assert rows[-1]["sector_return"] < 0
