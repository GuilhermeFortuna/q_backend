"""Incremental research strategy evaluator for forward execution."""

from __future__ import annotations

import datetime as dt
import time
from datetime import datetime, timezone
from typing import Any, Optional

import numpy as np
import pandas as pd

from q_backend.backtesting.models import OrderAction, SignalAction as BacktestSignalAction, Trade
from q_backend.backtesting.position_sizing import PositionSizingConfig, build_position_sizer
from q_backend.execution.bars import frame_close_times, trim_rolling_window
from q_backend.execution.domain import SignalAction, StrategyIdentity
from q_backend.execution.position_adapter import execution_position_to_trade
from q_backend.execution.results import EvaluationPhaseTiming, ForwardDecisionResult
from q_backend.execution.strategy_build import build_research_strategy_from_compiled
from q_backend.execution.warmup import compute_window_bound_bars
from q_backend.market_data.timezone import BRASILIA_TZ
from q_backend.research.adapter import ResearchStrategyAdapter, classify_order
from q_backend.research.positions import ResearchPosition
from q_backend.research.strategy import ResearchStrategy


def _utc_aware(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _prefix(frame: pd.DataFrame, end: int) -> pd.DataFrame:
    prefix = frame.iloc[:end]
    prefix.attrs = {}
    return prefix


class ResearchEvaluator:
    """Forward evaluator for frozen research strategy versions."""

    def __init__(
        self,
        *,
        deployment_id: str,
        identity: StrategyIdentity,
        initial_capital: float = 100_000.0,
        point_value: float = 1.0,
        open_trade: Optional[Trade] = None,
        last_evaluated_close: Optional[datetime] = None,
        strategy_instance: Optional[ResearchStrategy] = None,
        window_bound: Optional[int] = None,
    ) -> None:
        self.deployment_id = deployment_id
        self.identity = identity
        self.symbol = identity.symbol
        self.timeframe = identity.timeframe.upper()
        self.initial_capital = initial_capital
        self.point_value = point_value
        self.window_bound = window_bound or compute_window_bound_bars(identity.compiled_config)

        if strategy_instance is not None:
            self.strategy = strategy_instance
        else:
            self.strategy = build_research_strategy_from_compiled(identity.compiled_config)

        self.exit_params = dict(identity.compiled_config.get("exit_params", {}))
        self._adapter = ResearchStrategyAdapter(self.strategy, symbol=self.symbol, exit_params=self.exit_params)

        cfg = identity.compiled_config
        self._day_trade = bool(cfg.get("day_trade", True))
        self._day_trade_start_time = str(cfg.get("day_trade_start_time", "09:00"))
        self._day_trade_end_time = str(cfg.get("day_trade_end_time", "16:00"))
        self._day_trade_close_time = str(cfg.get("day_trade_close_time", "17:00"))

        sizing_cfg = identity.sizing_config
        qty = 1.0
        if sizing_cfg:
            qty = float(sizing_cfg.get("quantity", 1.0))
        self._quantity = qty

        self._rolling = pd.DataFrame()
        self._open_trade = open_trade
        self._last_evaluated_close = _utc_aware(last_evaluated_close) if last_evaluated_close is not None else None
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
    ) -> ResearchEvaluator:
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
        """Load an initial bounded history window."""
        ordered = frame.sort_index()
        self._rolling = trim_rolling_window(ordered, self.window_bound)

    def replay_recovery(
        self,
        frame: pd.DataFrame,
        *,
        end_close: Optional[datetime] = None,
    ) -> list[ForwardDecisionResult]:
        """Replay history during recovery without emitting decisions."""
        self._replay_mode = True
        try:
            return self.ingest_completed_bars(frame, through_close=end_close)
        finally:
            self._replay_mode = False

    def ingest_completed_bar(self, bar: pd.DataFrame) -> Optional[ForwardDecisionResult]:
        results = self.ingest_completed_bars(bar)
        if not results:
            return None
        return results[-1]

    def ingest_completed_bars(
        self,
        frame: pd.DataFrame,
        *,
        through_close: Optional[datetime] = None,
    ) -> list[ForwardDecisionResult]:
        if frame.empty:
            return []

        replay_through = _utc_aware(through_close) if through_close is not None else None
        ordered = frame.sort_index()
        results: list[ForwardDecisionResult] = []

        for open_time in ordered.index:
            single = ordered.loc[[open_time]]
            merged = pd.concat([self._rolling, single]) if not self._rolling.empty else single
            merged = merged[~merged.index.duplicated(keep="last")].sort_index()
            self._rolling = trim_rolling_window(merged, self.window_bound)

            close_time = _utc_aware(frame_close_times(single.index, self.timeframe)[0].to_pydatetime())
            if replay_through is not None and close_time > replay_through:
                break
            if self._last_evaluated_close is not None and close_time < _utc_aware(self._last_evaluated_close):
                continue

            results.append(self._evaluate_bar(single, close_time))

        return results

    def _positions_view(self) -> tuple[ResearchPosition, ...]:
        if self._open_trade is None:
            return ()
        trade = self._open_trade
        side = "long" if trade.action == OrderAction.BUY else "short"
        return (
            ResearchPosition(
                symbol=self.symbol,
                side=side,
                entry_time=pd.Timestamp(trade.entry_time),
                entry_price=float(trade.entry_price),
                quantity=float(trade.quantity),
                stop_loss=float(trade.stop_loss) if trade.stop_loss is not None else None,
                take_profit=float(trade.take_profit) if trade.take_profit is not None else None,
            ),
        )

    def _evaluate_bar(self, single: pd.DataFrame, bar_close_time_value: datetime) -> ForwardDecisionResult:
        started = time.perf_counter()
        duplicate = (
            not self._replay_mode
            and self._last_evaluated_close is not None
            and _utc_aware(bar_close_time_value) == _utc_aware(self._last_evaluated_close)
        )

        close_price = float(single["close"].iloc[-1])
        positions = self._positions_view()

        # Day-trade session times
        bar_local = pd.Timestamp(bar_close_time_value).tz_convert(BRASILIA_TZ)
        bar_time = bar_local.time()
        start_t = dt.time.fromisoformat(self._day_trade_start_time)
        end_t = dt.time.fromisoformat(self._day_trade_end_time)
        close_t = dt.time.fromisoformat(self._day_trade_close_time)

        signal_action = SignalAction.HOLD
        reason: Optional[str] = None
        pause_requested = False
        risk_rejection_code: Optional[str] = None
        risk_rejection_message: Optional[str] = None

        indicators_started = time.perf_counter()
        augmented = self._rolling
        try:
            augmented = self._adapter.compute_indicators(self._rolling.copy())
            self._adapter._decision_frame = augmented
        except Exception as exc:  # noqa: BLE001
            pause_requested = True
            risk_rejection_code = "hook_error"
            risk_rejection_message = f"compute_indicators error: {exc}"
        indicators_ms = (time.perf_counter() - indicators_started) * 1000.0

        evaluate_started = time.perf_counter()
        if not pause_requested:
            prefix = _prefix(augmented, len(augmented))

            # 1. Day trade close check
            if self._day_trade and positions:
                trade_entry_local = pd.Timestamp(positions[0].entry_time).tz_convert(BRASILIA_TZ)
                # If bar time is at/after session close time, or from a later day:
                if bar_local.date() > trade_entry_local.date() or bar_time >= close_t:
                    signal_action = SignalAction.CLOSE
                    reason = "day_trade_close"

            # 2. Exit strategy hook
            if signal_action == SignalAction.HOLD and positions:
                try:
                    exit_orders = self._adapter._invoke_hook("exit_strategy", prefix, positions)
                except Exception as exc:  # noqa: BLE001
                    pause_requested = True
                    risk_rejection_code = "hook_error"
                    risk_rejection_message = f"exit_strategy error: {exc}"
                    exit_orders = ()

                if exit_orders:
                    first = exit_orders[0]
                    kinds = classify_order(first)
                    if kinds - {"market_close"}:
                        pause_requested = True
                        risk_rejection_code = "unsupported_order_kind"
                        risk_rejection_message = f"Order kind {sorted(kinds)} is not deployable"
                    elif first.action == "close" and first.price is None:
                        signal_action = SignalAction.CLOSE
                        reason = first.reason or "exit_rule"

            # 3. Entry strategy hook
            if signal_action == SignalAction.HOLD and not positions and not pause_requested:
                if self._day_trade and (bar_time < start_t or bar_time > end_t):
                    # Refuse entry outside trading window
                    signal_action = SignalAction.HOLD
                else:
                    try:
                        entry_orders = self._adapter._invoke_hook("entry_strategy", prefix, positions)
                    except Exception as exc:  # noqa: BLE001
                        pause_requested = True
                        risk_rejection_code = "hook_error"
                        risk_rejection_message = f"entry_strategy error: {exc}"
                        entry_orders = ()

                    if entry_orders:
                        first = entry_orders[0]
                        kinds = classify_order(first)
                        if kinds - {"market_entry"}:
                            pause_requested = True
                            risk_rejection_code = "unsupported_order_kind"
                            risk_rejection_message = f"Order kind {sorted(kinds)} is not deployable"
                        elif first.action in ("buy", "sell") and first.kind == "market" and first.price is None:
                            signal_action = SignalAction.BUY if first.action == "buy" else SignalAction.SELL
                            reason = first.reason or "entry"

        evaluate_ms = (time.perf_counter() - evaluate_started) * 1000.0

        sizing_started = time.perf_counter()
        requested_quantity: Optional[float] = None
        sizing_inputs: dict[str, Any] = {
            "capital": self.initial_capital,
            "reference_price": close_price,
            "point_value": self.point_value,
        }
        if signal_action in {SignalAction.BUY, SignalAction.SELL} and not pause_requested:
            requested_quantity = self._quantity
            sizing_inputs["order_quantity"] = requested_quantity
        sizing_ms = (time.perf_counter() - sizing_started) * 1000.0

        total_ms = (time.perf_counter() - started) * 1000.0
        if not self._replay_mode and not duplicate:
            self._last_evaluated_close = bar_close_time_value

        queued_exits: list[dict[str, Any]] = []
        queued_entries: list[dict[str, Any]] = []
        if not pause_requested:
            if signal_action == SignalAction.CLOSE:
                queued_exits.append(
                    {
                        "action": BacktestSignalAction.CLOSE.value,
                        "symbol": self.symbol,
                        "exit_reason": reason,
                    }
                )
            elif signal_action == SignalAction.BUY:
                queued_entries.append(
                    {
                        "action": BacktestSignalAction.BUY.value,
                        "symbol": self.symbol,
                        "entry_reason": reason,
                    }
                )
            elif signal_action == SignalAction.SELL:
                queued_entries.append(
                    {
                        "action": BacktestSignalAction.SELL.value,
                        "symbol": self.symbol,
                        "entry_reason": reason,
                    }
                )

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
            queued_exit_signals=tuple(queued_exits),
            queued_entry_signals=tuple(queued_entries),
            timing=EvaluationPhaseTiming(
                indicators_ms=indicators_ms,
                evaluate_ms=evaluate_ms,
                sizing_ms=sizing_ms,
                total_ms=total_ms,
            ),
            replay=self._replay_mode,
            skipped_duplicate=duplicate,
            pause_requested=pause_requested,
            risk_rejection_code=risk_rejection_code,
            risk_rejection_message=risk_rejection_message,
        )
