"""Unit tests for optimization analytics computation."""

from __future__ import annotations

from unittest.mock import patch

import optuna
import pytest
from optuna.trial import TrialState

from q_backend.optimization.analytics import (
    MIN_TRIALS,
    compute_study_analytics,
    objective_labels,
)
from q_backend.optimization.models import OptimizationConfig


def _single_objective_config() -> OptimizationConfig:
    return OptimizationConfig.model_validate(
        {
            "study": {"name": "analytics_single", "storage": {"type": "memory"}},
            "objective": {"mode": "maximize_net_profit"},
            "backtest": {
                "symbol": "X",
                "start": "2024-01-01T00:00:00",
                "end": "2024-06-01T00:00:00",
            },
            "search_space": {},
        }
    )


def _multi_objective_config() -> OptimizationConfig:
    return OptimizationConfig.model_validate(
        {
            "study": {"name": "analytics_multi", "storage": {"type": "memory"}},
            "objective": {"mode": "multi_objective_return_drawdown"},
            "backtest": {
                "symbol": "X",
                "start": "2024-01-01T00:00:00",
                "end": "2024-06-01T00:00:00",
            },
            "search_space": {},
        }
    )


def _seed_single_objective_study(n_complete: int, *, n_pruned: int = 0) -> optuna.Study:
    study = optuna.create_study(direction="maximize")
    pruned = 0

    def objective(trial):
        nonlocal pruned
        x = trial.suggest_float("x", 0.0, 10.0)
        if pruned < n_pruned:
            pruned += 1
            raise optuna.TrialPruned()
        return x

    study.optimize(objective, n_trials=n_complete + n_pruned)
    study.add_trial(
        optuna.trial.create_trial(
            params={"x": 1.0},
            distributions={"x": optuna.distributions.FloatDistribution(0.0, 10.0)},
            value=None,
            state=TrialState.FAIL,
        )
    )
    return study


def _seed_multi_objective_study() -> optuna.Study:
    study = optuna.create_study(directions=["maximize", "minimize"])
    points = [
        (0.2, 0.1),
        (0.5, 0.3),
        (0.8, 0.7),
        (0.1, 0.9),
    ]
    for index, (ret, drawdown) in enumerate(points):
        study.add_trial(
            optuna.trial.create_trial(
                params={"weight": float(index)},
                distributions={
                    "weight": optuna.distributions.FloatDistribution(0.0, 10.0)
                },
                values=[ret, drawdown],
                state=TrialState.COMPLETE,
            )
        )
    return study


def test_objective_labels_single_and_multi():
    assert objective_labels(_single_objective_config()) == ["maximize_net_profit"]
    assert objective_labels(_multi_objective_config()) == ["return", "drawdown"]


def test_single_objective_parallel_coordinate_excludes_non_complete():
    config = _single_objective_config()
    study = _seed_single_objective_study(3, n_pruned=1)
    payload = compute_study_analytics(study, config, is_running=False)

    assert payload["n_complete_trials"] == 3
    assert len(payload["parallel_coordinate"]["rows"]) == 3
    assert payload["parallel_coordinate"]["params"] == ["x"]
    assert len(payload["parallel_coordinate"]["rows"][0]["values"]) == 1


def test_single_objective_param_importances_below_min_trials_is_none():
    config = _single_objective_config()
    study = _seed_single_objective_study(MIN_TRIALS - 1)
    payload = compute_study_analytics(study, config, is_running=False)
    assert payload["param_importances"] is None


def test_single_objective_param_importances_present_at_min_trials():
    config = _single_objective_config()
    study = _seed_single_objective_study(5)
    fake_importances = {"x": 0.9}

    with patch(
        "q_backend.optimization.analytics.get_param_importances",
        return_value=fake_importances,
    ):
        with patch("q_backend.optimization.analytics.MIN_TRIALS", 5):
            payload = compute_study_analytics(study, config, is_running=False)

    assert payload["param_importances"] is not None
    entries = payload["param_importances"]["maximize_net_profit"]
    assert entries == [{"param": "x", "importance": 0.9}]


def test_running_study_skips_param_importances():
    config = _single_objective_config()
    study = _seed_single_objective_study(5)
    payload = compute_study_analytics(study, config, is_running=True)
    assert payload["param_importances"] is None


def test_multi_objective_pareto_front_is_non_dominated_subset():
    config = _multi_objective_config()
    study = _seed_multi_objective_study()
    payload = compute_study_analytics(study, config, is_running=False)

    pareto = payload["pareto_front"]
    assert pareto["is_multi_objective"] is True
    assert pareto["objectives"] == ["return", "drawdown"]
    point_numbers = {point["number"] for point in pareto["points"]}
    assert point_numbers.issubset({0, 1, 2, 3})
    assert len(point_numbers) >= 2


def test_multi_objective_param_importances_has_both_labels():
    config = _multi_objective_config()
    study = _seed_multi_objective_study()

    def fake_importances(study, *, target=None):
        return {"weight": 0.75}

    with patch(
        "q_backend.optimization.analytics.get_param_importances",
        side_effect=fake_importances,
    ):
        with patch("q_backend.optimization.analytics.MIN_TRIALS", 2):
            payload = compute_study_analytics(study, config, is_running=False)

    assert payload["param_importances"] is not None
    assert set(payload["param_importances"]) == {"return", "drawdown"}


def test_fanova_failure_returns_null_importances():
    config = _single_objective_config()
    study = _seed_single_objective_study(5)

    with patch(
        "q_backend.optimization.analytics.get_param_importances",
        side_effect=RuntimeError("insufficient data"),
    ):
        with patch("q_backend.optimization.analytics.MIN_TRIALS", 5):
            payload = compute_study_analytics(study, config, is_running=False)

    assert payload["param_importances"] is None


def test_fanova_value_error_returns_null_importances():
    """Degenerate inputs (e.g. a constant param) raise ValueError from the
    evaluator — it must degrade to null, never propagate to a 500."""
    config = _single_objective_config()
    study = _seed_single_objective_study(5)

    with patch(
        "q_backend.optimization.analytics.get_param_importances",
        side_effect=ValueError("constant param"),
    ):
        with patch("q_backend.optimization.analytics.MIN_TRIALS", 5):
            payload = compute_study_analytics(study, config, is_running=False)

    assert payload["param_importances"] is None


def test_single_objective_pareto_empty_when_no_complete_trials():
    """Early live-running single-objective study (0 completed trials) must not
    touch study.best_trial, which raises 'No trials are completed yet.'"""
    config = _single_objective_config()
    study = optuna.create_study(direction="maximize")

    payload = compute_study_analytics(study, config, is_running=True, study_id="empty")

    assert payload["n_complete_trials"] == 0
    assert payload["pareto_front"]["points"] == []
    assert payload["pareto_front"]["is_multi_objective"] is False
    assert payload["param_importances"] is None


def test_single_objective_pareto_picks_best_completed_trial():
    config = _single_objective_config()
    study = _seed_single_objective_study(8)

    payload = compute_study_analytics(study, config, is_running=False)
    points = payload["pareto_front"]["points"]

    assert len(points) == 1
    best_value = max(
        trial.value
        for trial in study.trials
        if trial.state == TrialState.COMPLETE
    )
    assert points[0]["values"] == [best_value]


def test_real_param_importances_present_with_sklearn():
    """No mock: guards against the importance view silently no-op'ing when the
    evaluator's dependency (scikit-learn) is missing."""
    config = _single_objective_config()
    study = optuna.create_study(direction="maximize")
    study.optimize(
        lambda t: t.suggest_float("x", 0.0, 1.0) + 0.05 * t.suggest_float("y", 0.0, 1.0),
        n_trials=MIN_TRIALS + 5,
    )

    payload = compute_study_analytics(study, config, is_running=False, study_id="real")

    importances = payload["param_importances"]
    assert importances is not None, "param importance unexpectedly null"
    entries = importances["maximize_net_profit"]
    assert {entry["param"] for entry in entries} == {"x", "y"}
    # x drives the objective ~20x more than y; it must rank first.
    assert entries[0]["param"] == "x"
