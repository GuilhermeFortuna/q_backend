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
    write_signal_columns,
)
from q_backend.backtesting.strategy import ChartIndicatorSpec, TradingStrategy
from q_backend.research.charting import ChartIndicator, indicator_style
from q_backend.research.frame import FRAME_COLUMNS
from q_backend.research.hooks import detect_hook_call, invoke_hook, ticks_mode
from q_backend.research.orders import TradeOrder
from q_backend.research.positions import ResearchPosition
from q_backend.research.progress import BacktestProgress
from q_backend.research.strategy import ResearchStrategy
from q_backend.research.tick_data import TickData, TickSource
from q_backend.research.tick_phase import FormingRow, forming_compatible

_RESERVED_COLUMNS = frozenset((*SIGNAL_COLUMNS, *LEVEL_COLUMNS, BAR_INDEX))
_MARKET_COLUMNS = ("open", "high", "low", "close")


def _owned_index(index: pd.DatetimeIndex) -> pd.DatetimeIndex:
    """A private copy of the timestamps, read-only, so a hook cannot change the timestamps of later frames."""
    owned = index.copy(deep=True)
    owned.values.setflags(write=False)
    return owned


def _prefix(frame: pd.DataFrame, end: int) -> pd.DataFrame:
    """Positional slice of the prepared history through ``end``, with no attributes.

    A hook that writes to it copies the columns it writes (copy-on-write), so the history and
    later frames are unaffected.
    """
    prefix = frame.iloc[:end]
    prefix.attrs = {}
    return prefix


def _level(value: float) -> float | None:
    return None if np.isnan(value) else float(value)


def _same_positions(left: tuple, right: tuple) -> bool:
    """Whether two position snapshots match, with unset (NaN) levels equal to each other."""
    if len(left) != len(right):
        return False
    return all(
        a == b or (a != a and b != b) for left_row, right_row in zip(left, right) for a, b in zip(left_row, right_row)
    )


@dataclass
class _Candle:
    """A candle replayed tick by tick: its prices so far, and the positions it was last decided for."""

    bar: int
    open: float
    high: float
    low: float
    price: float
    seen: int = 0
    decided: tuple | None = None


class _RuntimeHooks:
    """Per-chunk decisions for the candle kernel's runtime callbacks.

    ``strategy`` runs once per bar after intrabar fills and returns the kernel's decision
    ``dict``. ``screen`` and ``tick`` exist only for a phase-aware exit and are None otherwise.
    Recorded levels are indexed by the bar that requested the entry. The kernel's decision
    trace identifies which requests passed the entry gates.
    """

    def __init__(self, adapter: ResearchStrategyAdapter, chunk: pd.DataFrame, history_positions: np.ndarray) -> None:
        self._adapter = adapter
        self._chunk = chunk
        self._history_positions = history_positions
        self._progress = adapter.progress
        self.stop_price = np.full(len(chunk), np.nan)
        self.target_price = np.full(len(chunk), np.nan)
        self.entry_price = np.full(len(chunk), np.nan)
        self._candle: _Candle | None = None
        self.screen: Callable[[int, tuple], bool] | None = self._screen if adapter._phased_exit else None
        self.tick: Callable[[int, int, int, float, tuple], bool] | None = self._tick if adapter._phased_exit else None

    def strategy(self, bar: int, raw_positions: tuple) -> dict[str, Any]:
        adapter = self._adapter
        self._restore_forming_row()
        positions = self._positions(raw_positions)
        position = int(self._history_positions[bar])
        end = position + 1
        timestamp = self._chunk.index[bar]
        phase = "bar" if adapter._phased_exit else None
        exit_decision = adapter._invoke_hook(
            "exit_strategy",
            _prefix(adapter._decision_frame, end),
            positions,
            phase=phase,
            ticks=adapter._ticks_for("exit_strategy", bar_position=position),
        )
        entry_decision = adapter._invoke_hook(
            "entry_strategy",
            _prefix(adapter._decision_frame, end),
            positions,
            ticks=adapter._ticks_for("entry_strategy", bar_position=position),
        )
        decision: dict[str, Any] = {}
        if exit_decision is not None:
            if exit_decision.action == "close":
                decision.update(exit_long=True, exit_short=True)
            else:
                adapter._require_ticks(
                    "amend", timestamp, "moves stop or take-profit levels, which are confirmed from ticks"
                )
                decision.update(amend_stop=exit_decision.stop_loss, amend_target=exit_decision.take_profit)
        if entry_decision is not None and entry_decision.action == "cancel":
            decision["cancel_entry"] = True
        elif entry_decision is not None:
            stop, target, price = adapter._levels_of(entry_decision, timestamp)
            self.stop_price[bar] = stop
            self.target_price[bar] = target
            self.entry_price[bar] = price if entry_decision.kind == "market" else np.nan
            decision.update(
                entry=1 if entry_decision.action == "buy" else -1,
                strength=1.0,
                stop_price=stop,
                target_price=target,
                entry_price=price,
            )
            if entry_decision.kind != "market":
                decision.update(entry_kind=entry_decision.kind, expire_bars=entry_decision.expire_bars)
        self._progress.bar(bar + 1, timestamp, _position_label(positions))
        return decision

    def _screen(self, bar: int, raw_positions: tuple) -> bool:
        adapter = self._adapter
        self._restore_forming_row()
        position = int(self._history_positions[bar])
        decision = adapter._invoke_hook(
            "exit_strategy",
            _prefix(adapter._decision_frame, position + 1),
            self._positions(raw_positions),
            phase="screen",
            # A screen sees the candle as it opens: its ticks are not yet known.
            ticks=adapter._ticks_for(
                "exit_strategy", bar_position=position, cutoff=adapter._decision_frame.index[position]
            ),
        )
        return decision is not None

    def _tick(self, bar: int, tick: int, time_us: int, price: float, raw_positions: tuple) -> bool:
        """Decide one tick of a screened candle, in order, from the candle's observed prices.

        A tick that leaves the candle's high, low and close where they were repeats the frame that
        last declined to exit, apart from its tick count, so it is not decided again unless the
        positions have changed since.
        """
        candle = self._candle
        if tick == 0:
            candle = self._candle = _Candle(bar=bar, open=price, high=price, low=price, price=price)
        if candle is None or candle.bar != bar or tick != candle.seen:
            raise RuntimeError(f"Tick replay for bar {bar} arrived out of order at tick {tick}")
        moved = tick == 0 or price != candle.price
        candle.seen += 1
        candle.high = max(candle.high, price)
        candle.low = min(candle.low, price)
        candle.price = price
        # Keep observing a screened scan while a resting order waits, but user exit hooks
        # still run only with an open position.
        if not raw_positions:
            return False
        if not moved and candle.decided is not None and _same_positions(raw_positions, candle.decided):
            return False
        candle.decided = raw_positions
        adapter = self._adapter
        frame = adapter._forming.frame(
            int(self._history_positions[bar]), (candle.open, candle.high, candle.low, price), tick + 1
        )
        self._progress.replay(self._chunk.index[bar], tick + 1)
        decision = invoke_hook(
            adapter.research_strategy,
            adapter._hook_calls["exit_strategy"],
            "exit_strategy",
            frame,
            self._positions(raw_positions),
            phase="tick",
            detail=f" tick {tick}",
            ticks=adapter._ticks_for("exit_strategy", bar_position=int(self._history_positions[bar]), tick=tick),
        )
        return decision is not None

    def _restore_forming_row(self) -> None:
        if self._adapter._forming is not None:
            self._adapter._forming.restore()

    def _positions(self, raw_positions: tuple) -> tuple[ResearchPosition, ...]:
        chunk = self._chunk
        return tuple(
            ResearchPosition(
                symbol=self._adapter.symbol,
                side="long" if side == 1 else "short",
                entry_time=chunk.index[entry_bar],
                entry_price=price,
                quantity=quantity,
                stop_loss=_level(stop),
                take_profit=_level(target),
            )
            for _ordinal, side, entry_bar, price, quantity, stop, target in raw_positions
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
        tick_source: TickSource | None = None,
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
        self._tick_source = tick_source
        self.progress = progress if progress is not None else BacktestProgress()
        self._hook_calls = {
            name: detect_hook_call(research_strategy, name) for name in ("exit_strategy", "entry_strategy")
        }
        self._ticks_modes = {
            "compute_indicators": ticks_mode(research_strategy.compute_indicators),
            "entry_strategy": self._hook_calls["entry_strategy"].ticks,
            "exit_strategy": self._hook_calls["exit_strategy"].ticks,
        }
        if tick_source is None:
            for name, mode in self._ticks_modes.items():
                if mode == "required":
                    raise ValueError(
                        f"{type(research_strategy).__name__}.{name} has a required ticks parameter; "
                        "pass a TickStore as ticks= to backtest()"
                    )
        self._phased_exit = self._hook_calls["exit_strategy"].phase
        self._decision_frame: pd.DataFrame | None = None
        self._forming: FormingRow | None = None

    def __getstate__(self) -> dict[str, Any]:
        """Session worker processes get a silent progress reporter; the parent's may drive a terminal."""
        state = self.__dict__.copy()
        state["progress"] = BacktestProgress()
        return state

    def close(self) -> None:
        """Put back a forming row that a tick decision left showing in the prepared history."""
        if self._forming is not None:
            self._forming.restore()

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

        ticks = self._ticks_for("compute_indicators", index=data_copy.index, bar_position=orig_len - 1)
        kwargs = {} if ticks is None else {"ticks": ticks}
        try:
            augmented = self.research_strategy.compute_indicators(data_copy, **kwargs)
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

        # Decisions are taken by the kernel's runtime callbacks, once per bar, through runtime_callback.
        # A phase-aware exit shows forming candles in this history, so its columns must hold missing values.
        history = augmented.copy()
        history.attrs = {}
        history.index = _owned_index(augmented.index)
        if self._phased_exit:
            history = forming_compatible(history)
        self._decision_frame = history
        self._forming = FormingRow(history) if self._phased_exit else None
        self.progress.start(orig_len)
        return write_signal_columns(
            augmented,
            entry_long=pd.Series(np.zeros(orig_len, dtype=bool), index=augmented.index, dtype=bool),
            entry_short=pd.Series(np.zeros(orig_len, dtype=bool), index=augmented.index, dtype=bool),
            exit_long=pd.Series(np.zeros(orig_len, dtype=bool), index=augmented.index, dtype=bool),
            exit_short=pd.Series(np.zeros(orig_len, dtype=bool), index=augmented.index, dtype=bool),
            strategy_name=type(self.research_strategy).__name__,
        )

    def _levels_of(self, decision: TradeOrder, timestamp: pd.Timestamp) -> tuple[float, float, float]:
        """Entry levels and price as floats, NaN where unset; levels and resting orders are confirmed from ticks."""
        stop = np.nan if decision.stop_loss is None else float(decision.stop_loss)
        target = np.nan if decision.take_profit is None else float(decision.take_profit)
        price = np.nan if decision.price is None else float(decision.price)
        if decision.stop_loss is not None or decision.take_profit is not None:
            self._require_ticks(
                "entry", timestamp, "carries stop or take-profit levels, which are confirmed from ticks"
            )
        if decision.kind != "market":
            self._require_ticks("entry", timestamp, f"rests a {decision.kind} order, which fills from ticks")
        return stop, target, price

    def _require_ticks(self, action: str, timestamp: pd.Timestamp, reason: str) -> None:
        if self._tick_source is None:
            raise ValueError(
                f"{type(self.research_strategy).__name__} {action} at bar {timestamp} {reason}; pass ticks= to backtest()"
            )

    def runtime_callback(self, chunk: pd.DataFrame) -> _RuntimeHooks:
        """Bind one prepared chunk to the prepared indicator history."""
        history = self._decision_frame
        if history is None:
            raise RuntimeError("Research indicators must be prepared before runtime decisions")
        history_positions = history.index.get_indexer(chunk.index)
        if (history_positions < 0).any():
            raise ValueError("Runtime chunk is not part of the prepared research frame")
        return _RuntimeHooks(self, chunk, history_positions)

    def _invoke_hook(
        self,
        name: str,
        prefix: pd.DataFrame,
        positions: tuple[ResearchPosition, ...],
        *,
        phase: str | None = None,
        ticks: TickData | None = None,
    ) -> TradeOrder | None:
        return invoke_hook(
            self.research_strategy, self._hook_calls[name], name, prefix, positions, phase=phase, ticks=ticks
        )

    def _ticks_for(
        self,
        hook: str,
        *,
        bar_position: int,
        index: pd.DatetimeIndex | None = None,
        cutoff: pd.Timestamp | None = None,
        tick: int | None = None,
    ) -> TickData | None:
        """A ``TickData`` for one hook call, or None when the run has no store or the hook takes no ticks.

        ``bar_position`` is the bar's position in ``index``, the prepared history by default.
        """
        if self._tick_source is None or self._ticks_modes[hook] == "none":
            return None
        history = self._decision_frame.index if index is None else index
        return TickData(self._tick_source, history, bar_position, cutoff=cutoff, tick=tick)

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
            if entry.start_column is not None:
                start = entry.start_column
                if start in _MARKET_COLUMNS or start not in augmented.columns:
                    raise ValueError(
                        f"{name}.chart_indicators declares start_column {start!r} missing from compute_indicators"
                    )
        declared_by_column = {entry.column: entry for entry in declared}
        for entry in declared:
            if entry.fill_to is None:
                continue
            target = declared_by_column.get(entry.fill_to)
            if entry.fill_to == entry.column:
                raise ValueError(f"{name}.chart_indicators: {entry.column!r} cannot fill to itself")
            if target is None or target.pane != entry.pane or target.kind not in {"line", "step"}:
                raise ValueError(
                    f"{name}.chart_indicators: {entry.column!r} fill_to {entry.fill_to!r} must name another "
                    "declared line or step series in the same pane"
                )
        return tuple(declared)

    def get_chart_indicators(self) -> list[ChartIndicatorSpec]:
        return [
            ChartIndicatorSpec(
                key=indicator.column,
                label=indicator.label,
                pane=indicator.pane,
                **indicator_style(indicator),
            )
            for indicator in self._chart_indicators
        ]
