"""Optuna-derived optimization analytics for chart rendering."""

from __future__ import annotations

import logging
from typing import Any, Callable

import optuna
from optuna.importance import get_param_importances
from optuna.study import StudyDirection
from optuna.trial import FrozenTrial, TrialState

from q_backend.optimization.models import OptimizationConfig

logger = logging.getLogger(__name__)

MIN_TRIALS = 30
MAX_PCP_ROWS = 2000

# Cache key: (study_id, n_complete_trials) -> param importances block or None.
_importance_cache: dict[tuple[str, int], dict[str, list[dict[str, Any]]] | None] = {}


def objective_labels(config: OptimizationConfig) -> list[str]:
    """Single source of truth for objective axis labels across all datasets."""
    if config.is_multi_objective():
        return ["return", "drawdown"]
    return [config.objective.mode.value]


def _complete_trials(study: optuna.Study) -> list[FrozenTrial]:
    return [trial for trial in study.trials if trial.state == TrialState.COMPLETE]


def _trial_values(trial: FrozenTrial, *, is_multi: bool) -> list[float]:
    if is_multi:
        return list(trial.values or [])
    if trial.value is not None:
        return [float(trial.value)]
    return []


def _parallel_coordinate_payload(
    complete_trials: list[FrozenTrial],
    config: OptimizationConfig,
) -> dict[str, Any]:
    labels = objective_labels(config)
    is_multi = config.is_multi_objective()
    param_names = sorted({key for trial in complete_trials for key in trial.params})
    sorted_trials = sorted(complete_trials, key=lambda trial: trial.number)
    rows_capped = False
    if len(sorted_trials) > MAX_PCP_ROWS:
        sorted_trials = sorted_trials[-MAX_PCP_ROWS:]
        rows_capped = True

    rows = [
        {
            "number": trial.number,
            "params": dict(trial.params),
            "values": _trial_values(trial, is_multi=is_multi),
        }
        for trial in sorted_trials
    ]
    payload: dict[str, Any] = {
        "params": param_names,
        "objectives": labels,
        "rows": rows,
    }
    if rows_capped:
        payload["rows_capped"] = True
    return payload


def _pareto_front_payload(
    study: optuna.Study,
    complete_trials: list[FrozenTrial],
    config: OptimizationConfig,
) -> dict[str, Any]:
    labels = objective_labels(config)
    is_multi = config.is_multi_objective()
    if is_multi:
        points = [
            {
                "number": trial.number,
                "values": list(trial.values or []),
                "params": dict(trial.params),
            }
            for trial in study.best_trials
        ]
    else:
        # Derive the best completed trial ourselves rather than touching
        # `study.best_trial`, which raises `ValueError("No trials are completed
        # yet.")` for single-objective studies before any trial finishes — the
        # exact early live-running state this endpoint must serve.
        if complete_trials:
            maximize = study.directions[0] == StudyDirection.MAXIMIZE
            best = max(
                complete_trials,
                key=lambda trial: (trial.value if maximize else -trial.value),
            )
            points = [
                {
                    "number": best.number,
                    "values": _trial_values(best, is_multi=False),
                    "params": dict(best.params),
                }
            ]
        else:
            points = []

    completed_numbers = {trial.number for trial in complete_trials}
    points = [point for point in points if point["number"] in completed_numbers]
    return {
        "is_multi_objective": is_multi,
        "objectives": labels,
        "points": points,
    }


def _importance_entries(importances: dict[str, float]) -> list[dict[str, Any]]:
    return [
        {"param": param, "importance": float(value)}
        for param, value in sorted(
            importances.items(),
            key=lambda item: item[1],
            reverse=True,
        )
    ]


def _compute_param_importances(
    study: optuna.Study,
    config: OptimizationConfig,
    *,
    study_id: str | None,
    n_complete: int,
    is_running: bool,
) -> dict[str, list[dict[str, Any]]] | None:
    if is_running or n_complete < MIN_TRIALS:
        return None

    cache_key = (study_id, n_complete) if study_id is not None else None
    if cache_key is not None and cache_key in _importance_cache:
        return _importance_cache[cache_key]

    labels = objective_labels(config)
    try:
        if config.is_multi_objective():
            result: dict[str, list[dict[str, Any]]] = {}
            for index, label in enumerate(labels):
                target: Callable[[FrozenTrial], float] = lambda trial, objective_index=index: trial.values[
                    objective_index
                ]  # type: ignore[index]
                result[label] = _importance_entries(get_param_importances(study, target=target))
        else:
            result = {
                labels[0]: _importance_entries(get_param_importances(study)),
            }
    except Exception as exc:  # noqa: BLE001 - best-effort importance diagnostic; logged
        # Importance is a best-effort diagnostic — fANOVA/sklearn can raise
        # ValueError on degenerate inputs (e.g. a constant param), ImportError
        # if the evaluator's deps are missing, etc. Never let it 500 the
        # endpoint; degrade to a null block.
        logger.info(
            "Param importance unavailable for study %s (%d complete trials): %s",
            study_id,
            n_complete,
            exc,
        )
        result = None

    if cache_key is not None:
        _importance_cache[cache_key] = result
    return result


def empty_analytics_datasets(config: OptimizationConfig) -> dict[str, Any]:
    labels = objective_labels(config)
    return {
        "n_complete_trials": 0,
        "param_importances": None,
        "parallel_coordinate": {
            "params": [],
            "objectives": labels,
            "rows": [],
        },
        "pareto_front": {
            "is_multi_objective": config.is_multi_objective(),
            "objectives": labels,
            "points": [],
        },
    }


def compute_study_analytics(
    study: optuna.Study,
    config: OptimizationConfig,
    *,
    is_running: bool,
    study_id: str | None = None,
) -> dict[str, Any]:
    complete = _complete_trials(study)
    n_complete = len(complete)
    return {
        "n_complete_trials": n_complete,
        "param_importances": _compute_param_importances(
            study,
            config,
            study_id=study_id,
            n_complete=n_complete,
            is_running=is_running,
        ),
        "parallel_coordinate": _parallel_coordinate_payload(complete, config),
        "pareto_front": _pareto_front_payload(study, complete, config),
    }
