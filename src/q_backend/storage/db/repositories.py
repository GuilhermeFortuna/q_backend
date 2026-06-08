import uuid
from datetime import datetime
from typing import Any, Optional

from sqlalchemy.orm import Session

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
    if error_message is not None:
        backtest_run.error_message = error_message
    if finished_at is not None:
        backtest_run.finished_at = finished_at
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
