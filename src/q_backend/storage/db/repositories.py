import uuid
from datetime import datetime, timezone
from typing import Any, Optional

from sqlalchemy import Float, cast, desc, func, nulls_last, select, update
from sqlalchemy.orm import Session, selectinload

from q_backend.storage.db.models import (
    BacktestConfig,
    BacktestRun,
    DataIngestionRun,
    EvaluationRun,
    FeatureDefinition,
    FeatureEvidenceRow,
    FeatureScoreRow,
    FeatureStatus,
    FeatureVersion,
    NeuralModel,
    NeuralModelStatus,
    NeuralModelVersion,
    OptimizationStudy,
    OptimizationTrial,
    RunStatus,
    Strategy,
    StrategySearchCandidate,
    StrategySearchRun,
    StrategyVersion,
    TrialStatus,
    WalkForwardRun,
    WalkForwardWindow,
)
from q_backend.streaming.jobs import (
    STATUS_TO_STREAM,
    clear_job_terminal_marker,
    record_job_terminal,
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
    values: dict[str, Any] = {"status": RunStatus.CANCELLED.value}
    if error_message is not None and hasattr(model, "error_message"):
        values["error_message"] = error_message
    if hasattr(model, "finished_at"):
        values["finished_at"] = datetime.now(timezone.utc)
    result = session.execute(update(model).where(model.status.in_(_ACTIVE_RUN_STATUSES)).values(**values))
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
    strategy = session.execute(select(Strategy).where(Strategy.name == name)).scalar_one_or_none()
    if strategy is None:
        strategy = create_strategy(session, name=name)
    return strategy


def get_backtest_run(session: Session, run_id: uuid.UUID) -> Optional[BacktestRun]:
    return session.get(BacktestRun, run_id)


def find_backtest_run_by_config(session: Session, config: dict[str, Any]) -> Optional[BacktestRun]:
    """Return the newest persisted run whose stored config matches exactly."""
    return session.execute(
        select(BacktestRun).where(BacktestRun.config == config).order_by(desc(BacktestRun.created_at)).limit(1)
    ).scalar_one_or_none()


def delete_backtest_run(session: Session, run_id: uuid.UUID) -> bool:
    backtest_run = session.get(BacktestRun, run_id)
    if backtest_run is None:
        return False
    session.delete(backtest_run)
    session.flush()
    return True


def delete_backtest_runs(session: Session, run_ids: list[uuid.UUID]) -> tuple[int, list[uuid.UUID]]:
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


def delete_optimization_studies(session: Session, study_ids: list[uuid.UUID]) -> tuple[int, list[uuid.UUID]]:
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

    total = session.execute(select(func.count()).select_from(base.subquery())).scalar_one()

    order_clauses = _backtest_run_order(sort)
    runs = session.execute(base.order_by(*order_clauses).limit(limit).offset(offset)).scalars().all()
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

    if status is not None:
        stream_status = STATUS_TO_STREAM.get(status.lower())
        if stream_status in ("completed", "failed", "cancelled"):
            record_job_terminal(
                session,
                kind="backtest",
                job_id=str(run_id),
                raw_status=status,
                error=error_message or backtest_run.error_message,
                finished_at=finished_at or backtest_run.finished_at,
            )
        elif stream_status == "running":
            clear_job_terminal_marker(session, "backtest", str(run_id))

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

    if status is not None:
        stream_status = STATUS_TO_STREAM.get(status.lower())
        job_id = study_id.hex if hasattr(study_id, "hex") else str(study_id).replace("-", "")
        if stream_status in ("completed", "failed", "cancelled"):
            err = None
            if config and isinstance(config, dict) and "persisted_snapshot" in config:
                err = config["persisted_snapshot"].get("error")
            record_job_terminal(
                session,
                kind="optimization",
                job_id=job_id,
                raw_status=status,
                error=err,
            )
        elif stream_status == "running":
            clear_job_terminal_marker(session, "optimization", job_id)

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


def get_optimization_study(session: Session, study_id: uuid.UUID) -> Optional[OptimizationStudy]:
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
    total = session.execute(select(func.count()).select_from(base.subquery())).scalar_one()
    studies = (
        session.execute(
            base.order_by(desc(OptimizationStudy.created_at))
            .limit(limit)
            .offset(offset)
            .options(selectinload(OptimizationStudy.trials))
        )
        .scalars()
        .all()
    )
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

    if status is not None:
        stream_status = STATUS_TO_STREAM.get(status.lower())
        if stream_status in ("completed", "failed", "cancelled"):
            record_job_terminal(
                session,
                kind="walkforward",
                job_id=str(run_id),
                raw_status=status,
                error=error_message or run.error_message,
                finished_at=finished_at or run.finished_at,
            )
        elif stream_status == "running":
            clear_job_terminal_marker(session, "walkforward", str(run_id))

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


def get_walkforward_run(session: Session, run_id: uuid.UUID) -> Optional[WalkForwardRun]:
    return session.execute(
        select(WalkForwardRun).where(WalkForwardRun.id == run_id).options(selectinload(WalkForwardRun.windows))
    ).scalar_one_or_none()


def list_walkforward_runs(
    session: Session,
    *,
    limit: int = 50,
    offset: int = 0,
) -> tuple[list[WalkForwardRun], int]:
    base = select(WalkForwardRun)
    total = session.execute(select(func.count()).select_from(base.subquery())).scalar_one()
    runs = (
        session.execute(
            base.order_by(desc(WalkForwardRun.created_at))
            .limit(limit)
            .offset(offset)
            .options(selectinload(WalkForwardRun.windows))
        )
        .scalars()
        .all()
    )
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

    if status is not None:
        stream_status = STATUS_TO_STREAM.get(status.lower())
        if stream_status in ("completed", "failed", "cancelled"):
            record_job_terminal(
                session,
                kind="strategy_search",
                job_id=str(run_id),
                raw_status=status,
                error=error_message or run.error_message,
                finished_at=finished_at or run.finished_at,
            )
        elif stream_status == "running":
            clear_job_terminal_marker(session, "strategy_search", str(run_id))

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
    generation: Optional[int] = None,
    genome: Optional[dict[str, Any]] = None,
    genome_node_count: Optional[int] = None,
    dsr: Optional[float] = None,
    complexity_penalty: Optional[float] = None,
    exit_preset_id: Optional[str] = None,
    exit_preset_label: Optional[str] = None,
    exit_policy_id: Optional[str] = None,
    exit_policy_label: Optional[str] = None,
    last_exit_mutation_op: Optional[str] = None,
    exit_param_names: Optional[list[str]] = None,
    diagnostics: Optional[dict[str, Any]] = None,
    profile_version: Optional[int] = None,
    hypothesis_id: Optional[str] = None,
    hypothesis_rationale: Optional[str] = None,
    hypothesis_required_features: Optional[list[str]] = None,
    hypothesis_template_hash: Optional[str] = None,
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
        generation=generation,
        genome=genome,
        genome_node_count=genome_node_count,
        dsr=dsr,
        complexity_penalty=complexity_penalty,
        exit_preset_id=exit_preset_id,
        exit_preset_label=exit_preset_label,
        exit_policy_id=exit_policy_id,
        exit_policy_label=exit_policy_label,
        last_exit_mutation_op=last_exit_mutation_op,
        exit_param_names=exit_param_names,
        diagnostics=diagnostics,
        profile_version=profile_version,
        hypothesis_id=hypothesis_id,
        hypothesis_rationale=hypothesis_rationale,
        hypothesis_required_features=hypothesis_required_features,
        hypothesis_template_hash=hypothesis_template_hash,
    )
    session.add(candidate)
    session.flush()
    return candidate


def get_strategy_search_run(session: Session, run_id: uuid.UUID) -> Optional[StrategySearchRun]:
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
    total = session.execute(select(func.count()).select_from(base.subquery())).scalar_one()
    runs = (
        session.execute(
            base.order_by(desc(StrategySearchRun.created_at))
            .limit(limit)
            .offset(offset)
            .options(selectinload(StrategySearchRun.candidates))
        )
        .scalars()
        .all()
    )
    return list(runs), total


def delete_strategy_search_run(session: Session, run_id: uuid.UUID) -> bool:
    run = get_strategy_search_run(session, run_id)
    if run is None:
        return False
    session.delete(run)
    session.flush()
    return True


def get_strategy_search_candidate(
    session: Session,
    *,
    run_id: uuid.UUID,
    candidate_id: str,
) -> Optional[StrategySearchCandidate]:
    return session.execute(
        select(StrategySearchCandidate).where(
            StrategySearchCandidate.run_id == run_id,
            StrategySearchCandidate.candidate_id == candidate_id,
        )
    ).scalar_one_or_none()


_FEATURE_STATUS_RANK = {
    FeatureStatus.EXPERIMENTAL.value: 0,
    FeatureStatus.CANDIDATE.value: 1,
    FeatureStatus.PRODUCTION.value: 2,
}


def _feature_status_rank(status: str) -> int:
    return _FEATURE_STATUS_RANK.get(status, 0)


def upsert_feature_definition(
    session: Session,
    *,
    name: str,
    category: str,
    description: Optional[str] = None,
) -> FeatureDefinition:
    definition = session.execute(select(FeatureDefinition).where(FeatureDefinition.name == name)).scalar_one_or_none()
    if definition is None:
        definition = FeatureDefinition(
            name=name,
            category=category,
            description=description,
            usage_count=0,
        )
        session.add(definition)
    else:
        definition.category = category
        definition.description = description
    session.flush()
    return definition


def upsert_feature_version(
    session: Session,
    *,
    definition_id: uuid.UUID,
    version: int,
    status: str,
    node_kind: str,
    param_keys: list[str],
    default_params: dict[str, Any],
    forward_window: int,
    leakage_status: str,
    provenance: dict[str, Any],
) -> FeatureVersion:
    feature_version = session.execute(
        select(FeatureVersion).where(
            FeatureVersion.definition_id == definition_id,
            FeatureVersion.version == version,
        )
    ).scalar_one_or_none()
    if feature_version is None:
        feature_version = FeatureVersion(
            definition_id=definition_id,
            version=version,
            status=status,
            node_kind=node_kind,
            param_keys=param_keys,
            default_params=default_params,
            forward_window=forward_window,
            leakage_status=leakage_status,
            provenance=provenance,
        )
        session.add(feature_version)
    else:
        feature_version.node_kind = node_kind
        feature_version.param_keys = param_keys
        feature_version.default_params = default_params
        feature_version.forward_window = forward_window
        feature_version.leakage_status = leakage_status
        feature_version.provenance = provenance
        if _feature_status_rank(status) > _feature_status_rank(feature_version.status):
            feature_version.status = status
    session.flush()
    return feature_version


def get_feature_definition(session: Session, name: str) -> Optional[FeatureDefinition]:
    return session.execute(
        select(FeatureDefinition)
        .where(FeatureDefinition.name == name)
        .options(selectinload(FeatureDefinition.versions))
    ).scalar_one_or_none()


def list_feature_definitions(
    session: Session,
    *,
    category: Optional[str] = None,
    status: Optional[str] = None,
) -> list[FeatureDefinition]:
    stmt = select(FeatureDefinition).options(selectinload(FeatureDefinition.versions))
    if category is not None:
        stmt = stmt.where(FeatureDefinition.category == category)
    if status is not None:
        stmt = stmt.where(FeatureDefinition.versions.any(FeatureVersion.status == status))
    definitions = session.execute(stmt.order_by(FeatureDefinition.name)).scalars().all()
    return list(definitions)


def set_feature_status(
    session: Session,
    *,
    name: str,
    version: int,
    status: str,
) -> FeatureVersion:
    definition = session.execute(select(FeatureDefinition).where(FeatureDefinition.name == name)).scalar_one_or_none()
    if definition is None:
        raise ValueError(f"FeatureDefinition '{name}' not found")
    feature_version = session.execute(
        select(FeatureVersion).where(
            FeatureVersion.definition_id == definition.id,
            FeatureVersion.version == version,
        )
    ).scalar_one_or_none()
    if feature_version is None:
        raise ValueError(f"FeatureVersion '{name}' v{version} not found")
    feature_version.status = status
    session.flush()
    return feature_version


def increment_feature_usage(
    session: Session,
    *,
    name: str,
    n: int = 1,
) -> FeatureDefinition:
    result = session.execute(
        update(FeatureDefinition)
        .where(FeatureDefinition.name == name)
        .values(usage_count=FeatureDefinition.usage_count + n)
    )
    if not result.rowcount:
        raise ValueError(f"FeatureDefinition '{name}' not found")
    session.flush()
    definition = session.execute(select(FeatureDefinition).where(FeatureDefinition.name == name)).scalar_one()
    return definition


def create_evaluation_run(
    session: Session,
    *,
    symbol: str,
    timeframe: str,
    start: datetime,
    end: datetime,
    target_name: str,
    target_horizon: int,
    matrix_id: str,
    status: str = RunStatus.PENDING.value,
    feature_count: int = 0,
    result_summary: Optional[dict[str, Any]] = None,
    error_message: Optional[str] = None,
    started_at: Optional[datetime] = None,
    finished_at: Optional[datetime] = None,
) -> EvaluationRun:
    run = EvaluationRun(
        symbol=symbol,
        timeframe=timeframe,
        start=start,
        end=end,
        target_name=target_name,
        target_horizon=target_horizon,
        matrix_id=matrix_id,
        status=status,
        feature_count=feature_count,
        result_summary=result_summary,
        error_message=error_message,
        started_at=started_at,
        finished_at=finished_at,
    )
    session.add(run)
    session.flush()
    return run


def update_evaluation_run(
    session: Session,
    run_id: uuid.UUID,
    *,
    status: Optional[str] = None,
    matrix_id: Optional[str] = None,
    feature_count: Optional[int] = None,
    result_summary: Optional[dict[str, Any]] = None,
    error_message: Optional[str] = None,
    started_at: Optional[datetime] = None,
    finished_at: Optional[datetime] = None,
    clear_error_message: bool = False,
) -> EvaluationRun:
    run = session.get(EvaluationRun, run_id)
    if run is None:
        raise ValueError(f"EvaluationRun {run_id} not found")
    if status is not None:
        run.status = status
    if matrix_id is not None:
        run.matrix_id = matrix_id
    if feature_count is not None:
        run.feature_count = feature_count
    if result_summary is not None:
        run.result_summary = result_summary
    if clear_error_message:
        run.error_message = None
    elif error_message is not None:
        run.error_message = error_message
    if started_at is not None:
        run.started_at = started_at
    if finished_at is not None:
        run.finished_at = finished_at
    session.flush()
    return run


def get_evaluation_run(session: Session, run_id: uuid.UUID) -> Optional[EvaluationRun]:
    return session.execute(
        select(EvaluationRun).where(EvaluationRun.id == run_id).options(selectinload(EvaluationRun.scores))
    ).scalar_one_or_none()


def create_feature_score_row(
    session: Session,
    *,
    run_id: uuid.UUID,
    feature_id: str,
    feature_name: str,
    ic: Optional[float] = None,
    rank_ic: Optional[float] = None,
    mutual_info: Optional[float] = None,
    stability: Optional[float] = None,
    global_score: Optional[float] = None,
    cluster_id: int,
    is_representative: bool,
    leakage_status: str,
    regime_ics: Optional[dict[str, Any]] = None,
) -> FeatureScoreRow:
    row = FeatureScoreRow(
        run_id=run_id,
        feature_id=feature_id,
        feature_name=feature_name,
        ic=ic,
        rank_ic=rank_ic,
        mutual_info=mutual_info,
        stability=stability,
        global_score=global_score,
        cluster_id=cluster_id,
        is_representative=is_representative,
        leakage_status=leakage_status,
        regime_ics=regime_ics or {},
    )
    session.add(row)
    session.flush()
    return row


def get_latest_global_scores(session: Session) -> dict[str, float]:
    latest = (
        select(
            FeatureScoreRow.feature_name,
            func.max(FeatureScoreRow.created_at).label("max_created"),
        )
        .group_by(FeatureScoreRow.feature_name)
        .subquery()
    )
    rows = session.execute(
        select(FeatureScoreRow.feature_name, FeatureScoreRow.global_score).join(
            latest,
            (FeatureScoreRow.feature_name == latest.c.feature_name)
            & (FeatureScoreRow.created_at == latest.c.max_created),
        )
    ).all()
    scores: dict[str, float] = {}
    for name, value in rows:
        if value is not None:
            scores[name] = float(value)
    return scores


_NEURAL_MODEL_STATUS_RANK = {
    NeuralModelStatus.TRAINED.value: 0,
    NeuralModelStatus.CANDIDATE.value: 1,
    NeuralModelStatus.PRODUCTION.value: 2,
    NeuralModelStatus.ARCHIVED.value: 3,
}


def _neural_model_status_rank(status: str) -> int:
    return _NEURAL_MODEL_STATUS_RANK.get(status, 0)


def create_neural_model(
    session: Session,
    *,
    model_key: str,
    kind: str,
    symbol: str,
    timeframe: str,
) -> NeuralModel:
    model = session.execute(select(NeuralModel).where(NeuralModel.model_key == model_key)).scalar_one_or_none()
    if model is None:
        model = NeuralModel(
            model_key=model_key,
            kind=kind,
            symbol=symbol,
            timeframe=timeframe,
        )
        session.add(model)
    else:
        model.kind = kind
        model.symbol = symbol
        model.timeframe = timeframe
    session.flush()
    return model


def _next_neural_model_version(session: Session, model_id: uuid.UUID) -> int:
    current = session.execute(
        select(func.max(NeuralModelVersion.version)).where(NeuralModelVersion.model_id == model_id)
    ).scalar_one_or_none()
    return int(current or 0) + 1


def create_neural_model_version(
    session: Session,
    *,
    model_id: uuid.UUID,
    model_hash: str,
    version: int | None = None,
    status: str = NeuralModelStatus.TRAINED.value,
    train_start: datetime,
    train_end: datetime,
    n_latents: int,
    input_features: list[str],
    hyperparams: dict[str, Any],
    val_metrics: dict[str, Any],
    latent_names: list[str],
    artifact_path: str,
) -> NeuralModelVersion:
    existing = session.execute(
        select(NeuralModelVersion).where(NeuralModelVersion.model_hash == model_hash)
    ).scalar_one_or_none()
    if existing is not None:
        existing.train_start = train_start
        existing.train_end = train_end
        existing.n_latents = n_latents
        existing.input_features = input_features
        existing.hyperparams = hyperparams
        existing.val_metrics = val_metrics
        existing.latent_names = latent_names
        existing.artifact_path = artifact_path
        if _neural_model_status_rank(status) > _neural_model_status_rank(existing.status):
            existing.status = status
        session.flush()
        return existing

    resolved_version = version if version is not None else _next_neural_model_version(session, model_id)
    model_version = NeuralModelVersion(
        model_id=model_id,
        model_hash=model_hash,
        version=resolved_version,
        status=status,
        train_start=train_start,
        train_end=train_end,
        n_latents=n_latents,
        input_features=input_features,
        hyperparams=hyperparams,
        val_metrics=val_metrics,
        latent_names=latent_names,
        artifact_path=artifact_path,
    )
    session.add(model_version)
    session.flush()
    return model_version


def get_neural_model_version(session: Session, model_hash: str) -> Optional[NeuralModelVersion]:
    return session.execute(
        select(NeuralModelVersion)
        .where(NeuralModelVersion.model_hash == model_hash)
        .options(selectinload(NeuralModelVersion.model))
    ).scalar_one_or_none()


def list_neural_model_versions(
    session: Session,
    *,
    status: Optional[str] = None,
) -> list[NeuralModelVersion]:
    stmt = select(NeuralModelVersion).options(selectinload(NeuralModelVersion.model))
    if status is not None:
        stmt = stmt.where(NeuralModelVersion.status == status)
    versions = session.execute(stmt.order_by(desc(NeuralModelVersion.created_at))).scalars().all()
    return list(versions)


def set_neural_model_status(
    session: Session,
    *,
    model_hash: str,
    status: str,
) -> NeuralModelVersion:
    model_version = session.execute(
        select(NeuralModelVersion).where(NeuralModelVersion.model_hash == model_hash)
    ).scalar_one_or_none()
    if model_version is None:
        raise ValueError(f"NeuralModelVersion with hash '{model_hash}' not found")
    model_version.status = status
    session.flush()
    return model_version


def get_feature_evaluation_history(
    session: Session,
    feature_name: str,
    *,
    limit: int = 20,
) -> list[dict[str, Any]]:
    rows = session.execute(
        select(FeatureScoreRow, EvaluationRun)
        .join(EvaluationRun, FeatureScoreRow.run_id == EvaluationRun.id)
        .where(FeatureScoreRow.feature_name == feature_name)
        .order_by(
            nulls_last(desc(EvaluationRun.finished_at)),
            desc(EvaluationRun.created_at),
        )
        .limit(limit)
    ).all()
    history: list[dict[str, Any]] = []
    for score_row, run in rows:
        evaluated_at = run.finished_at or run.created_at
        history.append(
            {
                "run_id": str(run.id),
                "target": f"{run.target_name}:{run.target_horizon}",
                "rank_ic": score_row.rank_ic,
                "global_score": score_row.global_score,
                "evaluated_at": evaluated_at.isoformat(),
            }
        )
    return history


def create_feature_evidence_row(
    session: Session,
    *,
    profile_id: str,
    profile_version: int,
    feature_id: str,
    feature_version: int,
    feature_name: str,
    node_kind: str | None,
    target: str,
    horizon: int,
    split_manifest_hash: str,
    data_fingerprint: str,
    ic: float | None,
    rank_ic: float | None,
    mutual_info: float | None,
    sign_consistency: float | None,
    median_effect: float | None,
    effect_dispersion: float | None,
    n_obs: int,
    permutation_null_floor: float | None,
    deflated_score: float | None,
    decision: str,
    rejection_reasons: list[str],
    leakage_status: str,
    is_representative: bool,
    cluster_id: int,
    attempted_feature_count: int,
    diagnostics_artifact_id: str,
    status: str,
) -> FeatureEvidenceRow:
    row = FeatureEvidenceRow(
        profile_id=profile_id,
        profile_version=profile_version,
        feature_id=feature_id,
        feature_version=feature_version,
        feature_name=feature_name,
        node_kind=node_kind,
        target=target,
        horizon=horizon,
        split_manifest_hash=split_manifest_hash,
        data_fingerprint=data_fingerprint,
        ic=ic,
        rank_ic=rank_ic,
        mutual_info=mutual_info,
        sign_consistency=sign_consistency,
        median_effect=median_effect,
        effect_dispersion=effect_dispersion,
        n_obs=n_obs,
        permutation_null_floor=permutation_null_floor,
        deflated_score=deflated_score,
        decision=decision,
        rejection_reasons=rejection_reasons,
        leakage_status=leakage_status,
        is_representative=is_representative,
        cluster_id=cluster_id,
        attempted_feature_count=attempted_feature_count,
        diagnostics_artifact_id=diagnostics_artifact_id,
        status=status,
    )
    session.add(row)
    session.flush()
    return row


def get_feature_evidence(
    session: Session,
    *,
    profile_id: str,
    profile_version: int,
    feature_id: str,
    feature_version: int,
    target: str,
    horizon: int,
    split_manifest_hash: str,
) -> FeatureEvidenceRow | None:
    return session.execute(
        select(FeatureEvidenceRow).where(
            FeatureEvidenceRow.profile_id == profile_id,
            FeatureEvidenceRow.profile_version == profile_version,
            FeatureEvidenceRow.feature_id == feature_id,
            FeatureEvidenceRow.feature_version == feature_version,
            FeatureEvidenceRow.target == target,
            FeatureEvidenceRow.horizon == horizon,
            FeatureEvidenceRow.split_manifest_hash == split_manifest_hash,
        )
    ).scalar_one_or_none()


def list_feature_evidence_for_profile(
    session: Session,
    *,
    profile_id: str,
    profile_version: int,
    split_manifest_hash: str,
    data_fingerprint: str,
) -> list[FeatureEvidenceRow]:
    return list(
        session.execute(
            select(FeatureEvidenceRow)
            .where(
                FeatureEvidenceRow.profile_id == profile_id,
                FeatureEvidenceRow.profile_version == profile_version,
                FeatureEvidenceRow.split_manifest_hash == split_manifest_hash,
                FeatureEvidenceRow.data_fingerprint == data_fingerprint,
            )
            .order_by(FeatureEvidenceRow.feature_name, FeatureEvidenceRow.horizon)
        ).scalars()
    )
