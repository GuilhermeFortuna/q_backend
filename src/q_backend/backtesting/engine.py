import datetime
from typing import List, Optional
import pandas as pd
from enum import Enum

from q_backend.backtesting.costs import TransactionCostConfig
from q_backend.backtesting.registry import TradeRegistry
from q_backend.backtesting.strategy import TradingStrategy
from q_backend.backtesting.position_sizing import PositionSizer
from q_backend.backtesting.indicator_frame import augment_indicator_frame
from q_backend.backtesting.signal_columns import signal_arrays
from q_backend.backtesting.candle_kernel import (
    ledger_to_registry,
    parse_day_trade_times,
    run_chunk,
    sizer_to_kernel,
)


class ParallelMode(str, Enum):
    SEQUENTIAL = "SEQUENTIAL"
    DAY_TRADE = "DAY_TRADE"


class BacktestEngine:
    """
    Core execution engine for backtests. Iterates through historical data,
    evaluates strategy signals, manages position sizing, and tracks trades in the registry.
    """

    def __init__(
        self,
        strategy: TradingStrategy,
        sizer: PositionSizer,
        initial_capital: float = 100000.0,
        point_values: Optional[dict] = None,
        day_trade: bool = False,
        day_trade_start_time: str = "09:00",
        day_trade_end_time: str = "16:00",
        day_trade_close_time: str = "17:00",
        costs: Optional[TransactionCostConfig] = None,
    ):
        self.strategy = strategy
        self.sizer = sizer
        self.initial_capital = initial_capital
        self.point_values = point_values or {}
        self.day_trade = day_trade
        self.day_trade_start_time = day_trade_start_time
        self.day_trade_end_time = day_trade_end_time
        self.day_trade_close_time = day_trade_close_time
        self.costs = costs
        self.kernel_sizing = sizer_to_kernel(sizer)

    def run(
        self,
        data: pd.DataFrame,
        parallel_mode: ParallelMode = ParallelMode.SEQUENTIAL,
        trade_start: Optional[datetime.datetime] = None,
    ) -> TradeRegistry:
        """
        Entry point for running a backtest.

        Args:
            data: Historical market data.
            parallel_mode: Execution mode (SEQUENTIAL for swing trading, DAY_TRADE for parallel daily processing).
            trade_start: When set, bars before this timestamp warm the strategy's
                indicators but produce no trades — the backtest starts flat here. Used
                by walk-forward to give an out-of-sample window the indicator lookback
                that sits just before it, so a long-period strategy is not judged on a
                window where its indicators are still all-NaN.

        Returns:
            TradeRegistry: Contains all executed trades and performance metrics.
        """
        if data.empty:
            return TradeRegistry()

        # Ensure index is datetime for day splitting
        if not isinstance(data.index, pd.DatetimeIndex):
            try:
                data.index = pd.to_datetime(data.index)
            except Exception as e:
                raise ValueError("DataFrame index must be or be convertible to a DatetimeIndex.") from e

        if parallel_mode == ParallelMode.DAY_TRADE:
            master_registry = TradeRegistry()

            # Each trading day is independent, so we process the day-chunks
            # sequentially in-process and merge them. We deliberately do NOT spawn a
            # ProcessPoolExecutor here: the Dramatiq worker pool is the single,
            # budgeted source of CPU parallelism in the backend, and a nested pool
            # per backtest would oversubscribe the cores when several jobs run.
            for _, chunk in data.groupby(data.index.date):
                master_registry.merge(self._run_single_chunk(chunk, force_close_at_end=True, trade_start=trade_start))

            return master_registry

        else:  # SEQUENTIAL
            return self._run_single_chunk(data, force_close_at_end=False, trade_start=trade_start)

    def _run_single_chunk(
        self,
        chunk: pd.DataFrame,
        force_close_at_end: bool,
        trade_start: Optional[datetime.datetime] = None,
    ) -> TradeRegistry:
        # 1. Compute indicators (vectorized, no lookahead bias)
        chunk = augment_indicator_frame(self.strategy, chunk)
        signals = signal_arrays(self.strategy, chunk)
        day_trade_us = (
            parse_day_trade_times(self.day_trade_start_time, self.day_trade_end_time, self.day_trade_close_time)
            if self.day_trade
            else None
        )
        run = run_chunk(
            self.strategy,
            chunk,
            signals,
            sizing=self.kernel_sizing,
            initial_capital=self.initial_capital,
            point_value=self.point_values.get(self.strategy.symbol, 1.0),
            costs=self.costs,
            day_trade_us=day_trade_us,
            force_close_at_end=force_close_at_end,
            trade_start=trade_start,
        )
        return ledger_to_registry(
            run,
            signals.index,
            symbol=self.strategy.symbol,
            point_value=self.point_values.get(self.strategy.symbol, 1.0),
        )
