"""Neural latent evaluation gate — IC vs classical baseline (WO144).

Latents are evaluated through the existing ``run_evaluation`` pipeline (no fork).
The baseline is the best ``|IC|`` among classical features on the same
``(symbol, timeframe, target, horizon, OOS window)``.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timezone

import pandas as pd
from sqlalchemy import desc, select
from sqlalchemy.orm import Session

from q_backend.features.evaluation import MIN_OBS
from q_backend.features.evaluation_service import run_evaluation
from q_backend.features.matrix import FeatureRequest
from q_backend.features.registry import (
    FEATURE_SPECS,
    list_feature_specs,
    neural_catalog_key,
    register_neural_model_features,
)
from q_backend.features.targets import TargetSpec, _TARGET_KINDS
from q_backend.market_data.read_through import read_ohlcv_fresh
from q_backend.storage.db.models import EvaluationRun, FeatureScoreRow, NeuralModelStatus
from q_backend.storage.db.models import NeuralModelVersion, RunStatus
from q_backend.storage.db.repositories import (
    get_neural_model_version,
    set_neural_model_status,
)

# Extra headroom required above the classical baseline for a pass (0 = strict beat).
GATE_MARGIN: float = 0.0


@dataclass(frozen=True)
class LatentGateResult:
    model_hash: str
    baseline_ic: float
    best_latent_ic: float
    n_latents_beating_baseline: int
    passed: bool
    evaluation_run_id: str | None = None


def _to_utc_timestamp(value: datetime | pd.Timestamp) -> pd.Timestamp:
    ts = pd.Timestamp(value)
    if ts.tzinfo is None:
        return ts.tz_localize("UTC")
    return ts.tz_convert("UTC")


def _classical_feature_names() -> frozenset[str]:
    return frozenset(spec.name for spec in list_feature_specs() if spec.source == "classical")


def _target_spec(target_name: str, horizon: int) -> TargetSpec:
    if target_name not in _TARGET_KINDS:
        raise ValueError(f"Unknown target '{target_name}'.")
    if horizon <= 0:
        raise ValueError(f"horizon must be positive, got {horizon}")
    return TargetSpec(
        name=target_name,
        horizon=horizon,
        kind=_TARGET_KINDS[target_name],
    )


def _classical_feature_requests() -> list[FeatureRequest]:
    return [
        FeatureRequest(name=spec.name, version=None, params={})
        for spec in list_feature_specs()
        if spec.source == "classical"
    ]


def resolve_oos_evaluation_range(
    version: NeuralModelVersion,
    *,
    end: datetime | None = None,
) -> tuple[datetime, datetime]:
    """Return ``(start, end)`` for scoring strictly OOS (first bar after ``train_end``)."""
    if version.model is None:
        raise ValueError(f"Neural model version '{version.model_hash}' has no parent model.")

    train_end_ts = _to_utc_timestamp(version.train_end)
    query_end = _to_utc_timestamp(end or datetime.now(timezone.utc))
    bars = read_ohlcv_fresh(
        version.model.symbol,
        version.model.timeframe,
        train_end_ts.to_pydatetime(),
        query_end.to_pydatetime(),
    )
    oos_times = [bar.time for bar in bars if _to_utc_timestamp(bar.time) > train_end_ts]
    if len(oos_times) < MIN_OBS:
        raise ValueError(f"Need at least {MIN_OBS} OOS bars after train_end; found {len(oos_times)}.")

    oos_start = _to_utc_timestamp(oos_times[0]).to_pydatetime()
    oos_end = _to_utc_timestamp(oos_times[-1]).to_pydatetime()
    if oos_start <= train_end_ts.to_pydatetime():
        raise AssertionError("OOS evaluation range must start strictly after train_end.")
    return oos_start, oos_end


def latent_feature_set(version: NeuralModelVersion) -> list[FeatureRequest]:
    """One ``FeatureRequest`` per registered latent of ``version``."""
    keys = [neural_catalog_key(name, version.model_hash) for name in version.latent_names]
    if not keys:
        raise ValueError(f"Model '{version.model_hash}' has no latent names.")

    if any(key not in FEATURE_SPECS for key in keys):
        register_neural_model_features(version)

    return [FeatureRequest(name=key, version=None, params={}) for key in keys]


def _baseline_threshold(baseline_ic: float) -> float:
    return baseline_ic * (1.0 + GATE_MARGIN)


def _max_abs_ic(rows: list[FeatureScoreRow], *, feature_names: frozenset[str] | None = None) -> float:
    values: list[float] = []
    for row in rows:
        if feature_names is not None and row.feature_name not in feature_names:
            continue
        if row.ic is None or (isinstance(row.ic, float) and math.isnan(row.ic)):
            continue
        values.append(abs(float(row.ic)))
    if not values:
        return 0.0
    return max(values)


def _load_score_rows(session: Session, run_id) -> list[FeatureScoreRow]:
    return list(session.execute(select(FeatureScoreRow).where(FeatureScoreRow.run_id == run_id)).scalars())


def _find_completed_run(
    session: Session,
    *,
    symbol: str,
    timeframe: str,
    target_name: str,
    horizon: int,
    start: datetime,
    end: datetime,
) -> EvaluationRun | None:
    start_ts = _to_utc_timestamp(start)
    end_ts = _to_utc_timestamp(end)
    runs = session.execute(
        select(EvaluationRun)
        .where(
            EvaluationRun.symbol == symbol,
            EvaluationRun.timeframe == timeframe.upper(),
            EvaluationRun.target_name == target_name,
            EvaluationRun.target_horizon == horizon,
            EvaluationRun.status == RunStatus.COMPLETED.value,
        )
        .order_by(desc(EvaluationRun.finished_at), desc(EvaluationRun.created_at))
    ).scalars()

    for run in runs:
        if _to_utc_timestamp(run.start) == start_ts and _to_utc_timestamp(run.end) == end_ts:
            return run
    return None


def classical_baseline_ic(
    session: Session,
    *,
    symbol: str,
    timeframe: str,
    target_name: str,
    horizon: int,
    start: datetime,
    end: datetime,
) -> float:
    """Best ``|IC|`` among classical features on the same eval slice.

    Uses the latest completed classical ``EvaluationRun`` on the exact
    ``(symbol, timeframe, target, horizon, start, end)`` when present; otherwise
    runs a fresh classical-only evaluation via ``run_evaluation``.
    """
    existing = _find_completed_run(
        session,
        symbol=symbol,
        timeframe=timeframe,
        target_name=target_name,
        horizon=horizon,
        start=start,
        end=end,
    )
    classical_names = _classical_feature_names()

    if existing is not None:
        rows = _load_score_rows(session, existing.id)
        classical_rows = [row for row in rows if row.feature_name in classical_names]
        if classical_rows:
            return _max_abs_ic(classical_rows)

    target = _target_spec(target_name, horizon)
    run = run_evaluation(
        session,
        symbol=symbol,
        timeframe=timeframe,
        start=start,
        end=end,
        target=target,
        feature_set=_classical_feature_requests(),
    )
    rows = _load_score_rows(session, run.id)
    return _max_abs_ic(rows, feature_names=classical_names)


def _latent_catalog_keys(version: NeuralModelVersion) -> frozenset[str]:
    return frozenset(neural_catalog_key(name, version.model_hash) for name in version.latent_names)


def find_latest_latent_evaluation_run(session: Session, version: NeuralModelVersion) -> EvaluationRun | None:
    """Return the newest completed evaluation run for this version's latents."""
    latent_keys = _latent_catalog_keys(version)
    if not latent_keys:
        return None

    runs = session.execute(
        select(EvaluationRun)
        .join(FeatureScoreRow, FeatureScoreRow.run_id == EvaluationRun.id)
        .where(
            FeatureScoreRow.feature_name.in_(latent_keys),
            EvaluationRun.status == RunStatus.COMPLETED.value,
        )
        .order_by(desc(EvaluationRun.finished_at), desc(EvaluationRun.created_at))
    ).scalars()

    for run in runs:
        rows = _load_score_rows(session, run.id)
        row_names = {row.feature_name for row in rows}
        if row_names and row_names <= latent_keys:
            return run
    return None


def read_latest_latent_gate_result(session: Session, version: NeuralModelVersion) -> LatentGateResult | None:
    """Read the persisted latest gate result without running a new evaluation."""
    run = find_latest_latent_evaluation_run(session, version)
    if run is None:
        return None

    latent_keys = _latent_catalog_keys(version)
    rows = _load_score_rows(session, run.id)
    latent_scores = [row for row in rows if row.feature_name in latent_keys]
    best_latent_ic = _max_abs_ic(latent_scores)

    baseline_ic = 0.0
    classical_run = _find_completed_run(
        session,
        symbol=run.symbol,
        timeframe=run.timeframe,
        target_name=run.target_name,
        horizon=run.target_horizon,
        start=run.start,
        end=run.end,
    )
    if classical_run is not None:
        classical_rows = _load_score_rows(session, classical_run.id)
        classical_names = _classical_feature_names()
        classical_only = [row for row in classical_rows if row.feature_name in classical_names]
        if classical_only:
            baseline_ic = _max_abs_ic(classical_only)

    threshold = _baseline_threshold(baseline_ic)
    n_beating = sum(
        1
        for row in latent_scores
        if row.ic is not None
        and not (isinstance(row.ic, float) and math.isnan(row.ic))
        and abs(float(row.ic)) > threshold
    )

    return LatentGateResult(
        model_hash=version.model_hash,
        baseline_ic=baseline_ic,
        best_latent_ic=best_latent_ic,
        n_latents_beating_baseline=n_beating,
        passed=best_latent_ic > threshold,
        evaluation_run_id=str(run.id),
    )


def evaluate_latents(
    session: Session,
    version: NeuralModelVersion,
    *,
    target_name: str,
    horizon: int,
    end: datetime | None = None,
) -> LatentGateResult:
    """Evaluate model latents OOS and compare against the classical baseline."""
    if version.model is None:
        raise ValueError(f"Neural model version '{version.model_hash}' has no parent model.")

    oos_start, oos_end = resolve_oos_evaluation_range(version, end=end)
    symbol = version.model.symbol
    timeframe = version.model.timeframe
    target = _target_spec(target_name, horizon)

    baseline_ic = classical_baseline_ic(
        session,
        symbol=symbol,
        timeframe=timeframe,
        target_name=target_name,
        horizon=horizon,
        start=oos_start,
        end=oos_end,
    )
    threshold = _baseline_threshold(baseline_ic)

    latent_run = run_evaluation(
        session,
        symbol=symbol,
        timeframe=timeframe,
        start=oos_start,
        end=oos_end,
        target=target,
        feature_set=latent_feature_set(version),
    )
    latent_rows = _load_score_rows(session, latent_run.id)
    latent_keys = frozenset(neural_catalog_key(name, version.model_hash) for name in version.latent_names)
    latent_scores = [row for row in latent_rows if row.feature_name in latent_keys]

    best_latent_ic = _max_abs_ic(latent_scores)
    n_beating = sum(
        1
        for row in latent_scores
        if row.ic is not None
        and not (isinstance(row.ic, float) and math.isnan(row.ic))
        and abs(float(row.ic)) > threshold
    )
    passed = best_latent_ic > threshold

    if passed:
        current = get_neural_model_version(session, version.model_hash)
        if current is not None and current.status == NeuralModelStatus.TRAINED.value:
            set_neural_model_status(
                session,
                model_hash=version.model_hash,
                status=NeuralModelStatus.CANDIDATE.value,
            )

    return LatentGateResult(
        model_hash=version.model_hash,
        baseline_ic=baseline_ic,
        best_latent_ic=best_latent_ic,
        n_latents_beating_baseline=n_beating,
        passed=passed,
        evaluation_run_id=str(latent_run.id),
    )
