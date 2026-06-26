"""Feature evaluation orchestration and persistence (WO135)."""

from __future__ import annotations

import logging
import math
import uuid
from datetime import datetime, timezone
from typing import Any

import pandas as pd
from sqlalchemy.orm import Session

from q_backend.features.evaluation import evaluate_matrix
from q_backend.features.matrix import (
    FeatureRequest,
    _ohlcv_to_compute_bars,
    build_feature_matrix,
    compute_matrix_id,
)
from q_backend.features.registry import get_feature_spec
from q_backend.features.scoring import (
    cluster_redundant,
    recommended_feature_set,
    score_features,
)
from q_backend.features.targets import TargetSpec, compute_target
from q_backend.market_data.local_store import read_ohlcv
from q_backend.storage.db.engine import session_scope
from q_backend.storage.db.models import EvaluationRun, RunStatus
from q_backend.storage.db.repositories import (
    create_evaluation_run,
    create_feature_score_row,
    get_evaluation_run,
    increment_feature_usage,
    mark_active_runs_cancelled,
    update_evaluation_run,
)

logger = logging.getLogger(__name__)


def reconcile_orphaned_eval_runs() -> int:
    """Cancel feature-eval runs left RUNNING by a previous process.

    Background tasks don't survive a restart, so any run still flagged RUNNING in
    the DB has no live worker and would otherwise make the client poll forever.
    Call once on startup. Returns the number of rows reconciled."""
    try:
        with session_scope() as session:
            count = mark_active_runs_cancelled(
                session,
                EvaluationRun,
                error_message="Cancelled after backend restart (run was orphaned).",
            )
    except Exception as exc:  # noqa: BLE001 - startup reconcile must not crash boot
        logger.warning("Failed to reconcile orphaned feature-eval runs: %s", exc)
        return 0
    if count:
        logger.info("Reconciled %d orphaned feature-eval run(s) on startup.", count)
    return count


def _nullable_float(value: float | None) -> float | None:
    if value is None:
        return None
    if isinstance(value, float) and math.isnan(value):
        return None
    return float(value)


def _json_safe_regime(regime_ics: dict[str, float]) -> dict[str, Any]:
    safe: dict[str, Any] = {}
    for key, value in regime_ics.items():
        if isinstance(value, float) and math.isnan(value):
            safe[key] = None
        else:
            safe[key] = value
    return safe


def _feature_name_map(manifest: dict[str, Any]) -> dict[str, str]:
    return {
        str(row["feature_id"]): str(row["name"])
        for row in manifest.get("features", [])
    }


def create_pending_evaluation_run(
    session: Session,
    *,
    symbol: str,
    timeframe: str,
    start: datetime,
    end: datetime,
    target: TargetSpec,
    feature_set: list[FeatureRequest],
) -> EvaluationRun:
    """Persist a RUNNING evaluation run row without doing the heavy work.

    Returns immediately so the API can hand back a ``run_id`` and let the
    evaluation execute in the background (the frontend polls for results).
    """
    if not feature_set:
        raise ValueError("feature_set must contain at least one FeatureRequest")

    matrix_id = compute_matrix_id(symbol, timeframe, start, end, feature_set)
    return create_evaluation_run(
        session,
        symbol=symbol,
        timeframe=timeframe,
        start=start,
        end=end,
        target_name=target.name,
        target_horizon=target.horizon,
        matrix_id=matrix_id,
        status=RunStatus.RUNNING.value,
        feature_count=len(feature_set),
        started_at=datetime.now(timezone.utc),
    )


def execute_evaluation_run(
    run_id: uuid.UUID,
    *,
    symbol: str,
    timeframe: str,
    start: datetime,
    end: datetime,
    target: TargetSpec,
    feature_set: list[FeatureRequest],
) -> None:
    """Run the heavy evaluation for an already-created run, in its own session.

    Background-task entry point: never raises (failures are recorded on the run
    row as ``FAILED`` so the polling client sees the error)."""
    with session_scope() as session:
        run = get_evaluation_run(session, run_id)
        if run is None:
            return
        try:
            _evaluate_into_run(
                session,
                run,
                symbol=symbol,
                timeframe=timeframe,
                start=start,
                end=end,
                target=target,
                feature_set=feature_set,
            )
        except Exception:  # noqa: BLE001 - status persisted; background context
            # _evaluate_into_run already marked the run FAILED with the message.
            pass


def run_evaluation(
    session: Session,
    *,
    symbol: str,
    timeframe: str,
    start: datetime,
    end: datetime,
    target: TargetSpec,
    feature_set: list[FeatureRequest],
) -> EvaluationRun:
    """Build matrix, evaluate, score, and persist one evaluation run (synchronous)."""
    run = create_pending_evaluation_run(
        session,
        symbol=symbol,
        timeframe=timeframe,
        start=start,
        end=end,
        target=target,
        feature_set=feature_set,
    )
    _evaluate_into_run(
        session,
        run,
        symbol=symbol,
        timeframe=timeframe,
        start=start,
        end=end,
        target=target,
        feature_set=feature_set,
    )
    persisted = get_evaluation_run(session, run.id)
    assert persisted is not None
    return persisted


def _evaluate_into_run(
    session: Session,
    run: EvaluationRun,
    *,
    symbol: str,
    timeframe: str,
    start: datetime,
    end: datetime,
    target: TargetSpec,
    feature_set: list[FeatureRequest],
) -> None:
    """Heavy lifting: build matrix, evaluate, score, persist; mark COMPLETED/FAILED."""

    def _report_progress(stage: str, processed: int, total: int) -> None:
        # Stream live progress into result_summary and commit so the polling
        # GET (a separate session) sees it advance. Overwritten by the real
        # summary on completion. Never let a progress write break the eval.
        try:
            update_evaluation_run(
                session,
                run.id,
                result_summary={
                    "stage": stage,
                    "processed_features": processed,
                    "total_features": total,
                },
            )
            session.commit()
        except Exception:  # noqa: BLE001 - progress is best-effort
            session.rollback()

    try:
        _report_progress("loading_data", 0, len(feature_set))
        matrix = build_feature_matrix(
            symbol, timeframe, start, end, feature_set
        )
        bars = read_ohlcv(symbol, timeframe, start, end)
        bars_df = _ohlcv_to_compute_bars(bars)
        target_series = compute_target(bars_df, target)
        close: pd.Series | None = None
        if not bars_df.empty:
            close = bars_df.set_index("time")["close"]

        evaluations = evaluate_matrix(
            matrix,
            target_series,
            close=close,
            bars=bars_df,
            progress_callback=lambda done, total: _report_progress(
                "evaluating", done, total
            ),
        )
        _report_progress("scoring", len(feature_set), len(feature_set))
        clusters = cluster_redundant(matrix)
        scores = score_features(evaluations, clusters)
        recommended = recommended_feature_set(scores)

        evaluation_by_id = {item.feature_id: item for item in evaluations}
        score_by_id = {item.feature_id: item for item in scores}
        names_by_id = _feature_name_map(matrix.manifest)

        for feature_id in sorted(matrix.frame.columns):
            evaluation = evaluation_by_id[feature_id]
            scored = score_by_id[feature_id]
            feature_name = names_by_id.get(feature_id, feature_id)
            try:
                evaluated_spec = get_feature_spec(feature_name)
            except KeyError:
                evaluated_spec = None
            create_feature_score_row(
                session,
                run_id=run.id,
                feature_id=feature_id,
                feature_name=feature_name,
                ic=_nullable_float(evaluation.ic),
                rank_ic=_nullable_float(evaluation.rank_ic),
                mutual_info=_nullable_float(evaluation.mutual_info),
                stability=_nullable_float(evaluation.stability),
                global_score=_nullable_float(scored.global_score),
                cluster_id=scored.cluster_id,
                is_representative=scored.is_representative,
                leakage_status=evaluation.leakage_status,
                regime_ics=_json_safe_regime(evaluation.regime_ics),
            )
            if evaluated_spec is None or evaluated_spec.source != "neural":
                increment_feature_usage(session, name=feature_name)

        top_score = max(
            (item.global_score for item in scores),
            default=float("nan"),
        )
        result_summary = {
            "recommended_feature_ids": recommended,
            "cluster_count": len(clusters),
            "top_global_score": _nullable_float(top_score),
            "matrix_id": matrix.matrix_id,
        }
        update_evaluation_run(
            session,
            run.id,
            status=RunStatus.COMPLETED.value,
            matrix_id=matrix.matrix_id,
            result_summary=result_summary,
            finished_at=datetime.now(timezone.utc),
            clear_error_message=True,
        )
    except Exception as exc:
        update_evaluation_run(
            session,
            run.id,
            status=RunStatus.FAILED.value,
            error_message=str(exc),
            finished_at=datetime.now(timezone.utc),
        )
        raise


def load_evaluation_run(
    session: Session, run_id: uuid.UUID
) -> EvaluationRun | None:
    return get_evaluation_run(session, run_id)
