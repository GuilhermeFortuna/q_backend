"""Unit coverage for the q_core candle-kernel bridge."""

from __future__ import annotations

import types

import numpy as np
import pandas as pd
import pytest

from q_backend.backtesting.position_sizing import (
    FixedQuantitySizer,
    FixedSafetyMarginSizer,
    InverseVolatilitySizer,
)


def test_sizer_to_kernel_maps_supported_sizers_and_rejects_subclasses() -> None:
    from q_backend.backtesting.candle_kernel import sizer_to_kernel

    fixed = sizer_to_kernel(FixedQuantitySizer(3, scale_by_signal_strength=True))
    assert fixed.mapping == {"type": "fixed_quantity", "quantity": 3, "scale_by_signal_strength": True}
    assert fixed.point_value == 1.0
    assert not fixed.needs_volatility

    margin = sizer_to_kernel(FixedSafetyMarginSizer(5000, max_contracts=7, min_contracts=2))
    assert margin.mapping == {
        "type": "fixed_safety_margin",
        "safety_margin_per_contract": 5000,
        "max_contracts": 7,
        "min_contracts": 2,
        "scale_by_signal_strength": False,
    }

    inverse = sizer_to_kernel(InverseVolatilitySizer(10, point_value=0.2, max_contracts=9, min_contracts=1))
    assert inverse.mapping["type"] == "inverse_volatility"
    assert inverse.point_value == 0.2
    assert inverse.needs_volatility

    class CustomSizer(FixedQuantitySizer):
        pass

    with pytest.raises(TypeError, match="CustomSizer"):
        sizer_to_kernel(CustomSizer())


def test_parse_day_trade_times_preserves_existing_error_message() -> None:
    from q_backend.backtesting.candle_kernel import parse_day_trade_times

    assert parse_day_trade_times("9:0", "16:00", "17:00") == (
        9 * 60 * 60 * 1_000_000,
        16 * 60 * 60 * 1_000_000,
        17 * 60 * 60 * 1_000_000,
    )
    with pytest.raises(ValueError, match=r"Invalid day trade time config \(start=09-00, end=16:00, close=17:00\)"):
        parse_day_trade_times("09-00", "16:00", "17:00")


def test_wall_clock_us_preserves_index_wall_clock_and_floors_pre_epoch() -> None:
    from q_backend.backtesting.candle_kernel import wall_clock_us

    utc = pd.date_range("2024-01-01T12:00:00Z", periods=2, freq="h")
    sao_paulo = utc.tz_convert("America/Sao_Paulo")
    assert np.array_equal(wall_clock_us(utc) - wall_clock_us(sao_paulo), np.full(2, 3 * 60 * 60 * 1_000_000))
    naive = utc.tz_localize(None)
    assert np.array_equal(wall_clock_us(naive), naive.as_unit("ns").asi8 // 1_000)
    assert wall_clock_us(pd.DatetimeIndex([pd.Timestamp("1969-12-31 23:59:59.999999999")])).item() == -1


def test_check_engine_names_missing_function_and_version() -> None:
    from q_backend.backtesting.candle_kernel import REQUIRED_ENGINE_FUNCTIONS, check_engine

    values = {name: object() for name in REQUIRED_ENGINE_FUNCTIONS if name != "DecisionStep"}
    values["version"] = lambda: "0.0.0"
    with pytest.raises(ImportError) as exc_info:
        check_engine(types.SimpleNamespace(**values))
    assert "DecisionStep" in str(exc_info.value)
    assert "0.0.0" in str(exc_info.value)
