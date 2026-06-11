from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Literal

import pandas as pd
from pydantic import BaseModel, Field

from q_backend.backtesting.models import Trade
from q_backend.optimization.backtest_runner import (
    BacktestRunConfig,
    BacktestRunner,
)
from q_backend.optimization.metrics import build_equity_curve, compute_extended_metrics
from q_backend.optimization.models import OptimizationConfig, StorageConfig
from q_backend.optimization.objectives import resolve_objective
from q_backend.optimization.runner import OptimizationRunner
from q_backend.optimization.search_space import build_position_sizing_config

ONE_DAY = timedelta(days=1)


class WalkForwardConfig(BaseModel):
    train_days: int = Field(ge=1)
    test_days: int = Field(ge=1)
    mode: Literal["rolling", "anchored"] = "rolling"
    min_windows: int = Field(default=2, ge=1)


@dataclass(frozen=True)
class WalkForwardWindow:
    index: int
    train_start: datetime
    train_end: datetime
    test_start: datetime
    test_end: datetime


@dataclass
class WalkForwardProgress:
    current_window: int
    total_windows: int
    phase: Literal["optimizing", "testing"]
    window_index: int


@dataclass
class WalkForwardWindowResult:
    index: int
    train_start: datetime
    train_end: datetime
    test_start: datetime
    test_end: datetime
    status: Literal["completed", "no_result"]
    best_params: dict[str, Any] = field(default_factory=dict)
    is_metrics: dict[str, Any] | None = None
    oos_metrics: dict[str, Any] | None = None
    oos_trades: list[Trade] = field(default_factory=list)


@dataclass
class WalkForwardResult:
    windows: list[WalkForwardWindowResult]
    oos_equity_curve: pd.Series
    oos_metrics: dict[str, Any]
    efficiency: float | None


def _window_duration(start: datetime, end: datetime) -> timedelta:
    return end - start


def split_windows(
    start: datetime, end: datetime, cfg: WalkForwardConfig
) -> list[WalkForwardWindow]:
    if start >= end:
        raise ValueError("start must be before end")

    train_delta = timedelta(days=cfg.train_days)
    test_delta = timedelta(days=cfg.test_days)
    windows: list[WalkForwardWindow] = []
    index = 0

    while True:
        test_start = start + train_delta + index * test_delta
        if test_start >= end:
            break

        test_end = min(test_start + test_delta, end)
        if _window_duration(test_start, test_end) < ONE_DAY:
            break

        if cfg.mode == "rolling":
            train_start = test_start - train_delta
            train_end = test_start
        else:
            train_start = start
            train_end = test_start

        if _window_duration(train_start, train_end) < ONE_DAY:
            break

        windows.append(
            WalkForwardWindow(
                index=index,
                train_start=train_start,
                train_end=train_end,
                test_start=test_start,
                test_end=test_end,
            )
        )
        index += 1

    if len(windows) < cfg.min_windows:
        raise ValueError(
            f"Date range yields {len(windows)} walk-forward window(s), "
            f"but min_windows is {cfg.min_windows}. "
            f"Try a longer history or smaller train_days/test_days."
        )

    return windows


class WalkForwardRunner:
    def __init__(
        self,
        config: OptimizationConfig,
        wf_config: WalkForwardConfig,
        backtest_runner: BacktestRunner,
    ):
        if config.is_multi_objective():
            raise ValueError(
                "Walk-forward analysis does not support multi-objective studies"
            )
        if config.backtest.engine == "tick":
            raise ValueError(
                "Walk-forward analysis supports candle engine only (engine='tick' "
                "is not supported yet)"
            )
        self.config = config
        self.wf_config = wf_config
        self.backtest_runner = backtest_runner

    def _build_backtest_config(
        self,
        *,
        start: datetime,
        end: datetime,
        strategy_params: dict[str, Any],
        risk_params: dict[str, Any],
    ) -> BacktestRunConfig:
        backtest = self.config.backtest
        position_sizing = build_position_sizing_config(risk_params)
        return BacktestRunConfig(
            symbol=backtest.symbol,
            timeframe=backtest.timeframe,
            start=start,
            end=end,
            initial_capital=backtest.initial_capital,
            point_value=backtest.point_value,
            strategy=backtest.strategy,
            strategy_params=strategy_params,
            position_sizing=position_sizing,
            costs=backtest.costs,
            parallel_mode=backtest.parallel_mode,
            day_trade=backtest.day_trade,
            day_trade_start_time=backtest.day_trade_start_time,
            day_trade_end_time=backtest.day_trade_end_time,
            day_trade_close_time=backtest.day_trade_close_time,
            engine=backtest.engine,
            display_timeframe=backtest.display_timeframe,
            tick_flags=backtest.tick_flags,
        )

    def _window_optimization_config(self, window: WalkForwardWindow) -> OptimizationConfig:
        window_config = self.config.model_copy(deep=True)
        window_config.backtest.start = window.train_start
        window_config.backtest.end = window.train_end
        window_config.study.name = f"{self.config.study.name}__w{window.index}"
        window_config.study.storage = StorageConfig(type="memory")
        return window_config

    def _notify(
        self,
        callback: Callable[[WalkForwardProgress], None] | None,
        *,
        window_index: int,
        total_windows: int,
        phase: Literal["optimizing", "testing"],
    ) -> None:
        if callback is None:
            return
        callback(
            WalkForwardProgress(
                current_window=window_index + 1,
                total_windows=total_windows,
                phase=phase,
                window_index=window_index,
            )
        )

    def run(
        self,
        progress_callback: Callable[[WalkForwardProgress], None] | None = None,
        should_stop: Callable[[], bool] | None = None,
    ) -> WalkForwardResult:
        backtest = self.config.backtest
        windows = split_windows(backtest.start, backtest.end, self.wf_config)
        total_windows = len(windows)
        window_results: list[WalkForwardWindowResult] = []
        is_objectives: list[float] = []
        all_oos_trades: list[Trade] = []

        for window in windows:
            if should_stop is not None and should_stop():
                break

            self._notify(
                progress_callback,
                window_index=window.index,
                total_windows=total_windows,
                phase="optimizing",
            )

            window_config = self._window_optimization_config(window)
            opt_result = OptimizationRunner(
                window_config, self.backtest_runner
            ).run()

            if opt_result.best_trial is None:
                window_results.append(
                    WalkForwardWindowResult(
                        index=window.index,
                        train_start=window.train_start,
                        train_end=window.train_end,
                        test_start=window.test_start,
                        test_end=window.test_end,
                        status="no_result",
                    )
                )
                continue

            best_trial = opt_result.best_trial
            strategy_params = best_trial.user_attrs.get("strategy_params", {})
            risk_params = best_trial.user_attrs.get("risk_params", {})
            is_metrics = best_trial.user_attrs.get("metrics", {})

            is_objectives.append(
                float(
                    resolve_objective(is_metrics, self.config.objective.mode)
                )
            )

            self._notify(
                progress_callback,
                window_index=window.index,
                total_windows=total_windows,
                phase="testing",
            )

            oos_config = self._build_backtest_config(
                start=window.test_start,
                end=window.test_end,
                strategy_params=strategy_params,
                risk_params=risk_params,
            )
            oos_result = self.backtest_runner.run(oos_config)
            oos_trades = oos_result.trades or []

            window_results.append(
                WalkForwardWindowResult(
                    index=window.index,
                    train_start=window.train_start,
                    train_end=window.train_end,
                    test_start=window.test_start,
                    test_end=window.test_end,
                    status="completed",
                    best_params={
                        "strategy_params": strategy_params,
                        "risk_params": risk_params,
                        **opt_result.best_params,
                    },
                    is_metrics=is_metrics,
                    oos_metrics=oos_result.metrics,
                    oos_trades=oos_trades,
                )
            )
            all_oos_trades.extend(oos_trades)

        oos_equity_curve = build_equity_curve(
            all_oos_trades,
            backtest.initial_capital,
            backtest.start,
            backtest.end,
        )

        if all_oos_trades:
            base_metrics = _aggregate_base_metrics(
                all_oos_trades, backtest.initial_capital, oos_equity_curve
            )
            backtest_days = max(
                (backtest.end - backtest.start).total_seconds() / 86400, 1.0
            )
            oos_metrics = compute_extended_metrics(
                base_metrics,
                all_oos_trades,
                backtest.initial_capital,
                oos_equity_curve,
                backtest_days,
            )
        else:
            oos_metrics = {}

        efficiency = _compute_efficiency(
            is_objectives,
            oos_metrics,
            self.config.objective.mode,
        )

        return WalkForwardResult(
            windows=window_results,
            oos_equity_curve=oos_equity_curve,
            oos_metrics=oos_metrics,
            efficiency=efficiency,
        )


def _aggregate_base_metrics(
    closed_trades: list[Trade],
    initial_capital: float,
    equity_curve: pd.Series,
) -> dict[str, Any]:
    total_pnl = sum(trade.pnl or 0.0 for trade in closed_trades)
    peak = initial_capital
    max_drawdown = 0.0
    max_drawdown_pct = 0.0

    for value in equity_curve.values:
        peak = max(peak, value)
        drawdown = peak - value
        drawdown_pct = drawdown / peak if peak > 0 else 0.0
        max_drawdown = max(max_drawdown, drawdown)
        max_drawdown_pct = max(max_drawdown_pct, drawdown_pct)

    wins = sum(1 for trade in closed_trades if (trade.pnl or 0.0) > 0)
    total_trades = len(closed_trades)

    return {
        "total_trades": total_trades,
        "total_pnl": total_pnl,
        "max_drawdown": max_drawdown,
        "max_drawdown_pct": max_drawdown_pct,
        "win_rate": wins / total_trades if total_trades else 0.0,
    }


def _compute_efficiency(
    is_objectives: list[float],
    oos_metrics: dict[str, Any],
    objective_mode: Any,
) -> float | None:
    if not is_objectives or not oos_metrics:
        return None

    mean_is = sum(is_objectives) / len(is_objectives)
    if mean_is == 0:
        return None

    oos_objective = float(resolve_objective(oos_metrics, objective_mode))
    return oos_objective / mean_is
