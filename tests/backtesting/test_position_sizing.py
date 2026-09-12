import pytest

from q_backend.backtesting.models import Signal, SignalAction, OrderAction, OrderType
from q_backend.backtesting.position_sizing import (
    FixedQuantitySizer,
    FixedSafetyMarginSizer,
)

SYMBOL = "ES"
PRICE = 100.0


def test_hold_returns_none():
    sizer = FixedSafetyMarginSizer(safety_margin_per_contract=5_000)
    signal = Signal(symbol=SYMBOL, action=SignalAction.HOLD)

    assert sizer.size_signal(signal, PRICE, 15_000) is None


def test_close_returns_none():
    sizer = FixedSafetyMarginSizer(safety_margin_per_contract=5_000)
    signal = Signal(symbol=SYMBOL, action=SignalAction.CLOSE)

    assert sizer.size_signal(signal, PRICE, 15_000) is None


def test_capital_below_margin_uses_min_contracts():
    sizer = FixedSafetyMarginSizer(safety_margin_per_contract=5_000, min_contracts=1)
    signal = Signal(symbol=SYMBOL, action=SignalAction.BUY)

    order = sizer.size_signal(signal, PRICE, 4_000)

    assert order is not None
    assert order.quantity == 1.0


def test_capital_below_margin_returns_none_when_above_zero_but_below_min():
    sizer = FixedSafetyMarginSizer(safety_margin_per_contract=5_000, min_contracts=5)
    signal = Signal(symbol=SYMBOL, action=SignalAction.BUY)

    # floor(7500/5000)=1 — can afford one margin unit but not min_contracts
    assert sizer.size_signal(signal, PRICE, 7_500) is None


def test_capital_below_margin_returns_none_when_min_contracts_zero():
    sizer = FixedSafetyMarginSizer(safety_margin_per_contract=5_000, min_contracts=0)
    signal = Signal(symbol=SYMBOL, action=SignalAction.BUY)

    assert sizer.size_signal(signal, PRICE, 4_000) is None


def test_standard_sizing():
    sizer = FixedSafetyMarginSizer(safety_margin_per_contract=5_000)
    signal = Signal(symbol=SYMBOL, action=SignalAction.BUY)

    order = sizer.size_signal(signal, PRICE, 15_000)

    assert order is not None
    assert order.quantity == 3.0


def test_max_contracts_caps_quantity():
    sizer = FixedSafetyMarginSizer(safety_margin_per_contract=5_000, max_contracts=2)
    signal = Signal(symbol=SYMBOL, action=SignalAction.BUY)

    order = sizer.size_signal(signal, PRICE, 15_000)

    assert order is not None
    assert order.quantity == 2.0


def test_buy_signal_creates_buy_market_order():
    sizer = FixedSafetyMarginSizer(safety_margin_per_contract=5_000)
    signal = Signal(symbol=SYMBOL, action=SignalAction.BUY)

    order = sizer.size_signal(signal, PRICE, 15_000)

    assert order is not None
    assert order.symbol == SYMBOL
    assert order.action == OrderAction.BUY
    assert order.order_type == OrderType.MARKET


def test_sell_signal_creates_sell_market_order():
    sizer = FixedSafetyMarginSizer(safety_margin_per_contract=5_000)
    signal = Signal(symbol=SYMBOL, action=SignalAction.SELL)

    order = sizer.size_signal(signal, PRICE, 15_000)

    assert order is not None
    assert order.symbol == SYMBOL
    assert order.action == OrderAction.SELL
    assert order.order_type == OrderType.MARKET


@pytest.mark.parametrize("invalid_margin", [0, -1.0])
def test_invalid_safety_margin_raises_value_error(invalid_margin):
    with pytest.raises(ValueError):
        FixedSafetyMarginSizer(safety_margin_per_contract=invalid_margin)


@pytest.mark.parametrize("quantity", [1.0, 2.0, 5.0])
def test_fixed_quantity_max_position_size_is_the_quantity(quantity):
    sizer = FixedQuantitySizer(quantity=quantity)
    # Capital/price are irrelevant for a fixed-quantity cap.
    assert sizer.max_position_size(PRICE, 999_999) == quantity


def test_safety_margin_max_position_size_matches_sized_quantity():
    sizer = FixedSafetyMarginSizer(safety_margin_per_contract=5_000)
    signal = Signal(symbol=SYMBOL, action=SignalAction.BUY)

    order = sizer.size_signal(signal, PRICE, 15_000)
    assert order is not None
    # The cap equals the quantity the model would size to for the same capital.
    assert sizer.max_position_size(PRICE, 15_000) == order.quantity


def test_safety_margin_max_position_size_respects_max_contracts():
    sizer = FixedSafetyMarginSizer(safety_margin_per_contract=5_000, max_contracts=2)
    # floor(15000/5000)=3, capped at 2.
    assert sizer.max_position_size(PRICE, 15_000) == 2.0


def test_sizers_always_yield_integer_quantities():
    import pandas as pd
    from q_backend.backtesting.position_sizing import InverseVolatilitySizer

    # 1. Fixed Quantity Sizer with float quantity
    fq_sizer = FixedQuantitySizer(quantity=2.7)
    sig = Signal(symbol=SYMBOL, action=SignalAction.BUY)
    order = fq_sizer.size_signal(sig, PRICE, 10000)
    assert order is not None
    assert order.quantity == 2.0
    assert isinstance(order.quantity, float)
    assert order.quantity.is_integer()

    # 2. Fixed Quantity Sizer with signal strength scaling
    fq_sizer_scale = FixedQuantitySizer(quantity=10.0, scale_by_signal_strength=True)
    sig_with_strength = Signal(symbol=SYMBOL, action=SignalAction.BUY, strength=0.75)
    order_scale = fq_sizer_scale.size_signal(sig_with_strength, PRICE, 10000)
    assert order_scale is not None
    # 10.0 * 0.75 = 7.5, floored to 7.0
    assert order_scale.quantity == 7.0
    assert order_scale.quantity.is_integer()

    # 3. Fixed Safety Margin Sizer
    fsm_sizer = FixedSafetyMarginSizer(safety_margin_per_contract=3000.0)
    order_fsm = fsm_sizer.size_signal(sig, PRICE, 8000.0)  # 8000 / 3000 = 2.666...
    assert order_fsm is not None
    assert order_fsm.quantity == 2.0
    assert order_fsm.quantity.is_integer()

    # 4. Inverse Volatility Sizer
    iv_sizer = InverseVolatilitySizer(target_volatility_pct=10.0, point_value=1.0)
    # Target notional = 10% * 100,000 = 10,000
    # Let vol = 0.15, price = 100.0. Notional per contract = 0.15 * 100 * 1 = 15
    # Contracts = 10,000 / 15 = 666.666...
    data = pd.Series({"volatility": 0.15})
    order_iv = iv_sizer.size_signal(sig, PRICE, 100000.0, current_data=data)
    assert order_iv is not None
    assert order_iv.quantity == 666.0
    assert order_iv.quantity.is_integer()
