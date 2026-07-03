import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Literal

import pandas as pd
import optuna
from pydantic import BaseModel, Field

from q_backend.backtesting.models import Trade
from q_backend.optimization.backtest_runner import (
    BacktestRunConfig,
    BacktestRunner,
)
from q_backend.optimization.metrics import build_equity_curve, compute_extended_metrics
from q_backend.optimization.models import OptimizationConfig, StorageConfig
from q_backend.optimization.objectives import resolve_objective
from q_backend.optimization.parallel import resolve_worker_count
from q_backend.optimization.runner import OptimizationRunner
from q_backend.optimization.search_space import build_position_sizing_config

logger = logging.getLogger(__name__)

ONE_DAY = timedelta(days=1)

# Param-name fragments whose values are a lookback length in bars. The warm-up a
# window needs is the largest such value (×3 so recursive indicators like EMA/HMA
# converge, not just become non-NaN).
_WARMUP_PARAM_KEYS = ("period", "lookback", "window")
_WARMUP_MULTIPLIER = 3


def _warmup_bars_for_params(strategy_params: dict[str, Any]) -> int:
    """Bars of indicator warm-up implied by a strategy's period-like parameters."""
    lookbacks = [
        int(value)
        for key, value in strategy_params.items()
        if isinstance(value, (int, float))
        and not isinstance(value, bool)
        and any(
            fragment in key.split("__", 1)[-1].lower()
            for fragment in _WARMUP_PARAM_KEYS
        )
    ]
    longest = max(lookbacks, default=0)
    return longest * _WARMUP_MULTIPLIER


class WalkForwardConfig(BaseModel):
    train_days: int = Field(ge=1)
    test_days: int = Field(ge=1)
    mode: Literal["rolling", "anchored"] = "rolling"
    min_windows: int = Field(default=2, ge=1)
    # Number of worker processes used to run independent windows in parallel.
    # None => auto (min(window_count, os.cpu_count())). 1 => force sequential.
    max_workers: int | None = Field(default=None, ge=1)


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
    # Set by the parallel path, where windows finish out of order and a monotonic
    # completion count is the meaningful progress signal. None in the sequential path.
    windows_completed: int | None = None


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
        ohlcv: pd.DataFrame | None = None,
        run_id: str | None = None,
        candidate_id: str | None = None,
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
        # When provided, the raw OHLCV frame is shipped to worker processes so
        # independent windows can be optimized in parallel. Without it, the runner
        # falls back to the sequential path using ``backtest_runner``.
        self._ohlcv = ohlcv
        self.run_id = run_id
        self.candidate_id = candidate_id

    def _build_backtest_config(
        self,
        *,
        start: datetime,
        end: datetime,
        strategy_params: dict[str, Any],
        risk_params: dict[str, Any],
        manager_params: dict[str, Any] | None = None,
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
            entries=backtest.entries,
            entry_manager=backtest.entry_manager,
            manager_params=manager_params,
            exit_params=backtest.exit_params,
            fixed_params=self.config.fixed_params,
            # Warm indicators with the bars right before this (out-of-sample) window —
            # in rolling mode that is the tail of the training data, so it adds no
            # look-ahead. Without it a long-period strategy can't trade a short OOS
            # window (indicators are all-NaN) and is wrongly scored as "no result".
            warmup_bars=_warmup_bars_for_params(strategy_params),
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

    def _run_single_window(
        self,
        window: WalkForwardWindow,
        total_windows: int,
        backtest_runner: BacktestRunner,
        progress_callback: Callable[[WalkForwardProgress], None] | None = None,
    ) -> tuple[WalkForwardWindowResult, float | None]:
        """Optimize one window and test the best params out-of-sample.

        Returns the window result plus its in-sample objective value (or ``None``
        when the window produced no usable trial). This is the unit of work the
        parallel path dispatches to worker processes, so it must not touch shared
        mutable state.
        """
        self._notify(
            progress_callback,
            window_index=window.index,
            total_windows=total_windows,
            phase="optimizing",
        )

        window_config = self._window_optimization_config(window)

        callbacks = []
        if self.run_id:
            def optuna_callback(study: optuna.Study, trial: optuna.trial.FrozenTrial) -> None:
                from datetime import datetime
                timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S,%f")[:-3]
                
                # Check state
                if trial.state == optuna.trial.TrialState.COMPLETE:
                    val_str = f"value: {trial.value}"
                elif trial.state == optuna.trial.TrialState.PRUNED:
                    val_str = "state: PRUNED"
                else:
                    val_str = f"state: {trial.state.name}"
                
                params_str = ", ".join(f"'{k}': {v}" for k, v in trial.params.items())
                params_str = "{" + params_str + "}"
                
                try:
                    best_trial = study.best_trial
                    best_val = best_trial.value
                    best_num = best_trial.number
                    best_str = f"Best is trial {best_num} with value: {best_val}"
                except ValueError:
                    best_str = "No best trial yet"
                
                candidate_prefix = f"Candidate {self.candidate_id} - " if self.candidate_id else ""
                window_prefix = f"Window {window.index} - "
                msg = f"[I {timestamp}] {candidate_prefix}{window_prefix}Trial {trial.number} finished with {val_str} and parameters: {params_str}. {best_str}."
                
                try:
                    from q_backend.storage.redis.client import get_redis
                    redis_client = get_redis()
                    log_key = f"strategy_search:logs:{self.run_id}"
                    redis_client.rpush(log_key, msg)
                    redis_client.ltrim(log_key, -100, -1)
                    redis_client.expire(log_key, 86400)
                except Exception as e:  # noqa: BLE001 - best-effort Redis progress; logged
                    logger.warning("Failed to write trial progress to Redis: %s", e)

            callbacks.append(optuna_callback)

        opt_result = OptimizationRunner(window_config, backtest_runner).run(callbacks=callbacks)

        if opt_result.best_trial is None:
            return (
                WalkForwardWindowResult(
                    index=window.index,
                    train_start=window.train_start,
                    train_end=window.train_end,
                    test_start=window.test_start,
                    test_end=window.test_end,
                    status="no_result",
                ),
                None,
            )

        best_trial = opt_result.best_trial
        strategy_params = best_trial.user_attrs.get("strategy_params", {})
        risk_params = best_trial.user_attrs.get("risk_params", {})
        manager_params = best_trial.user_attrs.get("manager_params", {})
        is_metrics = best_trial.user_attrs.get("metrics", {})
        is_objective = float(
            resolve_objective(is_metrics, self.config.objective.mode)
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
            manager_params=manager_params,
        )
        oos_result = backtest_runner.run(oos_config)
        oos_trades = oos_result.trades or []

        return (
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
                    "manager_params": manager_params,
                    **opt_result.best_params,
                },
                is_metrics=is_metrics,
                oos_metrics=oos_result.metrics,
                oos_trades=oos_trades,
            ),
            is_objective,
        )

    def run(
        self,
        progress_callback: Callable[[WalkForwardProgress], None] | None = None,
        should_stop: Callable[[], bool] | None = None,
    ) -> WalkForwardResult:
        # Always sequential. Window-level parallelism now lives one layer up: the
        # walk-forward job fans each window out as its own Dramatiq message, and a
        # discovery candidate runs its whole walk-forward inside a single worker.
        # The Dramatiq pool is the only source of CPU parallelism, so this runner
        # must not spawn a process pool of its own.
        backtest = self.config.backtest
        windows = split_windows(backtest.start, backtest.end, self.wf_config)
        return self._run_sequential(windows, progress_callback, should_stop)

    def _run_sequential(
        self,
        windows: list[WalkForwardWindow],
        progress_callback: Callable[[WalkForwardProgress], None] | None,
        should_stop: Callable[[], bool] | None,
    ) -> WalkForwardResult:
        total_windows = len(windows)
        window_results: list[WalkForwardWindowResult] = []
        is_objectives: list[float] = []

        for window in windows:
            if should_stop is not None and should_stop():
                break
            result, is_objective = self._run_single_window(
                window, total_windows, self.backtest_runner, progress_callback
            )
            window_results.append(result)
            if is_objective is not None:
                is_objectives.append(is_objective)

        return self._finalize_result(window_results, is_objectives)

    def _finalize_result(
        self,
        window_results: list[WalkForwardWindowResult],
        is_objectives: list[float],
    ) -> WalkForwardResult:
        backtest = self.config.backtest
        all_oos_trades: list[Trade] = [
            trade
            for window in window_results
            if window.status == "completed"
            for trade in window.oos_trades
        ]

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


# --- Parallel window execution -------------------------------------------------
#
# Each window is an independent unit of work (its own Optuna study + OOS backtest),
# so we fan them out across processes. The OHLCV frame is large and identical for
# every window, so it is shipped to each worker once via the pool initializer
# rather than pickled per task.

