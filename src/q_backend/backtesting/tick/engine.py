import uuid
from datetime import datetime, timezone
from typing import List

import numpy as np
from concurrent.futures import ProcessPoolExecutor

from q_backend.backtesting.engine import ParallelMode
from q_backend.backtesting.models import OrderAction, Trade
from q_backend.backtesting.position_sizing import PositionSizingConfig
from q_backend.backtesting.registry import TradeRegistry
from q_backend.backtesting.tick.kernel import simulate
from q_backend.backtesting.tick.orders import kernel_sizing_params
from q_backend.backtesting.tick.strategy import TickArrays, TickStrategy


def _msc_to_datetime(msc: int) -> datetime:
    return datetime.fromtimestamp(msc / 1000.0, tz=timezone.utc)


def _split_ticks_by_day(ticks: TickArrays) -> List[TickArrays]:
    n = len(ticks.time_msc)
    if n == 0:
        return []

    day_ids = ticks.time_msc // 86400000
    boundaries = np.where(day_ids[1:] != day_ids[:-1])[0] + 1
    starts = np.concatenate((np.array([0], dtype=np.int64), boundaries))
    ends = np.concatenate((boundaries, np.array([n], dtype=np.int64)))

    chunks: List[TickArrays] = []
    for start, end in zip(starts, ends):
        chunks.append(
            TickArrays(
                time_msc=ticks.time_msc[start:end],
                bid=ticks.bid[start:end],
                ask=ticks.ask[start:end],
                last=ticks.last[start:end],
                volume=ticks.volume[start:end],
            )
        )
    return chunks


def _events_to_registry(
    events: tuple,
    time_msc: np.ndarray,
    symbol: str,
    point_value: float,
) -> TradeRegistry:
    (
        entry_idx,
        exit_idx,
        entry_prices,
        exit_prices,
        directions,
        quantities,
        _exit_reasons,
        trade_count,
        _final_capital,
    ) = events

    registry = TradeRegistry()
    for i in range(trade_count):
        direction = int(directions[i])
        order_id = str(uuid.uuid4())
        trade = Trade(
            id=str(uuid.uuid4()),
            order_id=order_id,
            symbol=symbol,
            action=OrderAction.BUY if direction > 0 else OrderAction.SELL,
            quantity=float(quantities[i]),
            entry_time=_msc_to_datetime(int(time_msc[int(entry_idx[i])])),
            entry_price=float(entry_prices[i]),
            point_value=point_value,
        )
        registry.register_trade(trade)
        registry.close_trade(
            trade.id,
            _msc_to_datetime(int(time_msc[int(exit_idx[i])])),
            float(exit_prices[i]),
        )

    return registry


def _run_tick_day_chunk(args) -> TradeRegistry:
    (
        strategy,
        sizing_config,
        initial_capital,
        point_value,
        symbol,
        chunk,
    ) = args
    engine = TickBacktestEngine(
        strategy=strategy,
        sizing_config=sizing_config,
        initial_capital=initial_capital,
        point_value=point_value,
        symbol=symbol,
    )
    return engine._run_single_chunk(chunk)


class TickBacktestEngine:
    def __init__(
        self,
        strategy: TickStrategy,
        sizing_config: PositionSizingConfig,
        initial_capital: float = 100000.0,
        point_value: float = 1.0,
        symbol: str = "TEST",
    ):
        self.strategy = strategy
        self.sizing_config = sizing_config
        self.initial_capital = initial_capital
        self.point_value = point_value
        self.symbol = symbol
        self._sizing_mode, self._sizing_a, self._sizing_b, self._sizing_c = (
            kernel_sizing_params(sizing_config)
        )

    def run(
        self,
        ticks: TickArrays,
        parallel_mode: ParallelMode = ParallelMode.DAY_TRADE,
    ) -> TradeRegistry:
        if len(ticks.time_msc) == 0:
            return TradeRegistry()

        if parallel_mode == ParallelMode.DAY_TRADE:
            chunks = _split_ticks_by_day(ticks)
            args_list = [
                (
                    self.strategy,
                    self.sizing_config,
                    self.initial_capital,
                    self.point_value,
                    self.symbol,
                    chunk,
                )
                for chunk in chunks
            ]

            with ProcessPoolExecutor() as executor:
                results = list(executor.map(_run_tick_day_chunk, args_list))

            master = TradeRegistry()
            for registry in results:
                master.merge(registry)
            return master

        return self._run_single_chunk(ticks)

    def _run_single_chunk(self, ticks: TickArrays) -> TradeRegistry:
        signals = self.strategy.compute_signals(ticks)
        events = simulate(
            ticks.bid,
            ticks.ask,
            signals.direction,
            signals.sl_points,
            signals.tp_points,
            self.initial_capital,
            self.point_value,
            self._sizing_mode,
            self._sizing_a,
            self._sizing_b,
            self._sizing_c,
        )
        return _events_to_registry(
            events, ticks.time_msc, self.symbol, self.point_value
        )
