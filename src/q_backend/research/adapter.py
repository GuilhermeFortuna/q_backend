"""Private adapter bridging ResearchStrategy to candle TradingStrategy."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Mapping

import numpy as np
import pandas as pd
from pandas.api.types import is_numeric_dtype

from q_backend.backtesting.exit_strategy import ExitStrategy
from q_backend.backtesting.signal_columns import (
    BAR_INDEX,
    LEVEL_COLUMNS,
    SIGNAL_COLUMNS,
    SIGNAL_STOP_PRICE,
    SIGNAL_TARGET_PRICE,
    write_signal_columns,
)
from q_backend.backtesting.strategy import ChartIndicatorSpec, TradingStrategy
from q_backend.research.charting import ChartIndicator
from q_backend.research.frame import FRAME_COLUMNS
from q_backend.research.hooks import detect_hook_call, invoke_hook
from q_backend.research.orders import TradeOrder
from q_backend.research.positions import ResearchPosition
from q_backend.research.progress import BacktestProgress
from q_backend.research.strategy import ResearchStrategy
from q_backend.research.tick_phase import TickDecisions, TickEvaluator, TickWorkerPool

_RESERVED_COLUMNS = frozenset((*SIGNAL_COLUMNS, *LEVEL_COLUMNS, BAR_INDEX))
_MARKET_COLUMNS = ("open", "high", "low", "close")


def _owned_prefix(frame: pd.DataFrame, end: int) -> pd.DataFrame:
    prefix = frame.iloc[:end].copy()
    # DataFrame.copy retains index backing storage, which otherwise exposes future
    # timestamps and lets a hook mutate later decision/position timestamps.
    prefix.index = prefix.index.copy(deep=True)
    prefix.attrs = {}
    return prefix


@dataclass
class _TickPlan:
    """Tick-phase decisions for one screened candle.

    ``pending`` lists the ticks that are evaluated: the first, and every later one that moves
    the forming candle's high, low or close. The others repeat a frame that already declined
    to exit, apart from its tick count. Decisions hold for one position snapshot.
    """

    bar: int
    prices: np.ndarray
    candles: np.ndarray
    pending: np.ndarray
    positions: tuple
    cursor: int = 0
    decided: int = 0
    exit_tick: int = -1
    next_tick: int = 0


class _RuntimeHooks:
    """Per-chunk decisions for the candle kernel's runtime callbacks.

    ``strategy`` runs once per bar after intrabar fills. ``screen`` and ``tick`` exist only
    for a phase-aware exit and are None otherwise. Recorded levels are indexed by the bar
    that queued the entry, which opens on the following bar.
    """

    def __init__(
        self,
        adapter: ResearchStrategyAdapter,
        chunk: pd.DataFrame,
        history_positions: np.ndarray,
        intrabar: Callable[[int], tuple[np.ndarray, np.ndarray]] | None,
    ) -> None:
        self._adapter = adapter
        self._chunk = chunk
        self._history_positions = history_positions
        self._intrabar = intrabar
        self._progress = adapter.progress
        self.stop_price = np.full(len(chunk), np.nan)
        self.target_price = np.full(len(chunk), np.nan)
        self._plan: _TickPlan | None = None
        self.screen: Callable[[int, tuple], bool] | None = self._screen if adapter._phased_exit else None
        self.tick: Callable[[int, int, int, float, tuple], bool] | None = self._tick if adapter._phased_exit else None

    def strategy(self, bar: int, raw_positions: tuple) -> tuple[int, bool, bool, float, float, float]:
        adapter = self._adapter
        positions = self._positions(raw_positions)
        end = int(self._history_positions[bar]) + 1
        phase = "bar" if adapter._phased_exit else None
        exit_decision = adapter._invoke_hook(
            "exit_strategy", _owned_prefix(adapter._decision_frame, end), positions, phase=phase
        )
        entry_decision = adapter._invoke_hook("entry_strategy", _owned_prefix(adapter._decision_frame, end), positions)
        entry = 0 if entry_decision is None else (1 if entry_decision.action == "buy" else -1)
        stop = target = np.nan
        if entry_decision is not None and entry_decision.action in ("buy", "sell"):
            stop, target = adapter._levels_of(entry_decision, self._chunk.index[bar])
            self.stop_price[bar] = stop
            self.target_price[bar] = target
        exit_flag = exit_decision is not None
        self._progress.bar(bar + 1, self._chunk.index[bar], _position_label(positions))
        return entry, exit_flag, exit_flag, 1.0 if entry else 0.0, stop, target

    def _screen(self, bar: int, raw_positions: tuple) -> bool:
        adapter = self._adapter
        end = int(self._history_positions[bar]) + 1
        decision = adapter._invoke_hook(
            "exit_strategy",
            _owned_prefix(adapter._decision_frame, end),
            self._positions(raw_positions),
            phase="screen",
        )
        return decision is not None

    def _tick(self, bar: int, tick: int, time_us: int, price: float, raw_positions: tuple) -> bool:
        plan = self._plan
        if tick == 0:
            plan = self._plan = self._new_plan(bar, raw_positions)
        if plan is None or plan.bar != bar or plan.next_tick != tick or plan.prices[tick] != price:
            raise RuntimeError(f"Tick replay for bar {bar} arrived out of order at tick {tick}")
        plan.next_tick += 1
        if raw_positions != plan.positions:
            # Decisions were taken for other positions: start over from this tick.
            plan.positions = raw_positions
            plan.pending = np.concatenate(([tick], plan.pending[plan.pending > tick]))
            plan.cursor = plan.decided = 0
            plan.exit_tick = -1
        if plan.cursor >= len(plan.pending) or plan.pending[plan.cursor] != tick:
            return False
        if plan.cursor >= plan.decided:
            self._decide(plan, raw_positions)
        plan.cursor += 1
        return plan.exit_tick == tick

    def _new_plan(self, bar: int, raw_positions: tuple) -> _TickPlan:
        if self._intrabar is None:
            raise RuntimeError("Tick replay requires the candle price source")
        _times, prices = self._intrabar(bar)
        high = np.maximum.accumulate(prices)
        low = np.minimum.accumulate(prices)
        candles = np.column_stack((np.full(len(prices), prices[0]), high, low, prices))
        # A new high or low is also a new close, so a changed price marks every changed candle.
        moved = np.flatnonzero(prices[1:] != prices[:-1]) + 1
        return _TickPlan(
            bar=bar,
            prices=prices,
            candles=candles,
            pending=np.concatenate(([0], moved)),
            positions=raw_positions,
        )

    def _decide(self, plan: _TickPlan, raw_positions: tuple) -> None:
        """Decide the next wave of pending ticks, stopping at the first exit."""
        decisions = self._adapter._tick_decisions
        if decisions is None:
            raise RuntimeError("Research indicators must be prepared before tick replay")
        wave = plan.pending[plan.decided : plan.decided + decisions.wave_size]
        hit = decisions.first_exit(
            int(self._history_positions[plan.bar]), wave, plan.candles[wave], self._positions(raw_positions)
        )
        if hit is None:
            plan.decided += len(wave)
            exhausted = plan.decided >= len(plan.pending)
            replayed = len(plan.prices) if exhausted else int(plan.pending[plan.decided])
        else:
            plan.exit_tick = int(wave[hit])
            plan.decided = len(plan.pending)
            replayed = plan.exit_tick + 1
        self._progress.replay(self._chunk.index[plan.bar], replayed, len(plan.prices))

    def _positions(self, raw_positions: tuple) -> tuple[ResearchPosition, ...]:
        chunk = self._chunk
        return tuple(
            ResearchPosition(
                symbol=self._adapter.symbol,
                side="long" if side == 1 else "short",
                entry_time=chunk.index[entry_bar],
                entry_price=price,
                quantity=quantity,
            )
            for _ordinal, side, entry_bar, price, quantity in raw_positions
        )


def _position_label(positions: tuple[ResearchPosition, ...]) -> str:
    if not positions:
        return "flat"
    quantity = sum(position.quantity for position in positions)
    return f"{positions[0].side} {quantity:g}"


class ResearchStrategyAdapter(TradingStrategy):
    """Private adapter compiling ResearchStrategy hooks into candle engine signals."""

    def __init__(
        self,
        research_strategy: ResearchStrategy,
        symbol: str,
        exit_params: Mapping[str, Any] | None = None,
        *,
        ticks_available: bool = False,
        workers: int = 1,
        progress: BacktestProgress | None = None,
    ) -> None:
        if not isinstance(research_strategy, ResearchStrategy):
            raise TypeError(f"Expected ResearchStrategy instance, got {type(research_strategy).__name__}")

        self.research_strategy = research_strategy
        self.symbol = symbol
        self.parameters = dict(exit_params or {})

        # Retain engine exit strategy on the adapter so it does not shadow user's exit_strategy method
        self.exit_strategy = ExitStrategy(self.parameters)
        self._chart_indicators: tuple[ChartIndicator, ...] = ()
        self._ticks_available = ticks_available
        self._workers = workers
        self.progress = progress if progress is not None else BacktestProgress()
        self._hook_calls = {
            name: detect_hook_call(research_strategy, name) for name in ("exit_strategy", "entry_strategy")
        }
        self._uses_positions = any(
            call.positions != "legacy" and getattr(type(research_strategy), name) is not getattr(ResearchStrategy, name)
            for name, call in self._hook_calls.items()
        )
        self._phased_exit = self._hook_calls["exit_strategy"].phase
        self._uses_runtime = self._uses_positions or self._phased_exit
        self._decision_frame: pd.DataFrame | None = None
        self._tick_decisions: TickDecisions | None = None

    def close(self) -> None:
        """Release tick worker processes, if any were started."""
        if self._tick_decisions is not None:
            self._tick_decisions.close()
            self._tick_decisions = None

    @property
    def requires_ticks(self) -> bool:
        """Whether the strategy needs a TickStore to run, because its exit is phase-aware."""
        return self._phased_exit

    def compute_indicators(self, data: pd.DataFrame) -> pd.DataFrame:
        """Call compute_indicators once on an owned copy and compile prefix decisions."""
        if not isinstance(data, pd.DataFrame):
            raise TypeError(f"Expected DataFrame, got {type(data).__name__}")

        if data.columns.has_duplicates:
            raise ValueError("Input DataFrame contains duplicate column names")

        # Check reserved columns on input frame
        for col in _RESERVED_COLUMNS:
            if col in data.columns:
                raise ValueError(f"Input DataFrame contains reserved column {col!r}")

        orig_index = data.index
        orig_len = len(data)

        if orig_len == 0:
            empty = data.copy()
            empty.attrs = {}
            return write_signal_columns(
                empty,
                entry_long=pd.Series(dtype=bool, index=empty.index),
                entry_short=pd.Series(dtype=bool, index=empty.index),
                exit_long=pd.Series(dtype=bool, index=empty.index),
                exit_short=pd.Series(dtype=bool, index=empty.index),
                strategy_name=type(self.research_strategy).__name__,
            )

        # 1. Call compute_indicators once on an owned copy
        data_copy = data.copy()
        if hasattr(data, "attrs"):
            data_copy.attrs = {}  # Strip caller attrs

        try:
            augmented = self.research_strategy.compute_indicators(data_copy)
        except Exception as exc:
            raise RuntimeError(f"Error in {type(self.research_strategy).__name__}.compute_indicators: {exc}") from exc

        if not isinstance(augmented, pd.DataFrame):
            raise TypeError(
                f"{type(self.research_strategy).__name__}.compute_indicators must return a DataFrame, "
                f"got {type(augmented).__name__}"
            )

        if len(augmented) != orig_len or not augmented.index.equals(orig_index):
            raise ValueError(f"{type(self.research_strategy).__name__}.compute_indicators modified row count or index")

        # Disallow duplicate column names
        if augmented.columns.has_duplicates:
            raise ValueError("compute_indicators returned DataFrame with duplicate column names")

        # Disallow reserved columns in augmented frame
        for col in _RESERVED_COLUMNS:
            if col in augmented.columns:
                raise ValueError(
                    f"{type(self.research_strategy).__name__}.compute_indicators returned reserved column {col!r}"
                )

        # Check market columns were not modified/mutated
        for col in (*FRAME_COLUMNS, "volume"):
            if col in data.columns:
                if col not in augmented.columns:
                    raise ValueError(f"Missing original market column {col!r} after compute_indicators")
                if not data[col].equals(augmented[col]):
                    raise ValueError(f"Market column {col!r} was modified in compute_indicators")

        self._chart_indicators = self._declare_chart_indicators(augmented)

        # 2. Iterate bar by bar: evaluate exit_strategy then entry_strategy on owned prefix
        entry_long = np.zeros(orig_len, dtype=bool)
        entry_short = np.zeros(orig_len, dtype=bool)
        exit_long = np.zeros(orig_len, dtype=bool)
        exit_short = np.zeros(orig_len, dtype=bool)

        if self._uses_runtime:
            self._decision_frame = augmented.copy()
            self._decision_frame.attrs = {}
            if self._phased_exit:
                raw_history = data.copy()
                raw_history.attrs = {}
                self.close()
                evaluator = TickEvaluator(self.research_strategy, raw_history, self._hook_calls["exit_strategy"])
                self._tick_decisions = evaluator if self._workers == 1 else TickWorkerPool(evaluator, self._workers)
            self.progress.start(orig_len)
            return write_signal_columns(
                augmented,
                entry_long=pd.Series(entry_long, index=augmented.index, dtype=bool),
                entry_short=pd.Series(entry_short, index=augmented.index, dtype=bool),
                exit_long=pd.Series(exit_long, index=augmented.index, dtype=bool),
                exit_short=pd.Series(exit_short, index=augmented.index, dtype=bool),
                strategy_name=type(self.research_strategy).__name__,
            )

        stop_price = np.full(orig_len, np.nan)
        target_price = np.full(orig_len, np.nan)
        self.progress.start(orig_len)
        for i in range(orig_len):
            timestamp = augmented.index[i]

            # 2a. exit_strategy on an isolated owned copy of prefix
            prefix_exit = _owned_prefix(augmented, i + 1)

            try:
                exit_decision = self.research_strategy.exit_strategy(prefix_exit)
            except Exception as exc:
                raise RuntimeError(
                    f"Error in {type(self.research_strategy).__name__}.exit_strategy at bar {timestamp}: {exc}"
                ) from exc

            if exit_decision is not None:
                if not isinstance(exit_decision, TradeOrder):
                    raise TypeError(
                        f"{type(self.research_strategy).__name__}.exit_strategy at bar {timestamp} "
                        f"returned {type(exit_decision).__name__}, expected TradeOrder or None"
                    )
                if exit_decision.action != "close":
                    raise ValueError(
                        f"{type(self.research_strategy).__name__}.exit_strategy at bar {timestamp} "
                        f"returned illegal action {exit_decision.action!r}; only 'close' is permitted"
                    )
                exit_long[i] = True
                exit_short[i] = True

            # 2b. entry_strategy on an isolated owned copy of prefix
            prefix_entry = _owned_prefix(augmented, i + 1)

            try:
                entry_decision = self.research_strategy.entry_strategy(prefix_entry)
            except Exception as exc:
                raise RuntimeError(
                    f"Error in {type(self.research_strategy).__name__}.entry_strategy at bar {timestamp}: {exc}"
                ) from exc

            if entry_decision is not None:
                if not isinstance(entry_decision, TradeOrder):
                    raise TypeError(
                        f"{type(self.research_strategy).__name__}.entry_strategy at bar {timestamp} "
                        f"returned {type(entry_decision).__name__}, expected TradeOrder or None"
                    )
                if entry_decision.action == "buy":
                    entry_long[i] = True
                elif entry_decision.action == "sell":
                    entry_short[i] = True
                else:
                    raise ValueError(
                        f"{type(self.research_strategy).__name__}.entry_strategy at bar {timestamp} "
                        f"returned illegal action {entry_decision.action!r}; only 'buy' or 'sell' is permitted"
                    )
                stop_price[i], target_price[i] = self._levels_of(entry_decision, timestamp)
            self.progress.bar(i + 1, timestamp)

        # Write signal columns onto augmented frame
        result_df = write_signal_columns(
            augmented,
            entry_long=pd.Series(entry_long, index=augmented.index, dtype=bool),
            entry_short=pd.Series(entry_short, index=augmented.index, dtype=bool),
            exit_long=pd.Series(exit_long, index=augmented.index, dtype=bool),
            exit_short=pd.Series(exit_short, index=augmented.index, dtype=bool),
            strategy_name=type(self.research_strategy).__name__,
        )
        if not (np.isnan(stop_price).all() and np.isnan(target_price).all()):
            result_df[SIGNAL_STOP_PRICE] = stop_price
            result_df[SIGNAL_TARGET_PRICE] = target_price
        return result_df

    def _levels_of(self, decision: TradeOrder, timestamp: pd.Timestamp) -> tuple[float, float]:
        """Entry levels as floats, NaN where unset; they need ticks to be confirmed intrabar."""
        stop = np.nan if decision.stop_loss is None else float(decision.stop_loss)
        target = np.nan if decision.take_profit is None else float(decision.take_profit)
        if (decision.stop_loss is not None or decision.take_profit is not None) and not self._ticks_available:
            raise ValueError(
                f"{type(self.research_strategy).__name__} entry at bar {timestamp} carries stop or take-profit "
                "levels, which are confirmed from ticks; pass ticks= to backtest()"
            )
        return stop, target

    def runtime_callback(
        self,
        chunk: pd.DataFrame,
        intrabar: Callable[[int], tuple[np.ndarray, np.ndarray]] | None = None,
    ) -> _RuntimeHooks | None:
        """Bind one prepared chunk to its original, isolated indicator history."""
        if not self._uses_runtime:
            return None
        history = self._decision_frame
        if history is None:
            raise RuntimeError("Research indicators must be prepared before runtime decisions")
        history_positions = history.index.get_indexer(chunk.index)
        if (history_positions < 0).any():
            raise ValueError("Runtime chunk is not part of the prepared research frame")
        return _RuntimeHooks(self, chunk, history_positions, intrabar)

    def _invoke_hook(
        self,
        name: str,
        prefix: pd.DataFrame,
        positions: tuple[ResearchPosition, ...],
        *,
        phase: str | None = None,
    ) -> TradeOrder | None:
        return invoke_hook(self.research_strategy, self._hook_calls[name], name, prefix, positions, phase=phase)

    def _declare_chart_indicators(self, augmented: pd.DataFrame) -> tuple[ChartIndicator, ...]:
        name = type(self.research_strategy).__name__
        try:
            declared = list(self.research_strategy.chart_indicators())
        except Exception as exc:
            raise RuntimeError(f"Error in {name}.chart_indicators: {exc}") from exc

        seen: set[str] = set()
        for entry in declared:
            if not isinstance(entry, ChartIndicator):
                raise ValueError(f"{name}.chart_indicators returned {entry!r}, expected ChartIndicator")
            column = entry.column
            if column in seen:
                raise ValueError(f"{name}.chart_indicators declares column {column!r} more than once")
            seen.add(column)
            if column in _MARKET_COLUMNS:
                raise ValueError(f"{name}.chart_indicators declares market column {column!r}")
            if column not in augmented.columns:
                raise ValueError(f"{name}.chart_indicators declares column {column!r} missing from compute_indicators")
            if not is_numeric_dtype(augmented[column]):
                raise ValueError(f"{name}.chart_indicators declares non-numeric column {column!r}")
        return tuple(declared)

    def get_chart_indicators(self) -> list[ChartIndicatorSpec]:
        return [
            ChartIndicatorSpec(
                key=indicator.column,
                label=indicator.label,
                pane=indicator.pane,
                color=indicator.color,
                line_style=indicator.line_style,
                line_width=indicator.line_width,
            )
            for indicator in self._chart_indicators
        ]
