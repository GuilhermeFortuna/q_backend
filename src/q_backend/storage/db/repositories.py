import uuid
from datetime import datetime, timezone
from typing import Any, Optional

from sqlalchemy import Float, cast, desc, func, nulls_last, select, update
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
    WalkForwardRun,
    WalkForwardWindow,
    StrategySearchRun,
    StrategySearchCandidate,
)


_ACTIVE_RUN_STATUSES = (RunStatus.PENDING.value, RunStatus.RUNNING.value)


def mark_active_runs_cancelled(
    session: Session,
    model: type,
    *,
    error_message: Optional[str] = None,
) -> int:
    """Bulk-cancel runs still marked pending/running for the given model.

    Used to reconcile orphaned runs on startup: when the process restarts,
    the in-memory job registry is empty, so any run still flagged active in
    the DB has no live worker and would otherwise stay "running" forever.
    Returns the number of rows updated.
    """
    values: dict[str, Any] = {"status": "cancelled"}
    if error_message is not None and hasattr(model, "error_message"):
        values["error_message"] = error_message
    if hasattr(model, "finished_at"):
        values["finished_at"] = datetime.now(timezone.utc)
    result = session.execute(
        update(model)
        .where(model.status.in_(_ACTIVE_RUN_STATUSES))
        .values(**values)
    )
    return int(result.rowcount or 0)


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


def create_walkforward_run(
    session: Session,
    *,
    name: str,
    config: dict[str, Any],
    status: str = RunStatus.PENDING.value,
    started_at: Optional[datetime] = None,
) -> WalkForwardRun:
    run = WalkForwardRun(
        name=name,
        config=config,
        status=status,
        started_at=started_at,
    )
    session.add(run)
    session.flush()
    return run


def update_walkforward_run(
    session: Session,
    run_id: uuid.UUID,
    *,
    status: Optional[str] = None,
    config: Optional[dict[str, Any]] = None,
    result_summary: Optional[dict[str, Any]] = None,
    lake_paths: Optional[dict[str, Any]] = None,
    error_message: Optional[str] = None,
    finished_at: Optional[datetime] = None,
    started_at: Optional[datetime] = None,
    clear_error_message: bool = False,
) -> WalkForwardRun:
    run = session.get(WalkForwardRun, run_id)
    if run is None:
        raise ValueError(f"WalkForwardRun {run_id} not found")
    if status is not None:
        run.status = status
    if config is not None:
        run.config = config
    if result_summary is not None:
        run.result_summary = result_summary
    if lake_paths is not None:
        run.lake_paths = lake_paths
    if clear_error_message:
        run.error_message = None
    elif error_message is not None:
        run.error_message = error_message
    if finished_at is not None:
        run.finished_at = finished_at
    if started_at is not None:
        run.started_at = started_at
    session.flush()
    return run


def create_walkforward_window(
    session: Session,
    *,
    run_id: uuid.UUID,
    window_number: int,
    train_start: datetime,
    train_end: datetime,
    test_start: datetime,
    test_end: datetime,
    status: str,
    best_params: dict[str, Any],
    is_metrics: Optional[dict[str, Any]] = None,
    oos_metrics: Optional[dict[str, Any]] = None,
) -> WalkForwardWindow:
    window = WalkForwardWindow(
        run_id=run_id,
        window_number=window_number,
        train_start=train_start,
        train_end=train_end,
        test_start=test_start,
        test_end=test_end,
        status=status,
        best_params=best_params,
        is_metrics=is_metrics,
        oos_metrics=oos_metrics,
    )
    session.add(window)
    session.flush()
    return window


def get_walkforward_run(
    session: Session, run_id: uuid.UUID
) -> Optional[WalkForwardRun]:
    return session.execute(
        select(WalkForwardRun)
        .where(WalkForwardRun.id == run_id)
        .options(selectinload(WalkForwardRun.windows))
    ).scalar_one_or_none()


def list_walkforward_runs(
    session: Session,
    *,
    limit: int = 50,
    offset: int = 0,
) -> tuple[list[WalkForwardRun], int]:
    base = select(WalkForwardRun)
    total = session.execute(
        select(func.count()).select_from(base.subquery())
    ).scalar_one()
    runs = session.execute(
        base.order_by(desc(WalkForwardRun.created_at))
        .limit(limit)
        .offset(offset)
        .options(selectinload(WalkForwardRun.windows))
    ).scalars().all()
    return list(runs), total


def delete_walkforward_run(session: Session, run_id: uuid.UUID) -> bool:
    run = get_walkforward_run(session, run_id)
    if run is None:
        return False
    session.delete(run)
    session.flush()
    return True


def create_strategy_search_run(
    session: Session,
    *,
    name: str,
    config: dict[str, Any],
    status: str = RunStatus.PENDING.value,
    started_at: Optional[datetime] = None,
) -> StrategySearchRun:
    run = StrategySearchRun(
        name=name,
        config=config,
        status=status,
        started_at=started_at,
    )
    session.add(run)
    session.flush()
    return run


def update_strategy_search_run(
    session: Session,
    run_id: uuid.UUID,
    *,
    status: Optional[str] = None,
    config: Optional[dict[str, Any]] = None,
    result_summary: Optional[dict[str, Any]] = None,
    lake_paths: Optional[dict[str, Any]] = None,
    error_message: Optional[str] = None,
    finished_at: Optional[datetime] = None,
    started_at: Optional[datetime] = None,
    clear_error_message: bool = False,
) -> StrategySearchRun:
    run = session.get(StrategySearchRun, run_id)
    if run is None:
        raise ValueError(f"StrategySearchRun {run_id} not found")
    if status is not None:
        run.status = status
    if config is not None:
        run.config = config
    if result_summary is not None:
        run.result_summary = result_summary
    if lake_paths is not None:
        run.lake_paths = lake_paths
    if clear_error_message:
        run.error_message = None
    elif error_message is not None:
        run.error_message = error_message
    if finished_at is not None:
        run.finished_at = finished_at
    if started_at is not None:
        run.started_at = started_at
    session.flush()
    return run


def create_strategy_search_candidate(
    session: Session,
    *,
    run_id: uuid.UUID,
    candidate_id: str,
    strategy: str,
    status: str,
    rank: Optional[int] = None,
    objective_value: Optional[float] = None,
    robustness_score: Optional[float] = None,
    efficiency: Optional[float] = None,
    gate_flags: Optional[list[str]] = None,
    passed_gates: bool = False,
    oos_metrics: Optional[dict[str, Any]] = None,
    is_metrics_summary: Optional[dict[str, Any]] = None,
    best_params: Optional[dict[str, Any]] = None,
    window_count: int = 0,
    completed_windows: int = 0,
) -> StrategySearchCandidate:
    candidate = StrategySearchCandidate(
        run_id=run_id,
        candidate_id=candidate_id,
        strategy=strategy,
        status=status,
        rank=rank,
        objective_value=objective_value,
        robustness_score=robustness_score,
        efficiency=efficiency,
        gate_flags=gate_flags or [],
        passed_gates=passed_gates,
        oos_metrics=oos_metrics,
        is_metrics_summary=is_metrics_summary,
        best_params=best_params,
        window_count=window_count,
        completed_windows=completed_windows,
    )
    session.add(candidate)
    session.flush()
    return candidate


def get_strategy_search_run(
    session: Session, run_id: uuid.UUID
) -> Optional[StrategySearchRun]:
    return session.execute(
        select(StrategySearchRun)
        .where(StrategySearchRun.id == run_id)
        .options(selectinload(StrategySearchRun.candidates))
    ).scalar_one_or_none()


def list_strategy_search_runs(
    session: Session,
    *,
    limit: int = 50,
    offset: int = 0,
) -> tuple[list[StrategySearchRun], int]:
    base = select(StrategySearchRun)
    total = session.execute(
        select(func.count()).select_from(base.subquery())
    ).scalar_one()
    runs = session.execute(
        base.order_by(desc(StrategySearchRun.created_at))
        .limit(limit)
        .offset(offset)
        .options(selectinload(StrategySearchRun.candidates))
    ).scalars().all()
    return list(runs), total


def delete_strategy_search_run(session: Session, run_id: uuid.UUID) -> bool:
    run = get_strategy_search_run(session, run_id)
    if run is None:
        return False
    session.delete(run)
    session.flush()
    return True
