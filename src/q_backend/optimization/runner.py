import logging
from collections.abc import Callable
from concurrent.futures import Future, ProcessPoolExecutor
from dataclasses import dataclass, field, replace
from typing import Any, Literal, TypedDict

import optuna
import pandas as pd

from q_backend.backtesting.engine import ParallelMode
from q_backend.backtesting.models import Trade
from q_backend.optimization.backtest_runner import (
    BacktestRunConfig,
    BacktestRunner,
    DefaultBacktestRunner,
)
from q_backend.optimization.exceptions import ExpectedTrialFailure
from q_backend.optimization.models import OptimizationConfig, TrialParams
from q_backend.optimization.objectives import resolve_objective, worst_objective_value
from q_backend.optimization.parallel import resolve_worker_count
from q_backend.optimization.search_space import (
    build_position_sizing_config,
    suggest_params,
)
from q_backend.optimization.storage import load_or_create_study
from q_backend.optimization.validators import validate_trial_params

logger = logging.getLogger(__name__)


@dataclass
class OptimizationResult:
    study: optuna.Study
    best_params: dict[str, Any]
    best_trial: optuna.trial.FrozenTrial | None
    pareto_trials: list[optuna.trial.FrozenTrial] = field(default_factory=list)
    failures: list[dict[str, Any]] = field(default_factory=list)


class BacktestWorkerSuccess(TypedDict):
    status: Literal["complete"]
    metrics: dict[str, Any]
    trades: list[Trade] | None
    trial_user_attrs: dict[str, Any]


class BacktestWorkerFailure(TypedDict):
    status: Literal["pruned", "error"]
    error: str


BacktestWorkerResult = BacktestWorkerSuccess | BacktestWorkerFailure

_WORKER_OHLCV: pd.DataFrame | None = None


def _init_worker(df: pd.DataFrame) -> None:
    global _WORKER_OHLCV
    _WORKER_OHLCV = df


def _run_backtest_worker(cfg: BacktestRunConfig) -> BacktestWorkerResult:
    """Run one trial backtest in a worker process.

    Returns ``(metrics, trades, trial_user_attrs)`` on success, or a failure tag
    (``pruned`` / ``error`` + reason) when the backtest cannot produce a score.
    """
    if _WORKER_OHLCV is None:
        raise RuntimeError("Worker OHLCV frame was not initialized")

    worker_cfg = replace(cfg, parallel_mode=ParallelMode.SEQUENTIAL)
    runner = DefaultBacktestRunner.from_frame_sliced(_WORKER_OHLCV)

    try:
        result = runner.run(worker_cfg)
    except ExpectedTrialFailure as exc:
        return {"status": "pruned", "error": exc.reason}
    except ValueError as exc:
        return {"status": "pruned", "error": str(exc)}
    except Exception as exc:
        return {"status": "error", "error": str(exc)}

    if result.metrics.get("total_trades", 0) == 0:
        return {"status": "pruned", "error": "zero trades"}

    return {
        "status": "complete",
        "metrics": result.metrics,
        "trades": result.trades,
        "trial_user_attrs": result.trial_user_attrs,
    }


class OptimizationRunner:
    def __init__(
        self,
        config: OptimizationConfig,
        backtest_runner: BacktestRunner,
        *,
        ohlcv: pd.DataFrame | None = None,
        max_workers: int | None = None,
    ):
        self.config = config
        self.backtest_runner = backtest_runner
        self._ohlcv = ohlcv
        self._max_workers = max_workers

    def _build_backtest_config(self, trial_params: TrialParams) -> BacktestRunConfig:
        backtest = self.config.backtest
        position_sizing = build_position_sizing_config(trial_params.risk_params)
        return BacktestRunConfig(
            symbol=backtest.symbol,
            timeframe=backtest.timeframe,
            start=backtest.start,
            end=backtest.end,
            initial_capital=backtest.initial_capital,
            point_value=backtest.point_value,
            strategy=backtest.strategy,
            strategy_params=trial_params.strategy_params,
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

    def _objective(self, trial: optuna.Trial) -> float | tuple[float, ...]:
        trial_params = suggest_params(trial, self.config.search_space)
        validate_trial_params(trial_params, self.config.backtest.strategy)

        backtest_config = self._build_backtest_config(trial_params)

        try:
            result = self.backtest_runner.run(backtest_config)
        except ExpectedTrialFailure as exc:
            trial.set_user_attr("status", "pruned")
            trial.set_user_attr("error", exc.reason)
            raise optuna.TrialPruned(exc.reason) from exc
        except ValueError as exc:
            trial.set_user_attr("status", "pruned")
            trial.set_user_attr("error", str(exc))
            raise optuna.TrialPruned(str(exc)) from exc
        except Exception as exc:
            if not self.config.study.continue_on_trial_error:
                raise
            trial.set_user_attr("status", "error")
            trial.set_user_attr("error", str(exc))
            self.failures.append(
                {"trial_number": trial.number, "error": str(exc)},
            )
            worst = worst_objective_value(self.config.objective.mode)
            if isinstance(worst, list):
                return tuple(worst)
            return worst

        if result.metrics.get("total_trades", 0) == 0:
            trial.set_user_attr("status", "pruned")
            trial.set_user_attr("error", "zero trades")
            raise optuna.TrialPruned("zero trades")

        trial.set_user_attr("status", "complete")
        trial.set_user_attr("metrics", result.metrics)
        trial.set_user_attr("strategy_params", trial_params.strategy_params)
        trial.set_user_attr("risk_params", trial_params.risk_params)
        for key, value in result.trial_user_attrs.items():
            trial.set_user_attr(key, value)

        objective_value = resolve_objective(result.metrics, self.config.objective.mode)
        if isinstance(objective_value, list):
            return tuple(objective_value)
        return objective_value

    def _fire_callbacks(
        self,
        study: optuna.Study,
        trial_number: int,
        callbacks: list[Callable[[optuna.Study, optuna.trial.FrozenTrial], None]]
        | None,
    ) -> None:
        if not callbacks:
            return
        frozen = study.trials[trial_number]
        for callback in callbacks:
            callback(study, frozen)

    def _tell_worker_result(
        self,
        study: optuna.Study,
        trial: optuna.trial.Trial,
        trial_params: TrialParams,
        worker_result: BacktestWorkerResult,
        callbacks: list[Callable[[optuna.Study, optuna.trial.FrozenTrial], None]]
        | None,
    ) -> None:
        if worker_result["status"] == "pruned":
            trial.set_user_attr("status", "pruned")
            trial.set_user_attr("error", worker_result["error"])
            study.tell(trial, state=optuna.trial.TrialState.PRUNED)
            self._fire_callbacks(study, trial.number, callbacks)
            return

        if worker_result["status"] == "error":
            if not self.config.study.continue_on_trial_error:
                trial.set_user_attr("status", "error")
                trial.set_user_attr("error", worker_result["error"])
                study.tell(trial, state=optuna.trial.TrialState.FAIL)
                raise RuntimeError(worker_result["error"])

            trial.set_user_attr("status", "error")
            trial.set_user_attr("error", worker_result["error"])
            self.failures.append(
                {"trial_number": trial.number, "error": worker_result["error"]},
            )
            worst = worst_objective_value(self.config.objective.mode)
            if isinstance(worst, list):
                study.tell(trial, values=tuple(worst))
            else:
                study.tell(trial, worst)
            self._fire_callbacks(study, trial.number, callbacks)
            return

        trial.set_user_attr("status", "complete")
        trial.set_user_attr("metrics", worker_result["metrics"])
        trial.set_user_attr("strategy_params", trial_params.strategy_params)
        trial.set_user_attr("risk_params", trial_params.risk_params)
        for key, value in worker_result["trial_user_attrs"].items():
            trial.set_user_attr(key, value)

        objective_value = resolve_objective(
            worker_result["metrics"], self.config.objective.mode
        )
        if isinstance(objective_value, list):
            study.tell(trial, values=tuple(objective_value))
        else:
            study.tell(trial, objective_value)
        self._fire_callbacks(study, trial.number, callbacks)

    def _tell_validation_pruned(
        self,
        study: optuna.Study,
        trial: optuna.trial.Trial,
        reason: str,
        callbacks: list[Callable[[optuna.Study, optuna.trial.FrozenTrial], None]]
        | None,
    ) -> None:
        trial.set_user_attr("status", "pruned")
        trial.set_user_attr("error", reason)
        study.tell(trial, state=optuna.trial.TrialState.PRUNED)
        self._fire_callbacks(study, trial.number, callbacks)

    def _run_parallel(
        self,
        study: optuna.Study,
        n_trials: int,
        callbacks: list[Callable[[optuna.Study, optuna.trial.FrozenTrial], None]]
        | None,
        should_stop: Callable[[], bool] | None,
    ) -> None:
        """Fan trials out across worker processes via Optuna ask/tell.

        Parallel results are **not** byte-identical to the sequential path: the
        sampler observes completed trials in completion order, and batched
        ``ask()`` with ``constant_liar=True`` trades strict reproducibility for
        throughput. Assertions should compare comparable best objectives, not
        trial order or exact params.
        """
        workers = resolve_worker_count(self._max_workers, n_trials)
        assert self._ohlcv is not None

        dispatched = 0

        with ProcessPoolExecutor(
            max_workers=workers,
            initializer=_init_worker,
            initargs=(self._ohlcv,),
        ) as pool:
            while dispatched < n_trials:
                if should_stop is not None and should_stop():
                    break

                remaining = n_trials - dispatched
                batch_size = min(workers, remaining)
                batch: list[
                    tuple[
                        Future[BacktestWorkerResult],
                        optuna.trial.Trial,
                        TrialParams,
                    ]
                ] = []

                for _ in range(batch_size):
                    trial = study.ask()
                    trial_params = suggest_params(trial, self.config.search_space)
                    try:
                        validate_trial_params(
                            trial_params, self.config.backtest.strategy
                        )
                    except optuna.TrialPruned as exc:
                        self._tell_validation_pruned(
                            study, trial, str(exc), callbacks
                        )
                        dispatched += 1
                        continue
                    backtest_config = self._build_backtest_config(trial_params)
                    future = pool.submit(_run_backtest_worker, backtest_config)
                    batch.append((future, trial, trial_params))
                    dispatched += 1

                for future, trial, trial_params in batch:
                    worker_result = future.result()
                    self._tell_worker_result(
                        study, trial, trial_params, worker_result, callbacks
                    )

    def run(
        self,
        callbacks: list[Callable[[optuna.Study, optuna.trial.FrozenTrial], None]]
        | None = None,
        n_trials: int | None = None,
        should_stop: Callable[[], bool] | None = None,
    ) -> OptimizationResult:
        """Run trials and report the best/pareto result.

        ``n_trials`` overrides ``config.study.n_trials`` — distributed trial workers
        pass their per-worker chunk size so several workers can share one study
        (``storage.type == "shared"``) without each running the full budget.

        When ``ohlcv`` was supplied at construction and more than one worker process
        is resolved, trials run in parallel via ask/tell. Otherwise the sequential
        ``study.optimize`` path is used unchanged.
        """
        self.failures = []
        trial_budget = (
            self.config.study.n_trials if n_trials is None else n_trials
        )

        use_parallel = self._ohlcv is not None and resolve_worker_count(
            self._max_workers, trial_budget
        ) > 1

        disable_pruning = False
        if use_parallel and self.config.study.pruner != "none":
            logger.warning(
                "Intermediate-value pruning is disabled for parallel optimization; "
                "exception-based pruning (zero trades / ExpectedTrialFailure) still "
                "applies. Full parallel pruning is not supported in this release."
            )
            disable_pruning = True

        study = load_or_create_study(
            self.config,
            constant_liar=use_parallel,
            disable_pruning=disable_pruning,
        )

        if use_parallel:
            self._run_parallel(study, trial_budget, callbacks, should_stop)
        else:
            study.optimize(
                self._objective,
                n_trials=trial_budget,
                catch=(Exception,) if self.config.study.continue_on_trial_error else (),
                callbacks=callbacks,
            )

        if self.config.is_multi_objective():
            pareto_trials = study.best_trials
            best_trial = pareto_trials[0] if pareto_trials else None
            best_params = best_trial.params if best_trial is not None else {}
        else:
            pareto_trials = []
            try:
                best_trial = study.best_trial
                best_params = study.best_params
            except ValueError:
                best_trial = None
                best_params = {}

        return OptimizationResult(
            study=study,
            best_params=best_params,
            best_trial=best_trial,
            pareto_trials=pareto_trials,
            failures=self.failures,
        )
