from datetime import datetime
from typing import Any, Optional

import MetaTrader5 as mt5

from q_backend.backtesting.engine import ParallelMode
from q_backend.backtesting.models import Trade
from q_backend.backtesting.position_sizing import FixedQuantityPositionSizing
from q_backend.backtesting.tick.engine import TickBacktestEngine
from q_backend.backtesting.tick.factory import build_tick_strategy
from q_backend.backtesting.tick.strategy import TickArrays
from q_backend.optimization.backtest_runner import BacktestRunConfig, BacktestRunResult
from q_backend.optimization.metrics import build_equity_curve, compute_extended_metrics


def resolve_tick_flags(tick_flags: Optional[str]) -> int:
    if tick_flags is None or tick_flags.lower() == "all":
        return mt5.COPY_TICKS_ALL
    if tick_flags.lower() == "trade":
        return mt5.COPY_TICKS_TRADE
    raise ValueError(
        f"Invalid tick_flags '{tick_flags}'. Expected 'all' or 'trade'."
    )


def columnar_to_tick_arrays(columnar: dict[str, Any]) -> TickArrays:
    return TickArrays(
        time_msc=columnar["time_msc"],
        bid=columnar["bid"],
        ask=columnar["ask"],
        last=columnar["last"],
        volume=columnar["volume"],
    )


def _naive_closed_trades(trades: list[Trade]) -> list[Trade]:
    """Match candle optimization: config datetimes are naive local."""
    normalized: list[Trade] = []
    for trade in trades:
        updates: dict[str, datetime] = {}
        if trade.entry_time.tzinfo is not None:
            updates["entry_time"] = trade.entry_time.replace(tzinfo=None)
        if trade.exit_time is not None and trade.exit_time.tzinfo is not None:
            updates["exit_time"] = trade.exit_time.replace(tzinfo=None)
        normalized.append(trade.model_copy(update=updates) if updates else trade)
    return normalized


class TickBacktestRunner:
    """Runs tick backtests over a fixed in-memory tick stream for Optuna trials."""

    def __init__(self, ticks: TickArrays):
        self._ticks = ticks

    @classmethod
    def from_market_data(
        cls,
        market_data_service: Any,
        *,
        symbol: str,
        start: datetime,
        end: datetime,
        flags: int = mt5.COPY_TICKS_ALL,
    ) -> "TickBacktestRunner":
        """Fetch ticks once and reuse them for every trial in a study.

        MetaTrader5 must be used from the thread that initialized it. Optimization
        studies run on a worker thread, so tick data is loaded here on the caller
        thread (typically the FastAPI request handler) and cached in memory.
        """
        arrays = market_data_service.get_ticks_columnar(
            symbol, start, end, flags=flags
        )
        if len(arrays["time_msc"]) == 0:
            raise ValueError("No tick data found for the given parameters.")
        return cls(columnar_to_tick_arrays(arrays))

    def run(self, config: BacktestRunConfig) -> BacktestRunResult:
        strategy = build_tick_strategy(
            config.strategy, config.strategy_params, config.symbol
        )
        sizing_config = config.position_sizing or FixedQuantityPositionSizing()
        engine = TickBacktestEngine(
            strategy=strategy,
            sizing_config=sizing_config,
            initial_capital=config.initial_capital,
            point_value=config.point_value,
            symbol=config.symbol,
        )
        registry = engine.run(self._ticks, parallel_mode=ParallelMode.DAY_TRADE)

        closed_trades = _naive_closed_trades(registry.get_closed_trades())
        base_metrics = registry.get_performance_metrics(config.initial_capital)
        equity_curve = build_equity_curve(
            closed_trades,
            config.initial_capital,
            config.start,
            config.end,
        )
        backtest_days = max((config.end - config.start).total_seconds() / 86400, 1.0)
        metrics = compute_extended_metrics(
            base_metrics,
            closed_trades,
            config.initial_capital,
            equity_curve,
            backtest_days,
        )

        return BacktestRunResult(
            metrics=metrics,
            trial_user_attrs={
                "total_trades": metrics.get("total_trades", 0),
            },
        )
