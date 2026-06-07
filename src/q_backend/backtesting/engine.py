import uuid
from concurrent.futures import ProcessPoolExecutor
from typing import List, Optional
import pandas as pd
from enum import Enum

from q_backend.backtesting.models import Trade, OrderAction, SignalAction
from q_backend.backtesting.registry import TradeRegistry
from q_backend.backtesting.strategy import TradingStrategy
from q_backend.backtesting.position_sizing import PositionSizer


class ParallelMode(str, Enum):
    SEQUENTIAL = "SEQUENTIAL"
    DAY_TRADE = "DAY_TRADE"


def _run_day_trade_chunk(args) -> TradeRegistry:
    """
    Top-level helper for multiprocessing since instance methods can be tricky to pickle.
    """
    strategy, sizer, initial_capital, point_values, chunk = args
    engine = BacktestEngine(strategy, sizer, initial_capital, point_values=point_values)
    return engine._run_single_chunk(chunk, force_close_at_end=True)


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
    ):
        self.strategy = strategy
        self.sizer = sizer
        self.initial_capital = initial_capital
        self.point_values = point_values or {}

    def run(
        self, data: pd.DataFrame, parallel_mode: ParallelMode = ParallelMode.SEQUENTIAL
    ) -> TradeRegistry:
        """
        Entry point for running a backtest.

        Args:
            data: Historical market data.
            parallel_mode: Execution mode (SEQUENTIAL for swing trading, DAY_TRADE for parallel daily processing).

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
                raise ValueError(
                    "DataFrame index must be or be convertible to a DatetimeIndex."
                ) from e

        if parallel_mode == ParallelMode.DAY_TRADE:
            master_registry = TradeRegistry()

            # Group by date
            chunks = [group for _, group in data.groupby(data.index.date)]

            # Prepare args for multiprocessing
            args_list = [
                (
                    self.strategy,
                    self.sizer,
                    self.initial_capital,
                    self.point_values,
                    chunk,
                )
                for chunk in chunks
            ]

            # Execute in parallel
            with ProcessPoolExecutor() as executor:
                results = list(executor.map(_run_day_trade_chunk, args_list))

            # Merge results
            for registry in results:
                master_registry.merge(registry)

            return master_registry

        else:  # SEQUENTIAL
            return self._run_single_chunk(data, force_close_at_end=False)

    def _run_single_chunk(
        self, chunk: pd.DataFrame, force_close_at_end: bool
    ) -> TradeRegistry:
        registry = TradeRegistry()
        current_capital = self.initial_capital

        # 1. Compute indicators (vectorized, no lookahead bias)
        chunk = self.strategy.compute_indicators(chunk)

        # 2. Iterative evaluation
        for i in range(len(chunk)):
            current_data = chunk.iloc[i]
            timestamp = current_data.name
            current_price = current_data.get("close", 0.0)  # Assume 'close' exists

            # A. Check for Exits first (give priority to closing positions)
            exit_signals = self.strategy.check_exit_conditions(
                current_data, registry.get_open_trades()
            )
            for sig in exit_signals:
                if sig.action == SignalAction.CLOSE:
                    # In this simple model, we close all open trades for the symbol
                    # A more advanced model would let the sizer emit CLOSE orders.
                    open_trades = [
                        t for t in registry.get_open_trades() if t.symbol == sig.symbol
                    ]
                    for t in open_trades:
                        closed_trade = registry.close_trade(
                            t.id, timestamp, current_price
                        )
                        if closed_trade and closed_trade.pnl is not None:
                            current_capital += closed_trade.pnl

            # B. Check for Entries
            entry_signals = self.strategy.check_entry_conditions(current_data)
            for sig in entry_signals:
                order = self.sizer.size_signal(sig, current_price, current_capital)
                if order:
                    registry.register_order(order)

                    # C. Execute Order (Simplistic immediate market execution)
                    # Deduct from capital (naively) or just track PnL
                    point_val = self.point_values.get(order.symbol, 1.0)
                    trade = Trade(
                        id=str(uuid.uuid4()),
                        order_id=order.id,
                        symbol=order.symbol,
                        action=order.action,
                        quantity=order.quantity,
                        entry_time=timestamp,
                        entry_price=current_price,
                        point_value=point_val,
                    )
                    registry.register_trade(trade)

        # 3. End of chunk force close
        if force_close_at_end and len(chunk) > 0:
            final_row = chunk.iloc[-1]
            final_time = final_row.name
            final_price = final_row.get("close", 0.0)

            for t in registry.get_open_trades():
                registry.close_trade(t.id, final_time, final_price)

        return registry
