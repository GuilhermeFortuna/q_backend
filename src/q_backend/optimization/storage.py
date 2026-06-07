from pathlib import Path

import optuna
from optuna.storages import BaseStorage, InMemoryStorage, RDBStorage

from q_backend.optimization.models import OptimizationConfig, StorageConfig
from q_backend.optimization.sampler_factory import create_sampler_and_pruner


def _project_root() -> Path:
    return Path(__file__).resolve().parents[3]


def create_storage(config: StorageConfig) -> BaseStorage:
    if config.type == "memory":
        return InMemoryStorage()

    if config.type == "sqlite":
        db_path = Path(config.path)
        if not db_path.is_absolute():
            db_path = _project_root() / db_path
        db_path.parent.mkdir(parents=True, exist_ok=True)
        url = f"sqlite:///{db_path.as_posix()}"
        return RDBStorage(url=url)

    if config.type == "url":
        return RDBStorage(url=config.url)

    raise ValueError(f"Unknown storage type: {config.type}")


def load_or_create_study(config: OptimizationConfig) -> optuna.Study:
    sampler, pruner = create_sampler_and_pruner(config)
    storage = create_storage(config.study.storage)
    return optuna.create_study(
        study_name=config.study.name,
        storage=storage,
        load_if_exists=True,
        directions=config.optuna_directions(),
        sampler=sampler,
        pruner=pruner,
    )
