from datetime import datetime

import pandas as pd
import pytest

from q_backend.api.backtest_jobs import _execute_candle
from q_backend.api.schemas.backtest import BacktestRequest
from q_backend.backtesting.costs import TransactionCostConfig
from q_backend.backtesting.position_sizing import FixedQuantityPositionSizing
from q_backend.market_data.models import OHLCV
from q_backend.optimization.backtest_runner import BacktestRunConfig, DefaultBacktestRunner


@pytest.mark.parametrize("direction", [1, -1], ids=["long", "short"])
@pytest.mark.parametrize("explicit_entries", [False, True], ids=["legacy", "entries"])
@pytest.mark.parametrize("stop_loss_pct", [0.0, 0.05], ids=["middle-band", "stop"])
def test_bollinger_backtest_replays_optimization_exits(direction, explicit_entries, stop_loss_pct):
    # Each excursion is followed by mean reversion, then another excursion in the
    # same direction. A persistent entry-manager stance loses both exits/reentries.
    closes = [100.0] * 5 + [80.0, 81.0, 101.0] + [100.0] * 5 + [80.0, 81.0, 101.0, 100.0]
    closes = [100.0 + direction * (value - 100.0) for value in closes]
    index = pd.date_range("2024-01-01", periods=len(closes), freq="4h")
    frame = pd.DataFrame(
        {"open": closes, "high": [v + 1 for v in closes], "low": [v - 1 for v in closes], "close": closes},
        index=index,
    )
    # Trigger an explicit stop on the fill bar without altering the close-based
    # Bollinger signals. Both workflows must still use the shared exit overlay.
    if stop_loss_pct:
        frame.loc[index[6], "low" if direction == 1 else "high"] = 70.0 if direction == 1 else 130.0

    class FrozenMarketData:
        def get_ohlcv(self, symbol, timeframe, start, end):
            return [
                OHLCV(time=time, tick_volume=100, spread=0, real_volume=0, **row.to_dict())
                for time, row in frame.iterrows()
            ]

    params = {"period": 5, "num_std": 1.5}
    exits = {"stop_loss_pct": stop_loss_pct}
    costs = TransactionCostConfig(cost_per_contract=2.5)
    sizing = FixedQuantityPositionSizing(quantity=1.0)
    start, end = index[0].to_pydatetime(), index[-1].to_pydatetime()
    config = BacktestRunConfig(
        symbol="TEST",
        timeframe="H4",
        start=start,
        end=end,
        initial_capital=5000.0,
        point_value=12.5,
        strategy="BollingerReversion",
        strategy_params={**params, **exits},
        position_sizing=sizing,
        costs=costs,
    )
    optimized = DefaultBacktestRunner(data_provider=lambda _: frame).run(config)
    request = BacktestRequest(
        symbol=config.symbol,
        timeframe=config.timeframe,
        start=start,
        end=end,
        strategy=config.strategy,
        strategy_params=config.strategy_params,
        entries=[{"strategy": config.strategy, "params": params}] if explicit_entries else None,
        exit_params=exits,
        initial_capital=config.initial_capital,
        point_value=config.point_value,
        position_sizing=sizing,
        costs=costs,
    )
    result, _, _, market_data = _execute_candle(request, start, end, FrozenMarketData())
    expected = [trade.model_dump(mode="json") for trade in optimized.trades]
    assert len(expected) == 2
    if not stop_loss_pct:
        assert expected[0]["exit_time"] == datetime(2024, 1, 2, 8).isoformat()
        assert expected[0]["pnl"] == 232.5
        assert expected[0]["exit_reason"] == "SIGNAL"
    for trade in expected + result["trades"]:
        trade.pop("id")
        trade.pop("order_id")
    assert result["trades"] == expected
    assert result["metrics"] == {key: optimized.metrics[key] for key in result["metrics"]}
    assert market_data["buy_signal" if direction == 1 else "sell_signal"].sum() == 2
