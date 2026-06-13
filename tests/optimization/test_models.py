import pytest
from pydantic import ValidationError

from q_backend.optimization.models import (
    FloatParam,
    IntParam,
    ObjectiveMode,
    OptimizationConfig,
    StorageConfig,
    StudyConfig,
)


def test_int_param_validates():
    param = IntParam(type="int", low=1, high=10, step=1)
    assert param.low == 1


def test_storage_sqlite_requires_path():
    with pytest.raises(ValidationError):
        StorageConfig(type="sqlite")


def test_storage_url_requires_url():
    with pytest.raises(ValidationError):
        StorageConfig(type="url")


def test_multi_objective_rejects_direction():
    with pytest.raises(ValidationError):
        OptimizationConfig.model_validate(
            {
                "study": {
                    "name": "bad",
                    "direction": "maximize",
                    "storage": {"type": "memory"},
                },
                "objective": {"mode": "multi_objective_return_drawdown"},
                "backtest": {
                    "symbol": "X",
                    "start": "2024-01-01T00:00:00",
                    "end": "2024-06-01T00:00:00",
                },
                "search_space": {},
            }
        )


def test_backtest_start_must_be_before_end():
    with pytest.raises(ValidationError):
        OptimizationConfig.model_validate(
            {
                "study": {"name": "bad", "storage": {"type": "memory"}},
                "objective": {"mode": "maximize_net_profit"},
                "backtest": {
                    "symbol": "X",
                    "start": "2024-06-01T00:00:00",
                    "end": "2024-01-01T00:00:00",
                },
                "search_space": {},
            }
        )


def test_backtest_datetimes_normalized_to_naive_brasilia():
    config = OptimizationConfig.model_validate(
        {
            "study": {"name": "s", "storage": {"type": "memory"}},
            "objective": {"mode": "maximize_net_profit"},
            "backtest": {
                "symbol": "X",
                "start": "2024-01-01T03:00:00+00:00",
                "end": "2024-06-01T02:59:59.999+00:00",
            },
            "search_space": {},
        }
    )
    assert config.backtest.start.tzinfo is None
    assert config.backtest.end.tzinfo is None
    assert config.backtest.start.hour == 0
    assert config.backtest.end.hour == 23


def test_optuna_directions_single_objective():
    config = OptimizationConfig.model_validate(
        {
            "study": {"name": "s", "storage": {"type": "memory"}},
            "objective": {"mode": "minimize_drawdown"},
            "backtest": {
                "symbol": "X",
                "start": "2024-01-01T00:00:00",
                "end": "2024-06-01T00:00:00",
            },
            "search_space": {},
        }
    )
    assert config.optuna_directions() == ["minimize"]


def test_optuna_directions_multi_objective():
    config = OptimizationConfig.model_validate(
        {
            "study": {"name": "s", "storage": {"type": "memory"}},
            "objective": {"mode": ObjectiveMode.MULTI_OBJECTIVE_RETURN_DRAWDOWN},
            "backtest": {
                "symbol": "X",
                "start": "2024-01-01T00:00:00",
                "end": "2024-06-01T00:00:00",
            },
            "search_space": {},
        }
    )
    assert config.optuna_directions() == ["maximize", "minimize"]


def test_study_config_defaults():
    study = StudyConfig(name="test")
    assert study.seed == 42
    assert study.pruner == "none"
    assert study.continue_on_trial_error is False
    assert study.max_workers is None
