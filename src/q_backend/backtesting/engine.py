import uuid
import datetime
from typing import List, Optional
import pandas as pd
from enum import Enum

from q_backend.backtesting.costs import TransactionCostConfig, side_cost
from q_backend.backtesting.models import (
    Signal,
    Trade,
    OrderAction,
    SignalAction,
    TradeStatus,
)
from q_backend.backtesting.registry import TradeRegistry
from q_backend.backtesting.strategy import TradingStrategy
from q_backend.backtesting.position_sizing import PositionSizer


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

    def _close_trade_with_costs(
        self,
        registry: TradeRegistry,
        trade_id: str,
        exit_time: datetime.datetime,
        exit_price: float,
    ) -> Optional[Trade]:
        trade = registry.trades.get(trade_id)
        if trade and trade.status != TradeStatus.CLOSED:
            trade.commission += side_cost(
                self.costs, exit_price, trade.quantity, trade.point_value
            )
        return registry.close_trade(trade_id, exit_time, exit_price)

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
                raise ValueError(
                    "DataFrame index must be or be convertible to a DatetimeIndex."
                ) from e

        if parallel_mode == ParallelMode.DAY_TRADE:
            master_registry = TradeRegistry()

            # Each trading day is independent, so we process the day-chunks
            # sequentially in-process and merge them. We deliberately do NOT spawn a
            # ProcessPoolExecutor here: the Dramatiq worker pool is the single,
            # budgeted source of CPU parallelism in the backend, and a nested pool
            # per backtest would oversubscribe the cores when several jobs run.
            for _, chunk in data.groupby(data.index.date):
                master_registry.merge(
                    self._run_single_chunk(
                        chunk, force_close_at_end=True, trade_start=trade_start
                    )
                )

            return master_registry

        else:  # SEQUENTIAL
            return self._run_single_chunk(
                data, force_close_at_end=False, trade_start=trade_start
            )

    def _run_single_chunk(
        self,
        chunk: pd.DataFrame,
        force_close_at_end: bool,
        trade_start: Optional[datetime.datetime] = None,
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
            # Warm-up bars: indicators are already computed over the whole chunk, but
            # we take no action before trade_start so the run starts flat there.
            if trade_start is not None and timestamp < trade_start:
                continue
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
                    closed_trade = self._close_trade_with_costs(
                        registry, t.id, timestamp, fill_price
                    )
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
                    closed_trade = self._close_trade_with_costs(
                        registry, t.id, timestamp, fill_price
                    )
                    if closed_trade and closed_trade.pnl is not None:
                        current_capital += closed_trade.pnl

            # C. Execute entries queued on the previous bar.
            for sig in pending_entries:
                order = self.sizer.size_signal(
                    sig, fill_price, current_capital, current_data=current_data
                )
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
                        commission=side_cost(
                            self.costs, fill_price, order.quantity, point_val
                        ),
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
                    closed_trade = self._close_trade_with_costs(
                        registry, t.id, timestamp, close_price
                    )
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
                self._close_trade_with_costs(registry, t.id, final_time, final_price)

        return registry

