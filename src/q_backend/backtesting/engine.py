import uuid
import datetime
from concurrent.futures import ProcessPoolExecutor
from typing import List, Optional
import pandas as pd
from enum import Enum

from q_backend.backtesting.models import Signal, Trade, OrderAction, SignalAction
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
    (
        strategy,
        sizer,
        initial_capital,
        point_values,
        day_trade,
        day_trade_start_time,
        day_trade_end_time,
        day_trade_close_time,
        chunk,
    ) = args
    engine = BacktestEngine(
        strategy,
        sizer,
        initial_capital,
        point_values=point_values,
        day_trade=day_trade,
        day_trade_start_time=day_trade_start_time,
        day_trade_end_time=day_trade_end_time,
        day_trade_close_time=day_trade_close_time,
    )
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
        day_trade: bool = False,
        day_trade_start_time: str = "09:00",
        day_trade_end_time: str = "16:00",
        day_trade_close_time: str = "17:00",
    ):
        self.strategy = strategy
        self.sizer = sizer
        self.initial_capital = initial_capital
        self.point_values = point_values or {}
        self.day_trade = day_trade
        self.day_trade_start_time = day_trade_start_time
        self.day_trade_end_time = day_trade_end_time
        self.day_trade_close_time = day_trade_close_time

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
                    self.day_trade,
                    self.day_trade_start_time,
                    self.day_trade_end_time,
                    self.day_trade_close_time,
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

        # Parse time boundaries if day trading is active
        if self.day_trade:
            try:
                parts = self.day_trade_start_time.split(":")
                start_t = datetime.time(int(parts[0]), int(parts[1]))
                parts = self.day_trade_end_time.split(":")
                end_t = datetime.time(int(parts[0]), int(parts[1]))
                parts = self.day_trade_close_time.split(":")
                close_t = datetime.time(int(parts[0]), int(parts[1]))
            except Exception as e:
                raise ValueError(
                    f"Invalid day trade time config (start={self.day_trade_start_time}, "
                    f"end={self.day_trade_end_time}, close={self.day_trade_close_time}). "
                    f"Must be HH:MM format."
                ) from e

        # 2. Iterative evaluation.
        #
        # Execution model: signals are derived from a *fully closed* bar and
        # executed at the NEXT bar's open. A strategy can only know that bar i
        # met its criteria once bar i has closed, so the earliest tradable
        # price is bar i+1's open. Filling at bar i's own close would let a
        # trade react to information that only exists at the same instant the
        # bar finishes -- a subtle look-ahead bias. Keeping fill timing here
        # (and out of the strategies) means no individual strategy can
        # reintroduce it. See test_strategy_causality.py for the guardrail.
        pending_exits: List[Signal] = []
        pending_entries: List[Signal] = []

        for i in range(len(chunk)):
            current_data = chunk.iloc[i]
            timestamp = current_data.name
            current_time = timestamp.time()
            # Orders queued on the previous bar fill at this bar's open. Fall
            # back to close for close-only series that carry no 'open' column.
            fill_price = current_data.get("open", current_data.get("close", 0.0))

            is_last_bar_of_day = (
                i == len(chunk) - 1
                or chunk.index[i + 1].date() != timestamp.date()
            )

            # A. Early force-close at close time (priority over executing pending list).
            if self.day_trade and current_time >= close_t:
                open_trades = registry.get_open_trades()
                for t in open_trades:
                    closed_trade = registry.close_trade(t.id, timestamp, fill_price)
                    if closed_trade and closed_trade.pnl is not None:
                        current_capital += closed_trade.pnl
                pending_exits = []
                pending_entries = []
                continue

            # B. Execute exits queued on the previous bar (priority over entries).
            for sig in pending_exits:
                if sig.action != SignalAction.CLOSE:
                    continue
                open_trades = [
                    t for t in registry.get_open_trades() if t.symbol == sig.symbol
                ]
                for t in open_trades:
                    closed_trade = registry.close_trade(t.id, timestamp, fill_price)
                    if closed_trade and closed_trade.pnl is not None:
                        current_capital += closed_trade.pnl

            # C. Execute entries queued on the previous bar.
            for sig in pending_entries:
                order = self.sizer.size_signal(sig, fill_price, current_capital)
                if order:
                    max_size = self.sizer.max_position_size(
                        fill_price, current_capital
                    )
                    if max_size is not None:
                        open_qty = sum(
                            t.quantity
                            for t in registry.get_open_trades()
                            if t.symbol == order.symbol
                        )
                        remaining = max_size - open_qty
                        if remaining <= 0:
                            continue
                        if order.quantity > remaining:
                            order.quantity = remaining

                    registry.register_order(order)
                    point_val = self.point_values.get(order.symbol, 1.0)
                    trade = Trade(
                        id=str(uuid.uuid4()),
                        order_id=order.id,
                        symbol=order.symbol,
                        action=order.action,
                        quantity=order.quantity,
                        entry_time=timestamp,
                        entry_price=fill_price,
                        point_value=point_val,
                    )
                    registry.register_trade(trade)

            # D. Evaluate this (now-closed) bar and queue signals for the next bar.
            if self.day_trade:
                # We can check exits anytime before force-close time
                if current_time < close_t and not is_last_bar_of_day:
                    pending_exits = self.strategy.check_exit_conditions(
                        current_data, registry.get_open_trades()
                    )
                    # We only check entries within the entry window
                    if start_t <= current_time <= end_t:
                        pending_entries = self.strategy.check_entry_conditions(current_data)
                    else:
                        pending_entries = []
                else:
                    pending_exits = []
                    pending_entries = []
            else:
                pending_exits = self.strategy.check_exit_conditions(
                    current_data, registry.get_open_trades()
                )
                pending_entries = self.strategy.check_entry_conditions(current_data)

            # E. Daily force-close at the end of the last bar of the day.
            if self.day_trade and is_last_bar_of_day:
                close_price = current_data.get("close", fill_price)
                open_trades = registry.get_open_trades()
                for t in open_trades:
                    closed_trade = registry.close_trade(t.id, timestamp, close_price)
                    if closed_trade and closed_trade.pnl is not None:
                        current_capital += closed_trade.pnl
                pending_exits = []
                pending_entries = []

        # 3. End of chunk force close
        if force_close_at_end and len(chunk) > 0:
            final_row = chunk.iloc[-1]
            final_time = final_row.name
            final_price = final_row.get("close", 0.0)

            for t in registry.get_open_trades():
                registry.close_trade(t.id, final_time, final_price)

        return registry

