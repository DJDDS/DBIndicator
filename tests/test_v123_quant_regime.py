import numpy as np

from app.v123_quant_regime import (
    GaussianRegimeHMM,
    QuantFeatureState,
    lifecycle_sequence,
    lifecycle_step,
    model_snapshot,
)


def _synthetic_regimes():
    # Deterministic synthetic observations with three clearly separated
    # underlying regimes.  No random seed/noise is needed for the contract test.
    down = np.array([
        [-2.0, -1.8, -0.4, 0.3],
        [-1.9, -1.7, -0.2, 0.2],
        [-1.8, -1.9, 0.0, 0.4],
        [-2.1, -1.8, 0.1, 0.3],
        [-1.7, -1.6, 0.2, 0.2],
        [-1.9, -1.8, -0.1, 0.3],
    ])
    flat = np.array([
        [-0.1, 0.0, 0.0, -0.1],
        [0.0, 0.1, -0.1, 0.0],
        [0.1, -0.1, 0.1, 0.1],
        [0.0, 0.0, 0.0, 0.0],
        [-0.1, 0.1, 0.0, -0.1],
        [0.1, 0.0, -0.1, 0.0],
    ])
    up = np.array([
        [1.8, 1.7, 0.2, 0.3],
        [1.9, 1.8, 0.1, 0.2],
        [2.0, 1.9, 0.0, 0.4],
        [1.7, 1.6, -0.1, 0.3],
        [2.1, 1.8, 0.2, 0.2],
        [1.9, 1.7, 0.0, 0.3],
    ])
    # Repeat regimes to give transition learning real persistence.
    return np.vstack([flat, up, up, flat, down, down, flat, up, up])


def test_hmm_learns_ordered_down_flat_up_states_and_serializes():
    x = _synthetic_regimes()
    model = GaussianRegimeHMM(max_iter=60).fit(x)
    assert model.means.shape == (3, 4)
    assert model.transition.shape == (3, 3)

    centres = model.means[:, 0] + model.means[:, 1]
    assert centres[0] < centres[1] < centres[2]

    snap = model_snapshot(model)
    assert snap["state_names"] == ["DOWN", "FLAT", "UP"]
    assert snap["production_controls"] is False
    assert set(snap["expected_dwell_observations"]) == {"DOWN", "FLAT", "UP"}


def test_filter_is_causal_and_keeps_persistent_up_regime():
    model = GaussianRegimeHMM(max_iter=60).fit(_synthetic_regimes())
    up = np.tile(np.array([[2.0, 1.8, 0.0, 0.3]]), (40, 1))
    filtered = model.filter(up)
    life = lifecycle_sequence(filtered)

    # Once BUILDING_UP transitions to ACTIONABLE_UP, repeated UP observations
    # do not expire merely because more time/observations pass.
    actionable_indices = [
        i for i, row in enumerate(life) if row["lifecycle"] == "ACTIONABLE_UP"
    ]
    assert actionable_indices
    first = actionable_indices[0]
    assert all(
        row["lifecycle"] == "ACTIONABLE_UP"
        for row in life[first:]
    )


def test_lifecycle_has_no_timer_and_can_remain_actionable_indefinitely():
    state = "CLOSED"
    state = lifecycle_step(state, "UP")
    assert state == "BUILDING_UP"
    state = lifecycle_step(state, "UP")
    assert state == "ACTIONABLE_UP"

    # The lifecycle sees only mathematical regime states; elapsed time is not
    # an argument.  Thousands of continued UP observations remain actionable.
    for _ in range(5000):
        state = lifecycle_step(state, "UP")
    assert state == "ACTIONABLE_UP"


def test_flat_means_decay_and_opposite_regime_rebuilds_other_direction():
    state = "ACTIONABLE_UP"
    state = lifecycle_step(state, "FLAT")
    assert state == "DECAYING_UP"
    state = lifecycle_step(state, "FLAT")
    assert state == "CLOSED"

    state = lifecycle_step("ACTIONABLE_UP", "DOWN")
    assert state == "BUILDING_DOWN"
    state = lifecycle_step(state, "DOWN")
    assert state == "ACTIONABLE_DOWN"


def test_quant_features_are_underlying_only_and_causal():
    q = QuantFeatureState()
    rows = []
    price = 100.0
    for i in range(30):
        price *= 1.0008
        rows.append(q.update(
            price=price,
            market_return=0.0001,
            sector_return=0.0002,
            participation=1.0 + i / 20.0,
        ))

    x = np.vstack(rows)
    assert x.shape == (30, 4)
    assert np.isfinite(x).all()

    # Positive stock-specific drift should eventually be represented as
    # positive directional coordinates without using any future outcome.
    assert np.median(x[-10:, 0]) > 0
    assert np.median(x[-10:, 1]) > 0


def test_sector_input_can_be_missing_without_future_substitution():
    q = QuantFeatureState()
    first = q.update(price=100.0, market_return=0.0, sector_return=None, participation=1.0)
    second = q.update(price=100.2, market_return=0.0005, sector_return=None, participation=1.2)
    assert first.shape == (4,)
    assert second.shape == (4,)
    assert np.isfinite(second).all()
