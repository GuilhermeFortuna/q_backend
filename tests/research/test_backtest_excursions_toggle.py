from datetime import datetime
from unittest.mock import patch

import pandas as pd
import pytest

from q_backend.research import ResearchStrategy, TradeOrder, backtest
from q_backend.research.results import TRADE_COLUMNS

EXCURSION_COLUMNS = [
    "mfe_points",
    "mae_points",
    "mfe_value",
    "mae_value",
    "mfe_time",
    "mae_time",
    "mfe_capture",
    "post_exit_mfe_points",
    "post_exit_mae_points",
    "post_exit_return_points",
    "post_exit_bars_observed",
    "exit_bar_rest_high",
    "exit_bar_rest_low",
    "excursion_resolution",
]


class OneTrade(ResearchStrategy):
    def entry_strategy(self, frame):
        return TradeOrder.buy() if len(frame) == 2 else None

    def exit_strategy(self, frame):
        return TradeOrder.close() if len(frame) == 5 else None


@pytest.fixture
def bars():
    index = pd.date_range("2026-10-01 09:00", periods=8, freq="min", tz="America/Sao_Paulo", name="time")
    frame = pd.DataFrame(
        {
            "open": [100, 101, 102, 104, 103, 105, 106, 107],
            "high": [102, 103, 105, 106, 105, 107, 108, 109],
            "low": [99, 100, 101, 102, 101, 104, 105, 106],
            "close": [101, 102, 104, 103, 104, 106, 107, 108],
            "tick_volume": 1,
        },
        index=index,
    )
    frame.attrs["q_research"] = {"timeframe": "M1"}
    return frame


def test_excursions_false_skips_measurement_and_preserves_trade_results(bars):
    with patch("q_backend.backtesting.excursions.measure_trades", wraps=None) as measure:
        without = backtest(bars, strategy=OneTrade(), symbol="TEST", excursions=False)
    assert measure.call_count == 0
    with_excursions = backtest(bars, strategy=OneTrade(), symbol="TEST", excursions=True)

    pd.testing.assert_frame_equal(
        without.trades.drop(columns=[*EXCURSION_COLUMNS, "trade_id"]),
        with_excursions.trades.drop(columns=[*EXCURSION_COLUMNS, "trade_id"]),
    )
    pd.testing.assert_frame_equal(without.equity, with_excursions.equity)
    pd.testing.assert_frame_equal(without.data, with_excursions.data)
    assert without.metrics == with_excursions.metrics
    assert without.rejected_entries.equals(with_excursions.rejected_entries)
    assert without.config["excursions"] is False
    assert all(without.trades[column].isna().all() for column in EXCURSION_COLUMNS)
    assert without.trades[EXCURSION_COLUMNS].dtypes.astype(str).tolist() == [
        "float64",
        "float64",
        "float64",
        "float64",
        "datetime64[ns, America/Sao_Paulo]",
        "datetime64[ns, America/Sao_Paulo]",
        "float64",
        "float64",
        "float64",
        "float64",
        "Int64",
        "float64",
        "float64",
        "object",
    ]
    assert len(without._closed_trades) == 1
    assert without._closed_trades[0].mfe_points is None


@pytest.mark.parametrize("value", [0, 1, "no", None])
def test_excursions_requires_bool(bars, value):
    with pytest.raises(ValueError, match="excursions"):
        backtest(bars, strategy=OneTrade(), symbol="TEST", excursions=value)


def test_post_exit_bars_is_validated_even_when_excursions_are_disabled(bars):
    with pytest.raises(ValueError, match="post_exit_bars"):
        backtest(bars, strategy=OneTrade(), symbol="TEST", excursions=False, post_exit_bars=0)
