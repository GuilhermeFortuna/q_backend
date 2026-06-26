"""Feature evaluation orchestration and persistence (WO135)."""

from __future__ import annotations

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
from q_backend.features.scoring import (
    cluster_redundant,
    recommended_feature_set,
    score_features,
)
from q_backend.features.targets import TargetSpec, compute_target
from q_backend.market_data.local_store import read_ohlcv
from q_backend.storage.db.models import EvaluationRun, RunStatus
from q_backend.storage.db.repositories import (
    create_evaluation_run,
    create_feature_score_row,
    get_evaluation_run,
    increment_feature_usage,
    update_evaluation_run,
)


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
    """Build matrix, evaluate, score, and persist one evaluation run."""
    if not feature_set:
        raise ValueError("feature_set must contain at least one FeatureRequest")

    matrix_id = compute_matrix_id(symbol, timeframe, start, end, feature_set)
    started_at = datetime.now(timezone.utc)
    run = create_evaluation_run(
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
        started_at=started_at,
    )

    try:
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
        )
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

    persisted = get_evaluation_run(session, run.id)
    assert persisted is not None
    return persisted


def load_evaluation_run(
    session: Session, run_id: uuid.UUID
) -> EvaluationRun | None:
    return get_evaluation_run(session, run_id)
