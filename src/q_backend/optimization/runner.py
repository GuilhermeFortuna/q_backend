import logging
from dataclasses import dataclass, field
from typing import Any

import optuna

from q_backend.optimization.backtest_runner import (
    BacktestRunConfig,
    BacktestRunner,
)
from q_backend.optimization.exceptions import ExpectedTrialFailure
from q_backend.optimization.models import OptimizationConfig
from q_backend.optimization.objectives import resolve_objective, worst_objective_value
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


class OptimizationRunner:
    def __init__(
        self,
        config: OptimizationConfig,
        backtest_runner: BacktestRunner,
    ):
        self.config = config
        self.backtest_runner = backtest_runner

    def _build_backtest_config(self, trial_params) -> BacktestRunConfig:
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
            parallel_mode=backtest.parallel_mode,
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

    def run(self) -> OptimizationResult:
        self.failures: list[dict[str, Any]] = []
        study = load_or_create_study(self.config)
        study.optimize(
            self._objective,
            n_trials=self.config.study.n_trials,
            catch=(Exception,) if self.config.study.continue_on_trial_error else (),
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
