"""Tests for redundancy clustering and global feature scoring (WO134)."""

from __future__ import annotations

import numpy as np
import pandas as pd

from q_backend.features.evaluation import FeatureEvaluation
from q_backend.features.matrix import FeatureMatrix
from q_backend.features.scoring import (
    RedundancyCluster,
    cluster_redundant,
    recommended_feature_set,
    score_features,
)


def _evaluation(
    feature_id: str,
    *,
    rank_ic: float = 0.5,
    stability: float = 0.8,
    regime_ics: dict[str, float] | None = None,
    leakage_status: str = "clean",
) -> FeatureEvaluation:
    return FeatureEvaluation(
        feature_id=feature_id,
        target="fwd_return",
        ic=rank_ic,
        rank_ic=rank_ic,
        mutual_info=0.1,
        stability=stability,
        regime_ics=regime_ics or {"low": 0.4, "mid": 0.45, "high": 0.42},
        n_obs=200,
        leakage_status=leakage_status,
    )


def test_perfectly_correlated_features_share_one_cluster():
    n = 120
    times = pd.date_range("2023-01-01", periods=n, freq="h", tz="UTC")
    base = np.linspace(0, 1, n)
    rng = np.random.default_rng(1)
    frame = pd.DataFrame(
        {
            "feat.a": base,
            "feat.b": base,
            "feat.c": rng.normal(size=n),
        },
        index=times,
    )
    matrix = FeatureMatrix(
        matrix_id="test",
        frame=frame,
        manifest={"valid_from": times[10].isoformat()},
    )

    clusters = cluster_redundant(matrix, threshold=0.9)
    cluster_map = {tuple(c.feature_ids): c for c in clusters}

    assert ("feat.a", "feat.b") in cluster_map or ["feat.a", "feat.b"] in [
        c.feature_ids for c in clusters
    ]
    pair_cluster = next(c for c in clusters if "feat.a" in c.feature_ids)
    assert pair_cluster.feature_ids == ["feat.a", "feat.b"]

    solo_cluster = next(c for c in clusters if "feat.c" in c.feature_ids)
    assert solo_cluster.feature_ids == ["feat.c"]

    evaluations = [
        _evaluation("feat.a", rank_ic=0.7),
        _evaluation("feat.b", rank_ic=0.69),
        _evaluation("feat.c", rank_ic=0.4),
    ]
    scores = score_features(evaluations, clusters)
    reps = {item.feature_id for item in scores if item.is_representative}
    assert len(reps) == 2
    assert pair_cluster.representative in {"feat.a", "feat.b"}


def test_cluster_redundant_handles_naive_index_with_tzaware_valid_from():
    # Regression: production matrices have a tz-naive UTC index (from read_ohlcv)
    # while the manifest serializes valid_from with a trailing 'Z' (tz-aware).
    # cluster_redundant must not raise "Cannot compare tz-naive and tz-aware".
    n = 60
    times = pd.date_range("2023-01-01", periods=n, freq="h")  # tz-naive
    assert times.tz is None
    base = np.linspace(0, 1, n)
    frame = pd.DataFrame({"feat.a": base, "feat.b": base}, index=times)
    matrix = FeatureMatrix(
        matrix_id="test",
        frame=frame,
        manifest={"valid_from": times[10].isoformat() + "Z"},  # tz-aware string
    )

    clusters = cluster_redundant(matrix, threshold=0.9)

    # Rows before valid_from are trimmed; perfectly correlated pair clusters.
    assert any(c.feature_ids == ["feat.a", "feat.b"] for c in clusters)


def test_recommended_set_never_contains_two_from_same_cluster():
    clusters = [
        RedundancyCluster(0, ["a", "b"], "a"),
        RedundancyCluster(1, ["c"], "c"),
        RedundancyCluster(2, ["d", "e", "f"], "d"),
    ]
    evaluations = [
        _evaluation("a", rank_ic=0.9),
        _evaluation("b", rank_ic=0.85),
        _evaluation("c", rank_ic=0.8),
        _evaluation("d", rank_ic=0.75),
        _evaluation("e", rank_ic=0.7),
        _evaluation("f", rank_ic=0.65),
    ]
    scores = score_features(evaluations, clusters)
    recommended = recommended_feature_set(scores, top_k=10)

    cluster_by_feature = {}
    for cluster in clusters:
        for fid in cluster.feature_ids:
            cluster_by_feature[fid] = cluster.cluster_id

    seen_clusters: set[int] = set()
    for fid in recommended:
        cluster_id = cluster_by_feature[fid]
        assert cluster_id not in seen_clusters
        seen_clusters.add(cluster_id)


def test_suspect_feature_ranks_below_equivalent_clean_feature():
    clusters = [RedundancyCluster(0, ["clean", "suspect"], "clean")]
    evaluations = [
        _evaluation("clean", rank_ic=0.6, stability=0.7),
        _evaluation("suspect", rank_ic=0.6, stability=0.7, leakage_status="suspect"),
    ]
    scores = score_features(evaluations, clusters)
    by_id = {item.feature_id: item for item in scores}
    assert by_id["suspect"].global_score < by_id["clean"].global_score


def test_scoring_is_deterministic():
    times = pd.date_range("2023-01-01", periods=80, freq="h", tz="UTC")
    rng = np.random.default_rng(0)
    values = rng.normal(size=80)
    frame = pd.DataFrame(
        {
            "f1": values,
            "f2": values * 0.98 + 0.01,
            "f3": rng.normal(size=80),
        },
        index=times,
    )
    matrix = FeatureMatrix(
        matrix_id="det",
        frame=frame,
        manifest={"valid_from": times[5].isoformat()},
    )
    evaluations = [
        _evaluation("f1", rank_ic=0.5),
        _evaluation("f2", rank_ic=0.48),
        _evaluation("f3", rank_ic=0.3),
    ]

    first_clusters = cluster_redundant(matrix)
    first_scores = score_features(evaluations, first_clusters)
    first_recommended = recommended_feature_set(first_scores, top_k=2)

    second_clusters = cluster_redundant(matrix)
    second_scores = score_features(evaluations, second_clusters)
    second_recommended = recommended_feature_set(second_scores, top_k=2)

    assert first_clusters == second_clusters
    assert first_scores == second_scores
    assert first_recommended == second_recommended
