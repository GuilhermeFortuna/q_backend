"""Incremental closed-bar strategy evaluator for forward execution.

Evaluator input/output contract (for WO170)
-----------------------------------------

**Inputs per deployment**

- ``StrategyIdentity``: immutable ``strategy_name``, ``strategy_version``,
  ``compiled_config`` (``BacktestRequest`` JSON), ``config_hash``, ``symbol``,
  ``timeframe``, ``sizing_config``, ``risk_config``.
- Rolling OHLCV window: completed bars only, bounded by
  ``compute_window_bound_bars(compiled_config)``.
- Optional open ``Trade`` view from ``execution_position_to_trade`` for exit rules.
- ``last_evaluated_close`` watermark for idempotent delivery.

**Outputs per completed bar**

- ``ForwardDecisionResult`` with: ``bar_close_time``, ``bar_close_price``,
  ``signal_action`` (``hold`` / ``buy`` / ``sell`` / ``close``), ``reason``,
  ``requested_quantity``, ``sizing_inputs``, strategy identity fields,
  queued entry/exit signal payloads, ``timing`` (``indicators_ms``,
  ``evaluate_ms``, ``sizing_ms``, ``total_ms``), ``replay`` and
  ``skipped_duplicate`` flags.

Queued signals mirror ``BacktestEngine`` section D (evaluate on bar close, execute
next bar). Recovery replay sets ``replay=True`` and ``emits_decision=False``.

Warm-up / window bound: see ``execution.warmup.compute_window_bound_bars``.
"""

from __future__ import annotations

import time
from datetime import datetime
from typing import Any, Optional

import numpy as np
import pandas as pd

from q_backend.backtesting.models import Signal, SignalAction as BacktestSignalAction, Trade
from pydantic import TypeAdapter

from q_backend.backtesting.position_sizing import PositionSizingConfig, build_position_sizer
from q_backend.backtesting.signal_columns import SignalArrays
from q_backend.backtesting.strategy import TradingStrategy
from q_backend.execution.bars import bar_close_time, frame_close_times, trim_rolling_window
from q_backend.execution.domain import SignalAction, StrategyIdentity
from q_backend.execution.indicator_frame import augment_indicator_frame
from q_backend.execution.position_adapter import execution_position_to_trade
from q_backend.execution.results import EvaluationPhaseTiming, ForwardDecisionResult
from q_backend.execution.signal_eval import evaluate_queued_signals, signal_arrays
from q_backend.execution.strategy_build import build_strategy_from_compiled
from q_backend.execution.warmup import compute_window_bound_bars


def _signal_to_dict(signal: Signal) -> dict[str, Any]:
    payload = signal.model_dump(mode="json")
    if signal.action == BacktestSignalAction.CLOSE:
        payload["exit_reason"] = getattr(signal, "exit_reason", None)
    return payload


def _domain_action_from_queued(
    pending_exits: list[Signal],
    pending_entries: list[Signal],
) -> tuple[SignalAction, Optional[str]]:
    if pending_exits:
        first = pending_exits[0]
        reason = getattr(first, "exit_reason", None) or "exit_rule"
        return SignalAction.CLOSE, reason
    if pending_entries:
        for signal in pending_entries:
            if signal.action == BacktestSignalAction.BUY:
                return SignalAction.BUY, "entry"
            if signal.action == BacktestSignalAction.SELL:
                return SignalAction.SELL, "entry"
    return SignalAction.HOLD, None


_POSITION_SIZING_ADAPTER = TypeAdapter(PositionSizingConfig)


def _map_position_sizing_config(raw: dict[str, Any]) -> Optional[PositionSizingConfig]:
    if not raw:
        return None
    if "type" in raw:
        return _POSITION_SIZING_ADAPTER.validate_python(raw)
    # Legacy execution tests may store a simplified shape.
    if "quantity" in raw:
        return _POSITION_SIZING_ADAPTER.validate_python({"type": "fixed_quantity", "quantity": float(raw["quantity"])})
    return None


class StrategyEvaluator:
    """Evaluate one immutable strategy on newly completed bars with bounded work."""

    def __init__(
        self,
        *,
        deployment_id: str,
        identity: StrategyIdentity,
        initial_capital: float = 100_000.0,
        point_value: float = 1.0,
        open_trade: Optional[Trade] = None,
        last_evaluated_close: Optional[datetime] = None,
        strategy: Optional[TradingStrategy] = None,
        window_bound: Optional[int] = None,
    ) -> None:
        self.deployment_id = deployment_id
        self.identity = identity
        self.symbol = identity.symbol
        self.timeframe = identity.timeframe.upper()
        self.initial_capital = initial_capital
        self.point_value = point_value
        self.window_bound = window_bound or compute_window_bound_bars(identity.compiled_config)
        self.strategy = strategy or build_strategy_from_compiled(
            identity.compiled_config,
            symbol=identity.symbol,
        )
        sizing_config = _map_position_sizing_config(identity.sizing_config)
        self.sizer = build_position_sizer(sizing_config, point_value=point_value)
        self._rolling = pd.DataFrame()
        self._open_trade = open_trade
        self._last_evaluated_close = last_evaluated_close
        self._replay_mode = False

    @classmethod
    def from_execution_position(
        cls,
        *,
        deployment_id: str,
        identity: StrategyIdentity,
        position_side,
        position_quantity,
        average_entry_price,
        opened_at,
        initial_capital: float = 100_000.0,
        point_value: float = 1.0,
        last_evaluated_close: Optional[datetime] = None,
    ) -> StrategyEvaluator:
        trade = execution_position_to_trade(
            deployment_id=deployment_id,
            symbol=identity.symbol,
            side=position_side,
            quantity=position_quantity,
            average_entry_price=average_entry_price,
            opened_at=opened_at,
            point_value=point_value,
        )
        return cls(
            deployment_id=deployment_id,
            identity=identity,
            initial_capital=initial_capital,
            point_value=point_value,
            open_trade=trade,
            last_evaluated_close=last_evaluated_close,
        )

    def set_open_trade(self, trade: Optional[Trade]) -> None:
        self._open_trade = trade

    @property
    def last_evaluated_close(self) -> Optional[datetime]:
        return self._last_evaluated_close

    @property
    def rolling_bar_count(self) -> int:
        return len(self._rolling)

    def seed_window(self, frame: pd.DataFrame) -> None:
        """Load an initial bounded history window (restart / first poll)."""
        ordered = frame.sort_index()
        self._rolling = trim_rolling_window(ordered, self.window_bound)

    def replay_recovery(
        self,
        frame: pd.DataFrame,
        *,
        end_close: Optional[datetime] = None,
    ) -> list[ForwardDecisionResult]:
        """Reconstruct stateful exit state without emitting decisions or orders."""
        self._replay_mode = True
        try:
            return self.ingest_completed_bars(frame, through_close=end_close)
        finally:
            self._replay_mode = False

    def ingest_completed_bars(
        self,
        frame: pd.DataFrame,
        *,
        through_close: Optional[datetime] = None,
    ) -> list[ForwardDecisionResult]:
        if frame.empty:
            return []

        ordered = frame.sort_index()
        merged = pd.concat([self._rolling, ordered]) if not self._rolling.empty else ordered
        merged = merged[~merged.index.duplicated(keep="last")].sort_index()
        self._rolling = trim_rolling_window(merged, self.window_bound)

        closes = frame_close_times(ordered.index, self.timeframe)
        results: list[ForwardDecisionResult] = []
        for open_time, close_ts in zip(ordered.index, closes):
            close_time = close_ts.to_pydatetime()
            if through_close is not None and close_time > through_close:
                break
            if self._last_evaluated_close is not None and close_time < self._last_evaluated_close:
                continue
            indicators_started = time.perf_counter()
            augmented = self._augmented_frame()
            indicators_ms = (time.perf_counter() - indicators_started) * 1000.0
            signals = signal_arrays(self.strategy, augmented)
            matches = np.flatnonzero(augmented.index == open_time)
            if len(matches) == 0:
                continue
            position = int(matches[-1])
            row = augmented.iloc[position]
            results.append(
                self._evaluate_row(
                    row,
                    close_time,
                    signals=signals,
                    position=position,
                    indicators_ms=indicators_ms,
                )
            )
        return results

    def _augmented_frame(self) -> pd.DataFrame:
        return augment_indicator_frame(self.strategy, self._rolling)

    def _evaluate_row(
        self,
        current_data: pd.Series,
        bar_close_time_value: datetime,
        *,
        signals: SignalArrays,
        position: int,
        indicators_ms: float = 0.0,
    ) -> ForwardDecisionResult:
        started = time.perf_counter()
        duplicate = (
            not self._replay_mode
            and self._last_evaluated_close is not None
            and bar_close_time_value == self._last_evaluated_close
        )

        evaluate_started = time.perf_counter()
        open_trades = [self._open_trade] if self._open_trade is not None else []
        pending_exits, pending_entries = evaluate_queued_signals(
            self.strategy,
            signals,
            position,
            current_data,
            open_trades,
        )
        evaluate_ms = (time.perf_counter() - evaluate_started) * 1000.0

        signal_action, reason = _domain_action_from_queued(pending_exits, pending_entries)
        close_price = float(current_data.get("close", 0.0))

        sizing_started = time.perf_counter()
        requested_quantity: Optional[float] = None
        sizing_inputs: dict[str, Any] = {
            "capital": self.initial_capital,
            "reference_price": close_price,
            "point_value": self.point_value,
        }
        if signal_action in {SignalAction.BUY, SignalAction.SELL} and pending_entries:
            entry_signal = pending_entries[0]
            order = self.sizer.size_signal(
                entry_signal,
                close_price,
                self.initial_capital,
                current_data=current_data,
            )
            if order is not None:
                requested_quantity = float(order.quantity)
                sizing_inputs["order_quantity"] = requested_quantity
        sizing_ms = (time.perf_counter() - sizing_started) * 1000.0

        total_ms = (time.perf_counter() - started) * 1000.0
        if not self._replay_mode and not duplicate:
            self._last_evaluated_close = bar_close_time_value

        return ForwardDecisionResult(
            deployment_id=self.deployment_id,
            bar_close_time=bar_close_time_value,
            bar_close_price=close_price,
            signal_action=signal_action,
            reason=reason,
            requested_quantity=requested_quantity,
            sizing_inputs=sizing_inputs,
            strategy_name=self.identity.strategy_name,
            strategy_version=self.identity.strategy_version,
            config_hash=self.identity.config_hash,
            symbol=self.symbol,
            timeframe=self.timeframe,
            queued_exit_signals=tuple(_signal_to_dict(s) for s in pending_exits),
            queued_entry_signals=tuple(_signal_to_dict(s) for s in pending_entries),
            timing=EvaluationPhaseTiming(
                indicators_ms=indicators_ms,
                evaluate_ms=evaluate_ms,
                sizing_ms=sizing_ms,
                total_ms=total_ms,
            ),
            replay=self._replay_mode,
            skipped_duplicate=duplicate,
        )
