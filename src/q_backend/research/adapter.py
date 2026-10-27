"""Private adapter bridging ResearchStrategy to candle TradingStrategy."""

from __future__ import annotations

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


class _RuntimeHooks:
    """Per-chunk decisions for the candle kernel's runtime callbacks.

    Each bar calls ``strategy`` (``exit_strategy``) after intrabar fills, then ``entry``
    (``entry_strategy``) once the bar's priced closes have resolved, and returns each one's kernel
    decision ``dict``. ``entry_filled`` runs after a same-bar priced entry fills and returns
    whether to close at the next bar's open.
    Records are indexed by the bar that decided them, one item per order of a list; the ledger's
    ``entry_candidate`` and ``exit_candidate`` name the order that filled.
    """

    def __init__(self, adapter: ResearchStrategyAdapter, chunk: pd.DataFrame, history_positions: np.ndarray) -> None:
        self._adapter = adapter
        self._chunk = chunk
        self._history_positions = history_positions
        self._progress = adapter.progress
        # Per bar: one (stop, target, price, reason) per entry order, NaN where unset.
        self.entry_orders = np.full(len(chunk), None, dtype=object)
        # Per bar: the priced closes' prices and reasons, in list order.
        self.exit_prices = np.full(len(chunk), None, dtype=object)
        self.exit_reasons = np.full(len(chunk), None, dtype=object)
        # A close without a price, for the next bar's open.
        self.close_reason = np.full(len(chunk), None, dtype=object)
        self.close_requested = np.zeros(len(chunk), dtype=bool)
        # A close requested right after a same-bar priced entry filled, for the next bar's open.
        self.fill_close_requested = np.zeros(len(chunk), dtype=bool)
        self.fill_close_reason = np.full(len(chunk), None, dtype=object)

    def _hook(self, name: str, bar: int, positions: tuple[ResearchPosition, ...]) -> tuple[TradeOrder, ...]:
        adapter = self._adapter
        position = int(self._history_positions[bar])
        return adapter._invoke_hook(
            name,
            _prefix(adapter._decision_frame, position + 1),
            positions,
            ticks=adapter._ticks_for(name, bar_position=position),
        )

    def strategy(self, bar: int, raw_positions: tuple) -> dict[str, Any]:
        """The bar's exits: closes, priced closes the tape chooses among, and an amend."""
        adapter = self._adapter
        positions = self._positions(raw_positions)
        timestamp = self._chunk.index[bar]
        orders = self._hook("exit_strategy", bar, positions)
        decision: dict[str, Any] = {}
        priced = [order for order in orders if order.action == "close" and order.price is not None]
        plain = [order for order in orders if order.action == "close" and order.price is None]
        if priced or plain:
            decision.update(exit_long=True, exit_short=True)
        if priced:
            adapter._require_hook_ticks(
                "exit_strategy", timestamp, "returned a priced close, which is confirmed from ticks"
            )
            prices = [float(order.price) for order in priced]
            decision["exit_prices"] = prices
            self.exit_prices[bar] = tuple(prices)
            self.exit_reasons[bar] = tuple(order.reason for order in priced)
            if plain:
                decision["close_next_open"] = True
        # A close with no position open does nothing, so it takes no reason.
        if plain and positions:
            self.close_requested[bar] = True
            self.close_reason[bar] = plain[0].reason
        for amend in (order for order in orders if order.action == "amend"):
            adapter._require_ticks(
                "amend", timestamp, "moves stop or take-profit levels, which are confirmed from ticks"
            )
            decision.update(amend_stop=amend.stop_loss, amend_target=amend.take_profit)
        self._progress.bar(bar + 1, timestamp, _position_label(positions))
        return decision

    def entry(self, bar: int, raw_positions: tuple) -> dict[str, Any]:
        """The bar's entry, decided with the positions left after its priced closes."""
        adapter = self._adapter
        timestamp = self._chunk.index[bar]
        orders = self._hook("entry_strategy", bar, self._positions(raw_positions))
        if not orders:
            return {}
        if orders[0].action == "cancel":
            return {"cancel_entry": True}
        records = []
        for order in orders:
            stop, target, price = adapter._levels_of(order, timestamp)
            records.append((stop, target, price if order.kind == "market" else np.nan, order.reason))
        self.entry_orders[bar] = tuple(records)
        first = orders[0]
        decision: dict[str, Any] = {"entry": 1 if first.action == "buy" else -1, "strength": 1.0}
        stop, target, _price, _reason = records[0]
        if first.kind != "market":
            decision.update(
                stop_price=stop,
                target_price=target,
                entry_price=float(first.price),
                entry_kind=first.kind,
                expire_bars=first.expire_bars,
            )
        elif first.price is None:
            decision.update(stop_price=stop, target_price=target)
        else:
            decision["entry_candidates"] = [
                (1 if order.action == "buy" else -1, price, stop, target)
                for order, (stop, target, price, _reason) in zip(orders, records, strict=True)
            ]
        return decision

    def entry_filled(self, bar: int, raw_positions: tuple) -> bool:
        """Ask ``exit_strategy`` again once a same-bar priced entry has filled.

        The bar's first call ran before the fill, so this is the only one that sees the new
        position on its entry bar. A plain close exits at the next bar's open, ahead of that
        bar's stops and targets.
        """
        orders = self._hook("exit_strategy", bar, self._positions(raw_positions))
        if not orders:
            return False
        if len(orders) > 1 or orders[0].action != "close" or orders[0].price is not None:
            raise ValueError(
                f"{type(self._adapter.research_strategy).__name__}.exit_strategy at {self._chunk.index[bar]} "
                f"returned {orders!r} for a position its priced entry opened on this bar; only "
                "TradeOrder.close() without a price, which exits at the next bar's open, is supported there"
            )
        self.fill_close_requested[bar] = True
        self.fill_close_reason[bar] = orders[0].reason
        return True

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
        self._decision_frame: pd.DataFrame | None = None

    def __getstate__(self) -> dict[str, Any]:
        """Session worker processes get a silent progress reporter; the parent's may drive a terminal."""
        state = self.__dict__.copy()
        state["progress"] = BacktestProgress()
        return state

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
        history = augmented.copy()
        history.attrs = {}
        history.index = _owned_index(augmented.index)
        self._decision_frame = history
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

    def _require_hook_ticks(self, hook: str, timestamp: pd.Timestamp, reason: str) -> None:
        if self._tick_source is None:
            raise ValueError(
                f"{type(self.research_strategy).__name__}.{hook} at bar {timestamp} {reason}; "
                "pass ticks= to backtest()"
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
        ticks: TickData | None = None,
    ) -> tuple[TradeOrder, ...]:
        return invoke_hook(self.research_strategy, self._hook_calls[name], name, prefix, positions, ticks=ticks)

    def _ticks_for(
        self,
        hook: str,
        *,
        bar_position: int,
        index: pd.DatetimeIndex | None = None,
    ) -> TickData | None:
        """A ``TickData`` for one hook call, or None when the run has no store or the hook takes no ticks.

        ``bar_position`` is the bar's position in ``index``, the prepared history by default.
        """
        if self._tick_source is None or self._ticks_modes[hook] == "none":
            return None
        history = self._decision_frame.index if index is None else index
        return TickData(self._tick_source, history, bar_position)

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
