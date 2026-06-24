from pathlib import Path

import optuna
from optuna.pruners import NopPruner
from optuna.storages import BaseStorage, InMemoryStorage, RDBStorage

from q_backend.optimization.models import OptimizationConfig, StorageConfig
from q_backend.optimization.sampler_factory import create_sampler_and_pruner

# Dedicated Postgres schema for Optuna's own tables. It must NOT be `public`:
# Optuna keeps its own alembic_version, which would collide with the app's Alembic.
OPTUNA_SCHEMA = "optuna"


def _project_root() -> Path:
    return Path(__file__).resolve().parents[3]


def create_shared_optuna_storage() -> RDBStorage:
    """RDBStorage on the project Postgres, isolated in the `optuna` schema.

    Backs distributed studies where several Dramatiq trial workers optimize the same
    study concurrently. The schema is created on demand and Optuna's tables live
    inside it via the connection ``search_path``.
    """
    from sqlalchemy import create_engine, text

    from q_backend.storage.settings import get_settings

    url = get_settings().database_url
    admin_engine = create_engine(url)
    try:
        with admin_engine.begin() as conn:
            conn.execute(text(f"CREATE SCHEMA IF NOT EXISTS {OPTUNA_SCHEMA}"))
    finally:
        admin_engine.dispose()

    return RDBStorage(
        url=url,
        engine_kwargs={"connect_args": {"options": f"-csearch_path={OPTUNA_SCHEMA}"}},
    )


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

    if config.type == "shared":
        return create_shared_optuna_storage()

    raise ValueError(f"Unknown storage type: {config.type}")


def load_existing_study(config: OptimizationConfig) -> optuna.Study | None:
    """Load an Optuna study without creating one if it does not exist."""
    storage = create_storage(config.study.storage)
    try:
        return optuna.load_study(
            study_name=config.study.name,
            storage=storage,
        )
    except KeyError:
        return None


def load_or_create_study(
    config: OptimizationConfig,
    *,
    constant_liar: bool = False,
    disable_pruning: bool = False,
) -> optuna.Study:
    sampler, pruner = create_sampler_and_pruner(config, constant_liar=constant_liar)
    if disable_pruning:
        pruner = NopPruner()
    storage = create_storage(config.study.storage)
    return optuna.create_study(
        study_name=config.study.name,
        storage=storage,
        load_if_exists=True,
        directions=config.optuna_directions(),
        sampler=sampler,
        pruner=pruner,
    )
