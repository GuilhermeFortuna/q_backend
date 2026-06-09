import uuid
from datetime import datetime
from typing import Any, Optional

from sqlalchemy import Float, cast, desc, func, nulls_last, select
from sqlalchemy.orm import Session, selectinload

from q_backend.storage.db.models import (
    BacktestConfig,
    BacktestRun,
    DataIngestionRun,
    OptimizationStudy,
    OptimizationTrial,
    RunStatus,
    Strategy,
    StrategyVersion,
    TrialStatus,
)


def create_strategy(
    session: Session,
    *,
    name: str,
    description: Optional[str] = None,
) -> Strategy:
    strategy = Strategy(name=name, description=description)
    session.add(strategy)
    session.flush()
    return strategy


def get_or_create_strategy(session: Session, *, name: str) -> Strategy:
    strategy = session.execute(
        select(Strategy).where(Strategy.name == name)
    ).scalar_one_or_none()
    if strategy is None:
        strategy = create_strategy(session, name=name)
    return strategy


def get_backtest_run(session: Session, run_id: uuid.UUID) -> Optional[BacktestRun]:
    return session.get(BacktestRun, run_id)


def find_backtest_run_by_config(
    session: Session, config: dict[str, Any]
) -> Optional[BacktestRun]:
    """Return the newest persisted run whose stored config matches exactly."""
    return session.execute(
        select(BacktestRun)
        .where(BacktestRun.config == config)
        .order_by(desc(BacktestRun.created_at))
        .limit(1)
    ).scalar_one_or_none()


def delete_backtest_run(session: Session, run_id: uuid.UUID) -> bool:
    backtest_run = session.get(BacktestRun, run_id)
    if backtest_run is None:
        return False
    session.delete(backtest_run)
    session.flush()
    return True


def delete_backtest_runs(
    session: Session, run_ids: list[uuid.UUID]
) -> tuple[int, list[uuid.UUID]]:
    deleted = 0
    not_found: list[uuid.UUID] = []
    for run_id in run_ids:
        backtest_run = session.get(BacktestRun, run_id)
        if backtest_run is None:
            not_found.append(run_id)
            continue
        session.delete(backtest_run)
        deleted += 1
    if deleted:
        session.flush()
    return deleted, not_found


def delete_optimization_studies(
    session: Session, study_ids: list[uuid.UUID]
) -> tuple[int, list[uuid.UUID]]:
    deleted = 0
    not_found: list[uuid.UUID] = []
    for study_id in study_ids:
        study = get_optimization_study(session, study_id)
        if study is None:
            not_found.append(study_id)
            continue
        session.delete(study)
        deleted += 1
    if deleted:
        session.flush()
    return deleted, not_found


def _backtest_run_order(sort: str):
    if sort == "pnl_desc":
        pnl = cast(BacktestRun.result_summary["total_pnl"].as_string(), Float)
        return nulls_last(desc(pnl)), desc(BacktestRun.created_at)
    if sort == "pnl_asc":
        pnl = cast(BacktestRun.result_summary["total_pnl"].as_string(), Float)
        return nulls_last(pnl.asc()), desc(BacktestRun.created_at)
    return (desc(BacktestRun.created_at),)


def list_backtest_runs(
    session: Session,
    *,
    limit: int = 50,
    offset: int = 0,
    symbol: Optional[str] = None,
    strategy: Optional[str] = None,
    saved_only: Optional[bool] = None,
    sort: str = "created_at_desc",
) -> tuple[list[BacktestRun], int]:
    base = select(BacktestRun)
    if symbol is not None:
        base = base.where(BacktestRun.config["symbol"].as_string() == symbol)
    if strategy is not None:
        base = base.where(BacktestRun.config["strategy"].as_string() == strategy)
    if saved_only:
        base = base.where(BacktestRun.is_saved.is_(True))

    total = session.execute(
        select(func.count()).select_from(base.subquery())
    ).scalar_one()

    order_clauses = _backtest_run_order(sort)
    runs = session.execute(
        base.order_by(*order_clauses).limit(limit).offset(offset)
    ).scalars().all()
    return list(runs), total


def create_strategy_version(
    session: Session,
    *,
    strategy_id: uuid.UUID,
    version: int,
    config: dict[str, Any],
    status: str = RunStatus.PENDING.value,
) -> StrategyVersion:
    strategy_version = StrategyVersion(
        strategy_id=strategy_id,
        version=version,
        config=config,
        status=status,
    )
    session.add(strategy_version)
    session.flush()
    return strategy_version


def create_backtest_config(
    session: Session,
    *,
    name: str,
    config: dict[str, Any],
    strategy_version_id: Optional[uuid.UUID] = None,
) -> BacktestConfig:
    backtest_config = BacktestConfig(
        name=name,
        config=config,
        strategy_version_id=strategy_version_id,
    )
    session.add(backtest_config)
    session.flush()
    return backtest_config


def create_backtest_run(
    session: Session,
    *,
    backtest_config_id: uuid.UUID,
    config: dict[str, Any],
    status: str = RunStatus.PENDING.value,
    started_at: Optional[datetime] = None,
) -> BacktestRun:
    backtest_run = BacktestRun(
        backtest_config_id=backtest_config_id,
        config=config,
        status=status,
        started_at=started_at,
    )
    session.add(backtest_run)
    session.flush()
    return backtest_run


def update_backtest_run(
    session: Session,
    run_id: uuid.UUID,
    *,
    status: Optional[str] = None,
    result_summary: Optional[dict[str, Any]] = None,
    lake_paths: Optional[dict[str, Any]] = None,
    error_message: Optional[str] = None,
    finished_at: Optional[datetime] = None,
    started_at: Optional[datetime] = None,
    is_saved: Optional[bool] = None,
    clear_error_message: bool = False,
) -> BacktestRun:
    backtest_run = session.get(BacktestRun, run_id)
    if backtest_run is None:
        raise ValueError(f"BacktestRun {run_id} not found")
    if status is not None:
        backtest_run.status = status
    if result_summary is not None:
        backtest_run.result_summary = result_summary
    if lake_paths is not None:
        backtest_run.lake_paths = lake_paths
    if clear_error_message:
        backtest_run.error_message = None
    elif error_message is not None:
        backtest_run.error_message = error_message
    if finished_at is not None:
        backtest_run.finished_at = finished_at
    if started_at is not None:
        backtest_run.started_at = started_at
    if is_saved is not None:
        backtest_run.is_saved = is_saved
    session.flush()
    return backtest_run


def create_optimization_study(
    session: Session,
    *,
    name: str,
    config: dict[str, Any],
    status: str = RunStatus.PENDING.value,
) -> OptimizationStudy:
    study = OptimizationStudy(name=name, config=config, status=status)
    session.add(study)
    session.flush()
    return study


def update_optimization_study(
    session: Session,
    study_id: uuid.UUID,
    *,
    status: Optional[str] = None,
    config: Optional[dict[str, Any]] = None,
) -> OptimizationStudy:
    study = session.get(OptimizationStudy, study_id)
    if study is None:
        raise ValueError(f"OptimizationStudy {study_id} not found")
    if status is not None:
        study.status = status
    if config is not None:
        study.config = config
    session.flush()
    return study


def create_optimization_trial(
    session: Session,
    *,
    study_id: uuid.UUID,
    trial_number: int,
    params: dict[str, Any],
    status: str = TrialStatus.PENDING.value,
    metrics: Optional[dict[str, Any]] = None,
) -> OptimizationTrial:
    trial = OptimizationTrial(
        study_id=study_id,
        trial_number=trial_number,
        params=params,
        status=status,
        metrics=metrics,
    )
    session.add(trial)
    session.flush()
    return trial


def update_optimization_trial(
    session: Session,
    trial_id: uuid.UUID,
    *,
    status: Optional[str] = None,
    params: Optional[dict[str, Any]] = None,
    metrics: Optional[dict[str, Any]] = None,
) -> OptimizationTrial:
    trial = session.get(OptimizationTrial, trial_id)
    if trial is None:
        raise ValueError(f"OptimizationTrial {trial_id} not found")
    if status is not None:
        trial.status = status
    if params is not None:
        trial.params = params
    if metrics is not None:
        trial.metrics = metrics
    session.flush()
    return trial


def get_optimization_study(
    session: Session, study_id: uuid.UUID
) -> Optional[OptimizationStudy]:
    return session.execute(
        select(OptimizationStudy)
        .where(OptimizationStudy.id == study_id)
        .options(selectinload(OptimizationStudy.trials))
    ).scalar_one_or_none()


def delete_optimization_study(session: Session, study_id: uuid.UUID) -> bool:
    study = get_optimization_study(session, study_id)
    if study is None:
        return False
    session.delete(study)
    session.flush()
    return True


def list_optimization_studies(
    session: Session,
    *,
    limit: int = 50,
    offset: int = 0,
) -> tuple[list[OptimizationStudy], int]:
    base = select(OptimizationStudy)
    total = session.execute(
        select(func.count()).select_from(base.subquery())
    ).scalar_one()
    studies = session.execute(
        base.order_by(desc(OptimizationStudy.created_at))
        .limit(limit)
        .offset(offset)
        .options(selectinload(OptimizationStudy.trials))
    ).scalars().all()
    return list(studies), total


def get_optimization_trial_by_number(
    session: Session,
    *,
    study_id: uuid.UUID,
    trial_number: int,
) -> Optional[OptimizationTrial]:
    return session.execute(
        select(OptimizationTrial).where(
            OptimizationTrial.study_id == study_id,
            OptimizationTrial.trial_number == trial_number,
        )
    ).scalar_one_or_none()


def create_data_ingestion_run(
    session: Session,
    *,
    source: str,
    symbol: str,
    dataset_type: str,
    timeframe: Optional[str] = None,
    status: str = RunStatus.PENDING.value,
    lake_path: Optional[str] = None,
    stats: Optional[dict[str, Any]] = None,
    started_at: Optional[datetime] = None,
) -> DataIngestionRun:
    ingestion_run = DataIngestionRun(
        source=source,
        symbol=symbol,
        dataset_type=dataset_type,
        timeframe=timeframe,
        status=status,
        lake_path=lake_path,
        stats=stats,
        started_at=started_at,
    )
    session.add(ingestion_run)
    session.flush()
    return ingestion_run


def update_data_ingestion_run(
    session: Session,
    run_id: uuid.UUID,
    *,
    status: Optional[str] = None,
    lake_path: Optional[str] = None,
    stats: Optional[dict[str, Any]] = None,
    finished_at: Optional[datetime] = None,
) -> DataIngestionRun:
    ingestion_run = session.get(DataIngestionRun, run_id)
    if ingestion_run is None:
        raise ValueError(f"DataIngestionRun {run_id} not found")
    if status is not None:
        ingestion_run.status = status
    if lake_path is not None:
        ingestion_run.lake_path = lake_path
    if stats is not None:
        ingestion_run.stats = stats
    if finished_at is not None:
        ingestion_run.finished_at = finished_at
    session.flush()
    return ingestion_run
