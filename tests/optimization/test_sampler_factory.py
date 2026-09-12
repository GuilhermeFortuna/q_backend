from optuna.pruners import MedianPruner, NopPruner
from optuna.samplers import NSGAIISampler, TPESampler

from q_backend.optimization.models import OptimizationConfig, StudyConfig
from q_backend.optimization.sampler_factory import (
    create_pruner,
    create_sampler,
    create_sampler_and_pruner,
)


def _minimal_config(objective_mode: str, sampler=None) -> OptimizationConfig:
    study_data = {"name": "s", "storage": {"type": "memory"}}
    if sampler is not None:
        study_data["sampler"] = sampler
    return OptimizationConfig.model_validate(
        {
            "study": study_data,
            "objective": {"mode": objective_mode},
            "backtest": {
                "symbol": "X",
                "start": "2024-01-01T00:00:00",
                "end": "2024-06-01T00:00:00",
            },
            "search_space": {},
        }
    )


def test_multi_objective_defaults_to_nsgaii():
    config = _minimal_config("multi_objective_return_drawdown")
    sampler = create_sampler(config.study, config.is_multi_objective())
    assert isinstance(sampler, NSGAIISampler)


def test_single_objective_defaults_to_tpe():
    config = _minimal_config("maximize_sharpe")
    sampler = create_sampler(config.study, config.is_multi_objective())
    assert isinstance(sampler, TPESampler)


def test_explicit_sampler_override():
    config = _minimal_config("multi_objective_return_drawdown", sampler="tpe")
    sampler = create_sampler(config.study, config.is_multi_objective())
    assert isinstance(sampler, TPESampler)


def test_pruner_mapping():
    assert isinstance(create_pruner(StudyConfig(name="s")), NopPruner)
    assert isinstance(create_pruner(StudyConfig(name="s", pruner="median")), MedianPruner)


def test_create_sampler_and_pruner_returns_pair():
    config = _minimal_config("maximize_net_profit")
    sampler, pruner = create_sampler_and_pruner(config)
    assert isinstance(sampler, TPESampler)
    assert isinstance(pruner, NopPruner)
