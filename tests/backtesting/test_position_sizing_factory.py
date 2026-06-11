import pytest
from pydantic import ValidationError

from q_backend.backtesting.position_sizing import (
    FixedQuantityPositionSizing,
    FixedSafetyMarginPositionSizing,
    InverseVolatilityPositionSizing,
    FixedQuantitySizer,
    FixedSafetyMarginSizer,
    InverseVolatilitySizer,
    build_position_sizer,
)


def test_build_position_sizer_defaults_when_none():
    sizer = build_position_sizer(None)

    assert isinstance(sizer, FixedQuantitySizer)
    assert sizer.quantity == 1.0


def test_build_position_sizer_fixed_quantity():
    config = FixedQuantityPositionSizing(type="fixed_quantity", quantity=3.0)
    sizer = build_position_sizer(config)

    assert isinstance(sizer, FixedQuantitySizer)
    assert sizer.quantity == 3.0


def test_build_position_sizer_fixed_safety_margin():
    config = FixedSafetyMarginPositionSizing(
        type="fixed_safety_margin",
        safety_margin_per_contract=5000.0,
        min_contracts=1,
        max_contracts=10,
    )
    sizer = build_position_sizer(config)

    assert isinstance(sizer, FixedSafetyMarginSizer)
    assert sizer.safety_margin_per_contract == 5000.0
    assert sizer.min_contracts == 1
    assert sizer.max_contracts == 10


def test_build_position_sizer_fixed_safety_margin_null_max_contracts():
    config = FixedSafetyMarginPositionSizing(
        type="fixed_safety_margin",
        safety_margin_per_contract=5000.0,
        min_contracts=1,
        max_contracts=None,
    )
    sizer = build_position_sizer(config)

    assert isinstance(sizer, FixedSafetyMarginSizer)
    assert sizer.max_contracts is None


def test_build_position_sizer_inverse_volatility():
    config = InverseVolatilityPositionSizing(
        type="inverse_volatility",
        target_volatility_pct=12.5,
        min_contracts=1,
        max_contracts=50,
    )
    sizer = build_position_sizer(config, point_value=0.2)

    assert isinstance(sizer, InverseVolatilitySizer)
    assert sizer.target_volatility_pct == 12.5
    assert sizer.point_value == 0.2
    assert sizer.min_contracts == 1
    assert sizer.max_contracts == 50


@pytest.mark.parametrize("quantity", [0, -1.0])
def test_fixed_quantity_model_rejects_invalid_quantity(quantity):
    with pytest.raises(ValidationError):
        FixedQuantityPositionSizing(type="fixed_quantity", quantity=quantity)


@pytest.mark.parametrize("margin", [0, -100.0])
def test_fixed_safety_margin_model_rejects_invalid_margin(margin):
    with pytest.raises(ValidationError):
        FixedSafetyMarginPositionSizing(
            type="fixed_safety_margin",
            safety_margin_per_contract=margin,
        )


def test_fixed_safety_margin_model_rejects_max_less_than_min():
    with pytest.raises(ValidationError):
        FixedSafetyMarginPositionSizing(
            type="fixed_safety_margin",
            safety_margin_per_contract=5000.0,
            min_contracts=5,
            max_contracts=2,
        )
