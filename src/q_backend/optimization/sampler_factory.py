import optuna
from optuna.pruners import HyperbandPruner, MedianPruner, NopPruner
from optuna.samplers import NSGAIISampler, RandomSampler, TPESampler

from q_backend.optimization.models import OptimizationConfig, StudyConfig


def create_sampler(
    study_config: StudyConfig,
    is_multi_objective: bool,
    *,
    constant_liar: bool = False,
) -> optuna.samplers.BaseSampler:
    sampler_name = study_config.sampler
    if sampler_name is None:
        sampler_name = "nsgaii" if is_multi_objective else "tpe"

    seed = study_config.seed
    if sampler_name == "tpe":
        return TPESampler(seed=seed, constant_liar=constant_liar)
    if sampler_name == "random":
        return RandomSampler(seed=seed)
    if sampler_name == "nsgaii":
        return NSGAIISampler(seed=seed)
    raise ValueError(f"Unknown sampler: {sampler_name}")


def create_pruner(study_config: StudyConfig) -> optuna.pruners.BasePruner:
    if study_config.pruner == "none":
        return NopPruner()
    if study_config.pruner == "median":
        return MedianPruner()
    if study_config.pruner == "hyperband":
        return HyperbandPruner()
    raise ValueError(f"Unknown pruner: {study_config.pruner}")


def create_sampler_and_pruner(
    config: OptimizationConfig,
    *,
    constant_liar: bool = False,
) -> tuple[optuna.samplers.BaseSampler, optuna.pruners.BasePruner]:
    return (
        create_sampler(
            config.study,
            config.is_multi_objective(),
            constant_liar=constant_liar,
        ),
        create_pruner(config.study),
    )
