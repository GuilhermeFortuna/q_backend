import uuid
from typing import Optional

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from q_backend.api.deps import get_session
from q_backend.api.schemas.features import (
    FeatureEvalCreateRequest,
    FeatureEvalRunResponse,
    FeatureEvalStartResponse,
    FeatureEvalClusterItem,
    FeatureEvalHeatmap,
    FeatureEvalHeatmapRow,
    FeatureEvalLeaderboardItem,
    FeatureLeaderboardItem,
    FeatureLeaderboardResponse,
    FeatureListItem,
    FeatureListResponse,
    FeaturePassportResponse,
    FeatureStatusUpdateRequest,
    FeatureVersionDetail,
)
from q_backend.features.evaluation_service import (
    create_pending_evaluation_run,
    execute_evaluation_run,
)
from q_backend.features.matrix import FeatureRequest
from q_backend.features.targets import list_target_specs
from q_backend.storage.db.engine import session_scope
from q_backend.storage.db.models import FeatureDefinition, FeatureStatus, FeatureVersion
from q_backend.storage.db.repositories import (
    get_evaluation_run,
    get_feature_definition,
    get_feature_evaluation_history,
    get_latest_global_scores,
    list_feature_definitions,
    set_feature_status,
)

router = APIRouter(tags=["features"])


def _latest_version(versions: list[FeatureVersion]) -> Optional[FeatureVersion]:
    if not versions:
        return None
    return max(versions, key=lambda version: version.version)


def _version_detail(version: FeatureVersion) -> FeatureVersionDetail:
    return FeatureVersionDetail(
        version=version.version,
        status=version.status,
        node_kind=version.node_kind,
        param_keys=list(version.param_keys),
        default_params=dict(version.default_params),
        forward_window=version.forward_window,
        leakage_status=version.leakage_status,
        provenance=dict(version.provenance),
    )


def _passport_from_definition(
    definition: FeatureDefinition, session: Session
) -> FeaturePassportResponse:
    versions = sorted(definition.versions, key=lambda version: version.version, reverse=True)
    latest_scores = get_latest_global_scores(session)
    return FeaturePassportResponse(
        name=definition.name,
        category=definition.category,
        description=definition.description,
        usage_count=definition.usage_count,
        versions=[_version_detail(version) for version in versions],
        score=latest_scores.get(definition.name),
        evaluation_history=get_feature_evaluation_history(session, definition.name),
    )


def _list_item_from_definition(
    definition: FeatureDefinition, *, score: Optional[float] = None
) -> FeatureListItem:
    latest = _latest_version(definition.versions)
    return FeatureListItem(
        name=definition.name,
        category=definition.category,
        latest_version=latest.version if latest is not None else 0,
        status=(
            latest.status
            if latest is not None
            else FeatureStatus.EXPERIMENTAL.value
        ),
        usage_count=definition.usage_count,
        score=score,
    )


def _resolve_target_spec(name: str, horizon: int):
    for spec in list_target_specs([horizon]):
        if spec.name == name:
            return spec
    raise ValueError(f"Unknown target {name!r} for horizon {horizon}")


def _leaderboard_items_from_run(run) -> list[FeatureEvalLeaderboardItem]:
    rows = sorted(
        run.scores,
        key=lambda row: (
            -(row.global_score or 0.0),
            row.feature_id,
        ),
    )
    return [
        FeatureEvalLeaderboardItem(
            feature_id=row.feature_id,
            feature_name=row.feature_name,
            ic=row.ic,
            rank_ic=row.rank_ic,
            mutual_info=row.mutual_info,
            stability=row.stability,
            global_score=row.global_score,
            cluster_id=row.cluster_id,
            is_representative=row.is_representative,
            leakage_status=row.leakage_status,
            regime_ics=dict(row.regime_ics),
        )
        for row in rows
    ]


def _clusters_from_run(run) -> list[FeatureEvalClusterItem]:
    by_cluster: dict[int, list] = {}
    for row in run.scores:
        by_cluster.setdefault(row.cluster_id, []).append(row)

    clusters: list[FeatureEvalClusterItem] = []
    for cluster_id in sorted(by_cluster):
        members = sorted(by_cluster[cluster_id], key=lambda row: row.feature_id)
        representative = next(
            row.feature_id for row in members if row.is_representative
        )
        clusters.append(
            FeatureEvalClusterItem(
                cluster_id=cluster_id,
                feature_ids=[row.feature_id for row in members],
                representative=representative,
            )
        )
    return clusters


def _heatmap_from_run(run) -> FeatureEvalHeatmap:
    rows = sorted(
        run.scores,
        key=lambda row: (-(row.global_score or 0.0), row.feature_id),
    )
    return FeatureEvalHeatmap(
        metrics=["ic", "rank_ic", "mutual_info", "stability"],
        rows=[
            FeatureEvalHeatmapRow(
                feature_id=row.feature_id,
                feature_name=row.feature_name,
                ic=row.ic,
                rank_ic=row.rank_ic,
                mutual_info=row.mutual_info,
                stability=row.stability,
            )
            for row in rows
        ],
    )


def _run_response(run) -> FeatureEvalRunResponse:
    return FeatureEvalRunResponse(
        run_id=str(run.id),
        status=run.status,
        symbol=run.symbol,
        timeframe=run.timeframe,
        start=run.start,
        end=run.end,
        target_name=run.target_name,
        target_horizon=run.target_horizon,
        matrix_id=run.matrix_id,
        feature_count=run.feature_count,
        result_summary=dict(run.result_summary) if run.result_summary else None,
        error_message=run.error_message,
        started_at=run.started_at,
        finished_at=run.finished_at,
        leaderboard=_leaderboard_items_from_run(run),
        clusters=_clusters_from_run(run),
        heatmap=_heatmap_from_run(run),
    )


@router.get("/api/v1/features", response_model=FeatureListResponse)
def list_features(
    session: Session = Depends(get_session),
    category: Optional[str] = Query(None),
    status: Optional[str] = Query(None),
):
    """Return Feature Store rows for the catalog table."""
    definitions = list_feature_definitions(
        session, category=category, status=status
    )
    latest_scores = get_latest_global_scores(session)
    return {
        "features": [
            _list_item_from_definition(
                definition, score=latest_scores.get(definition.name)
            )
            for definition in definitions
        ],
    }


@router.get(
    "/api/v1/features/leaderboard",
    response_model=FeatureLeaderboardResponse,
)
def get_features_leaderboard(session: Session = Depends(get_session)):
    """Return the latest global_score per feature across evaluation runs."""
    latest_scores = get_latest_global_scores(session)
    items = [
        FeatureLeaderboardItem(feature_name=name, global_score=score)
        for name, score in sorted(
            latest_scores.items(),
            key=lambda item: (-item[1], item[0]),
        )
    ]
    return {"features": items}


@router.get("/api/v1/features/{name}", response_model=FeaturePassportResponse)
def get_feature_passport(name: str, session: Session = Depends(get_session)):
    """Return the Feature Passport for a single feature."""
    definition = get_feature_definition(session, name)
    if definition is None:
        raise HTTPException(
            status_code=404, detail=f"Feature '{name}' not found."
        )
    return _passport_from_definition(definition, session)


@router.post(
    "/api/v1/features/{name}/{version}/status",
    response_model=FeaturePassportResponse,
)
def update_feature_status(
    name: str,
    version: int,
    body: FeatureStatusUpdateRequest,
    session: Session = Depends(get_session),
):
    """Promote or demote a feature version's lifecycle status."""
    try:
        set_feature_status(
            session,
            name=name,
            version=version,
            status=body.status.value,
        )
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    definition = get_feature_definition(session, name)
    if definition is None:
        raise HTTPException(
            status_code=404, detail=f"Feature '{name}' not found."
        )
    return _passport_from_definition(definition, session)


@router.post(
    "/api/v1/feature-eval",
    response_model=FeatureEvalStartResponse,
)
def start_feature_evaluation(
    body: FeatureEvalCreateRequest,
    background_tasks: BackgroundTasks,
):
    """Kick off a feature evaluation and return its run id immediately.

    The evaluation can take tens of seconds for large feature sets / windows, so
    it runs in the background: the run row is created as RUNNING (in its own
    committed transaction, so the background task reliably sees it) and the
    client polls ``GET /api/v1/feature-eval/{run_id}`` for progress and results.
    """
    try:
        target = _resolve_target_spec(body.target.name, body.target.horizon)
        feature_set = [
            FeatureRequest(item.name, item.version, dict(item.params))
            for item in body.features
        ]
        with session_scope() as session:
            run = create_pending_evaluation_run(
                session,
                symbol=body.symbol,
                timeframe=body.timeframe,
                start=body.start,
                end=body.end,
                target=target,
                feature_set=feature_set,
            )
            run_id = run.id
            status = run.status
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    background_tasks.add_task(
        execute_evaluation_run,
        run_id,
        symbol=body.symbol,
        timeframe=body.timeframe,
        start=body.start,
        end=body.end,
        target=target,
        feature_set=feature_set,
    )

    return {"run_id": str(run_id), "status": status}


@router.get(
    "/api/v1/feature-eval/{run_id}",
    response_model=FeatureEvalRunResponse,
)
def get_feature_evaluation(run_id: str, session: Session = Depends(get_session)):
    """Return evaluation run status, leaderboard, clusters, and heatmap."""
    try:
        parsed_id = uuid.UUID(run_id)
    except ValueError as exc:
        raise HTTPException(
            status_code=404, detail=f"Feature evaluation run '{run_id}' not found."
        ) from exc

    run = get_evaluation_run(session, parsed_id)
    if run is None:
        raise HTTPException(
            status_code=404, detail=f"Feature evaluation run '{run_id}' not found."
        )
    return _run_response(run)
