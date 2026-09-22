import pytest

from app.index_option_fees import FEE_MODEL_VERSION, calculate_option_charges


def test_index_option_fee_model_is_independent_and_versioned():
    out = calculate_option_charges([
        {"side": "BUY", "price": 100.0, "quantity": 75},
        {"side": "SELL", "price": 120.0, "quantity": 75},
    ])
    assert out["model_version"] == FEE_MODEL_VERSION
    assert FEE_MODEL_VERSION.startswith("INDEX_")
    assert out["brokerage"] == 40.0
    assert out["stt"] == 14.0
    assert out["total"] > out["brokerage"] + out["stt"]


def test_index_option_fee_model_rejects_invalid_fills():
    with pytest.raises(ValueError):
        calculate_option_charges([{"side": "BUY", "price": 0.0, "quantity": 75}])
