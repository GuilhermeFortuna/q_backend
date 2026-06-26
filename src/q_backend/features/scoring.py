"""Redundancy clustering and global feature scoring (WO134).

Weights for ``global_score`` (sum to 1.0):
- ``W_RANK_IC = 0.4`` — ``|rank_ic|``
- ``W_STABILITY = 0.3`` — time stability from WO133
- ``W_REGIME_CONSISTENCY = 0.15`` — ``1 - normalized spread(regime_ics)``
- ``W_UNIQUENESS = 0.15`` — ``1 / cluster_size``

Non-``clean`` leakage status applies ``LEAKAGE_PENALTY = 0.5``.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

import numpy as np
import pandas as pd

from q_backend.features.evaluation import FeatureEvaluation
from q_backend.features.matrix import FeatureMatrix

W_RANK_IC = 0.4
W_STABILITY = 0.3
W_REGIME_CONSISTENCY = 0.15
W_UNIQUENESS = 0.15
LEAKAGE_PENALTY = 0.5


@dataclass(frozen=True)
class RedundancyCluster:
    cluster_id: int
    feature_ids: list[str]
    representative: str


@dataclass(frozen=True)
class FeatureScore:
    feature_id: str
    global_score: float
    cluster_id: int
    is_representative: bool


def _trim_to_valid_from(frame: pd.DataFrame, manifest: dict[str, Any]) -> pd.DataFrame:
    valid_from = manifest.get("valid_from")
    if valid_from is None or frame.empty:
        return frame
    cutoff = pd.Timestamp(valid_from)
    index = pd.to_datetime(frame.index)
    return frame.loc[index >= cutoff]


def _union_find_components(
    feature_ids: list[str], corr: pd.DataFrame, threshold: float
) -> list[list[str]]:
    parent = {fid: fid for fid in feature_ids}

    def find(node: str) -> str:
        while parent[node] != node:
            parent[node] = parent[parent[node]]
            node = parent[node]
        return node

    def union(left: str, right: str) -> None:
        root_left = find(left)
        root_right = find(right)
        if root_left == root_right:
            return
        if root_left < root_right:
            parent[root_right] = root_left
        else:
            parent[root_left] = root_right

    for i, left_id in enumerate(feature_ids):
        for right_id in feature_ids[i + 1 :]:
            value = corr.loc[left_id, right_id]
            if not np.isnan(value) and value >= threshold:
                union(left_id, right_id)

    groups: dict[str, list[str]] = {}
    for fid in feature_ids:
        root = find(fid)
        groups.setdefault(root, []).append(fid)

    clusters = [sorted(members) for members in groups.values()]
    clusters.sort(key=lambda members: members[0])
    return clusters


def cluster_redundant(
    matrix: FeatureMatrix, *, threshold: float = 0.9
) -> list[RedundancyCluster]:
    frame = _trim_to_valid_from(matrix.frame, matrix.manifest)
    feature_ids = sorted(frame.columns)
    if not feature_ids:
        return []

    trimmed = frame[feature_ids].dropna(how="all")
    if trimmed.empty or len(trimmed) < 2:
        return [
            RedundancyCluster(
                cluster_id=index,
                feature_ids=[fid],
                representative=fid,
            )
            for index, fid in enumerate(feature_ids)
        ]

    corr = trimmed.corr(method="spearman").abs()
    member_groups = _union_find_components(feature_ids, corr, threshold)

    clusters: list[RedundancyCluster] = []
    for cluster_id, members in enumerate(member_groups):
        clusters.append(
            RedundancyCluster(
                cluster_id=cluster_id,
                feature_ids=members,
                representative=members[0],
            )
        )
    return clusters


def _regime_consistency(regime_ics: dict[str, float]) -> float:
    values = [value for value in regime_ics.values() if not math.isnan(value)]
    if len(values) < 2:
        return 0.0
    spread = max(values) - min(values)
    normalized = min(spread / 2.0, 1.0)
    return 1.0 - normalized


def _cluster_maps(
    clusters: list[RedundancyCluster],
) -> tuple[dict[str, int], dict[int, int]]:
    cluster_id_by_feature: dict[str, int] = {}
    cluster_size_by_id: dict[int, int] = {}
    for cluster in clusters:
        cluster_size_by_id[cluster.cluster_id] = len(cluster.feature_ids)
        for fid in cluster.feature_ids:
            cluster_id_by_feature[fid] = cluster.cluster_id
    return cluster_id_by_feature, cluster_size_by_id


def _compute_global_score(
    evaluation: FeatureEvaluation,
    *,
    cluster_size: int,
) -> float:
    rank_term = 0.0 if math.isnan(evaluation.rank_ic) else abs(evaluation.rank_ic)
    stability_term = 0.0 if math.isnan(evaluation.stability) else evaluation.stability
    regime_term = _regime_consistency(evaluation.regime_ics)
    uniqueness_term = 1.0 / max(cluster_size, 1)

    score = (
        W_RANK_IC * rank_term
        + W_STABILITY * stability_term
        + W_REGIME_CONSISTENCY * regime_term
        + W_UNIQUENESS * uniqueness_term
    )
    if evaluation.leakage_status != "clean":
        score *= LEAKAGE_PENALTY
    return float(np.clip(score, 0.0, 1.0))


def _representative_by_cluster(scores: list[FeatureScore]) -> dict[int, str]:
    by_cluster: dict[int, list[FeatureScore]] = {}
    for item in scores:
        by_cluster.setdefault(item.cluster_id, []).append(item)

    representatives: dict[int, str] = {}
    for cluster_id, members in by_cluster.items():
        winner = sorted(
            members,
            key=lambda item: (-item.global_score, item.feature_id),
        )[0]
        representatives[cluster_id] = winner.feature_id
    return representatives


def score_features(
    evaluations: list[FeatureEvaluation],
    clusters: list[RedundancyCluster],
) -> list[FeatureScore]:
    cluster_id_by_feature, cluster_size_by_id = _cluster_maps(clusters)

    provisional: list[FeatureScore] = []
    for evaluation in sorted(evaluations, key=lambda item: item.feature_id):
        cluster_id = cluster_id_by_feature.get(evaluation.feature_id, 0)
        cluster_size = cluster_size_by_id.get(cluster_id, 1)
        global_score = _compute_global_score(
            evaluation, cluster_size=cluster_size
        )
        provisional.append(
            FeatureScore(
                feature_id=evaluation.feature_id,
                global_score=global_score,
                cluster_id=cluster_id,
                is_representative=False,
            )
        )

    representatives = _representative_by_cluster(provisional)
    scored: list[FeatureScore] = []
    for item in provisional:
        scored.append(
            FeatureScore(
                feature_id=item.feature_id,
                global_score=item.global_score,
                cluster_id=item.cluster_id,
                is_representative=item.feature_id
                == representatives[item.cluster_id],
            )
        )
    return scored


def recommended_feature_set(
    scores: list[FeatureScore], *, top_k: int = 20
) -> list[str]:
    representatives = [
        item
        for item in scores
        if item.is_representative
    ]
    representatives.sort(key=lambda item: (-item.global_score, item.feature_id))
    return [item.feature_id for item in representatives[:top_k]]
