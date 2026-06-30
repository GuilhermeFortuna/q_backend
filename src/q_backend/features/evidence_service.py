"""Orchestration and persistence for profile-scoped feature evidence (WO162)."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

import pandas as pd
from sqlalchemy.orm import Session

from q_backend.features.compute import compute_feature
from q_backend.features.evaluation import evaluate_matrix
from q_backend.features.evidence import (
    FeatureEvidence,
    FeatureEvidenceKey,
    apply_redundancy_decisions,
    evaluate_feature_evidence,
    merge_evidence_thresholds,
    profile_target_specs,
)
from q_backend.features.matrix import FeatureMatrix, FeatureRequest, compute_matrix_id
from q_backend.features.profile_features import node_kind_for_feature_name, profile_evidence_feature_requests
from q_backend.features.registry import feature_id, get_feature_spec
from q_backend.features.scoring import cluster_redundant, score_features
from q_backend.features.split_manifest import SplitManifest, build_split_manifest, slice_segment
from q_backend.features.targets import TargetSpec, compute_target
from q_backend.market_data.local_store import read_ohlcv
from q_backend.optimization.hypothesis import RESEARCH_PROFILES
from q_backend.storage.db.models import FeatureEvidenceRow, RunStatus
from q_backend.storage.db.repositories import (
    create_feature_evidence_row,
    get_feature_evidence,
    list_feature_evidence_for_profile,
)
from q_backend.storage.lake.artifacts import read_feature_evidence, write_feature_evidence


def _ohlcv_to_bars(records: list[Any]) -> pd.DataFrame:
    rows = [record.model_dump() if hasattr(record, "model_dump") else dict(record) for record in records]
    frame = pd.DataFrame(rows).sort_values("time").reset_index(drop=True)
    # OHLCV records carry `tick_volume`/`real_volume`, but the feature-compute path
    # (features.compute / features.matrix) requires a canonical `volume` column.
    # Mirror features.matrix._ohlcv_to_compute_bars so Feature Store and evidence agree.
    if "volume" not in frame.columns and "tick_volume" in frame.columns:
        frame["volume"] = frame["tick_volume"].astype(float)
    return frame


def _matrix_from_segment(
    *,
    symbol: str,
    timeframe: str,
    segment_bars: pd.DataFrame,
    feature_requests: list[FeatureRequest],
) -> FeatureMatrix:
    if segment_bars.empty:
        raise ValueError("Cannot build feature matrix from empty evidence segment.")
    start = pd.Timestamp(segment_bars["time"].iloc[0]).to_pydatetime()
    end = pd.Timestamp(segment_bars["time"].iloc[-1]).to_pydatetime()
    matrix_id = compute_matrix_id(symbol, timeframe, start, end, feature_requests)
    columns: dict[str, pd.Series] = {}
    manifest_features: list[dict[str, Any]] = []
    for request in feature_requests:
        spec = get_feature_spec(request.name, version=request.version)
        resolved = {**spec.default_params, **request.params}
        computed = compute_feature(segment_bars, spec, resolved)
        fid = feature_id(spec, resolved)
        columns[fid] = computed.series
        manifest_features.append(
            {
                "feature_id": fid,
                "name": spec.name,
                "version": spec.version,
                "params": resolved,
                "leakage_status": computed.leakage_status,
            }
        )
    frame = pd.DataFrame(columns)
    frame.index = pd.to_datetime(segment_bars["time"], utc=True)
    manifest = {
        "symbol": symbol,
        "timeframe": timeframe.upper(),
        "start": start.isoformat(),
        "end": end.isoformat(),
        "bar_count": len(segment_bars),
        "features": manifest_features,
        "segment": "evidence",
    }
    return FeatureMatrix(matrix_id=matrix_id, frame=frame, manifest=manifest)


def run_profile_feature_evidence(
    session: Session,
    *,
    profile_id: str,
    start: datetime,
    end: datetime,
    target_name: str = "fwd_return",
    feature_requests: list[FeatureRequest] | None = None,
) -> list[FeatureEvidence]:
    """Evaluate and persist feature evidence for one profile on the evidence segment."""
    profile = RESEARCH_PROFILES.get(profile_id)
    if profile is None:
        raise ValueError(f"Unknown profile_id '{profile_id}'.")

    bars = _ohlcv_to_bars(read_ohlcv(profile.symbol, profile.timeframe, start, end))
    manifest = build_split_manifest(
        bars,
        symbol=profile.symbol,
        timeframe=profile.timeframe,
    )
    evidence_bars = slice_segment(bars, manifest.evidence)
    thresholds = merge_evidence_thresholds(profile.evidence_thresholds)
    requests = feature_requests or profile_evidence_feature_requests(profile_id)
    if not requests:
        return []

    matrix = _matrix_from_segment(
        symbol=profile.symbol,
        timeframe=profile.timeframe,
        segment_bars=evidence_bars,
        feature_requests=requests,
    )
    target_specs = [
        spec
        for spec in profile_target_specs(profile.target_horizons)
        if spec.name == target_name
    ]
    if not target_specs:
        raise ValueError(f"Unsupported target '{target_name}' for profile '{profile_id}'.")

    attempted_count = len(requests) * len(target_specs)
    all_evidences: list[FeatureEvidence] = []

    for target_spec in target_specs:
        target = compute_target(evidence_bars, target_spec)
        target.index = pd.to_datetime(evidence_bars["time"], utc=True)
        close = pd.Series(
            evidence_bars["close"].to_numpy(),
            index=target.index,
            name="close",
        )
        evaluations = evaluate_matrix(
            matrix,
            target,
            close=close,
            bars=evidence_bars,
        )
        clusters = cluster_redundant(
            matrix,
            threshold=float(thresholds["redundancy_correlation_threshold"]),
        )
        scores = score_features(evaluations, clusters)
        representative_by_cluster = {
            cluster.cluster_id: cluster.representative for cluster in clusters
        }

        batch: list[FeatureEvidence] = []
        for evaluation in evaluations:
            spec_entry = next(
                entry
                for entry in matrix.manifest["features"]
                if entry["feature_id"] == evaluation.feature_id
            )
            feature_name = spec_entry["name"]
            spec = get_feature_spec(feature_name)
            key = FeatureEvidenceKey(
                profile_id=profile.profile_id,
                profile_version=profile.version,
                feature_id=evaluation.feature_id,
                feature_version=spec.version,
                target=target_spec.name,
                horizon=target_spec.horizon,
                split_manifest_hash=manifest.manifest_hash,
            )
            cluster_id = next(
                (
                    cluster.cluster_id
                    for cluster in clusters
                    if evaluation.feature_id in cluster.feature_ids
                ),
                0,
            )
            batch.append(
                evaluate_feature_evidence(
                    key=key,
                    feature_name=feature_name,
                    feature=matrix.frame[evaluation.feature_id],
                    target=target,
                    close=close,
                    manifest=manifest,
                    evidence_bars=evidence_bars,
                    thresholds=thresholds,
                    attempted_feature_count=attempted_count,
                    leakage_status=evaluation.leakage_status,
                    is_representative=evaluation.feature_id
                    == representative_by_cluster.get(cluster_id, evaluation.feature_id),
                    cluster_id=cluster_id,
                    node_kind=node_kind_for_feature_name(feature_name),
                )
            )

        all_evidences.extend(apply_redundancy_decisions(batch, clusters, scores))

    for evidence in all_evidences:
        persist_feature_evidence(session, evidence)

    return all_evidences


def persist_feature_evidence(session: Session, evidence: FeatureEvidence) -> FeatureEvidenceRow:
    artifact_id = evidence.key.composite_key()
    write_feature_evidence(artifact_id, evidence.to_diagnostics_dict())
    row = create_feature_evidence_row(
        session,
        profile_id=evidence.key.profile_id,
        profile_version=evidence.key.profile_version,
        feature_id=evidence.key.feature_id,
        feature_version=evidence.key.feature_version,
        feature_name=evidence.feature_name,
        node_kind=evidence.node_kind,
        target=evidence.key.target,
        horizon=evidence.key.horizon,
        split_manifest_hash=evidence.key.split_manifest_hash,
        data_fingerprint=evidence.data_fingerprint,
        ic=evidence.ic,
        rank_ic=evidence.rank_ic,
        mutual_info=evidence.mutual_info,
        sign_consistency=evidence.sign_consistency,
        median_effect=evidence.median_effect,
        effect_dispersion=evidence.effect_dispersion,
        n_obs=evidence.n_obs,
        permutation_null_floor=evidence.permutation_null_floor,
        deflated_score=evidence.deflated_score,
        decision=evidence.decision,
        rejection_reasons=evidence.rejection_reasons,
        leakage_status=evidence.leakage_status,
        is_representative=evidence.is_representative,
        cluster_id=evidence.cluster_id,
        attempted_feature_count=evidence.attempted_feature_count,
        diagnostics_artifact_id=artifact_id,
        status=RunStatus.COMPLETED.value,
    )
    evidence.diagnostics_artifact_id = artifact_id
    return row


def load_feature_evidence(
    session: Session,
    key: FeatureEvidenceKey,
) -> FeatureEvidence | None:
    row = get_feature_evidence(
        session,
        profile_id=key.profile_id,
        profile_version=key.profile_version,
        feature_id=key.feature_id,
        feature_version=key.feature_version,
        target=key.target,
        horizon=key.horizon,
        split_manifest_hash=key.split_manifest_hash,
    )
    if row is None:
        return None
    diagnostics = read_feature_evidence(row.diagnostics_artifact_id)
    return _row_to_evidence(row, diagnostics)


def _row_to_evidence(row: FeatureEvidenceRow, diagnostics: dict[str, Any]) -> FeatureEvidence:
    from q_backend.features.evidence import FoldDiagnostic

    key = FeatureEvidenceKey(
        profile_id=row.profile_id,
        profile_version=row.profile_version,
        feature_id=row.feature_id,
        feature_version=row.feature_version,
        target=row.target,
        horizon=row.horizon,
        split_manifest_hash=row.split_manifest_hash,
    )
    fold_diagnostics = [
        FoldDiagnostic(
            fold_index=int(item["fold_index"]),
            ic=float(item["ic"]),
            rank_ic=float(item["rank_ic"]),
            n_obs=int(item["n_obs"]),
        )
        for item in diagnostics.get("fold_diagnostics", [])
    ]
    return FeatureEvidence(
        key=key,
        feature_name=row.feature_name,
        node_kind=row.node_kind,
        ic=row.ic if row.ic is not None else float("nan"),
        rank_ic=row.rank_ic if row.rank_ic is not None else float("nan"),
        mutual_info=row.mutual_info if row.mutual_info is not None else float("nan"),
        sign_consistency=row.sign_consistency if row.sign_consistency is not None else 0.0,
        median_effect=row.median_effect if row.median_effect is not None else float("nan"),
        effect_dispersion=row.effect_dispersion if row.effect_dispersion is not None else float("nan"),
        n_obs=row.n_obs,
        fold_diagnostics=fold_diagnostics,
        regime_ics=dict(diagnostics.get("regime_ics", {})),
        permutation_null_floor=row.permutation_null_floor
        if row.permutation_null_floor is not None
        else float("nan"),
        deflated_score=row.deflated_score if row.deflated_score is not None else 0.0,
        decision=row.decision,  # type: ignore[arg-type]
        rejection_reasons=list(row.rejection_reasons or []),
        leakage_status=row.leakage_status,
        is_representative=row.is_representative,
        cluster_id=row.cluster_id,
        data_fingerprint=row.data_fingerprint,
        attempted_feature_count=row.attempted_feature_count,
        diagnostics_artifact_id=row.diagnostics_artifact_id,
    )


def list_profile_evidence(
    session: Session,
    *,
    profile_id: str,
    profile_version: int,
    split_manifest_hash: str,
    data_fingerprint: str,
) -> list[FeatureEvidence]:
    rows = list_feature_evidence_for_profile(
        session,
        profile_id=profile_id,
        profile_version=profile_version,
        split_manifest_hash=split_manifest_hash,
        data_fingerprint=data_fingerprint,
    )
    evidences: list[FeatureEvidence] = []
    for row in rows:
        diagnostics = read_feature_evidence(row.diagnostics_artifact_id)
        evidences.append(_row_to_evidence(row, diagnostics))
    return evidences
