"""Feature Intelligence — named, versioned bar→series recipes (WO127+)."""

from q_backend.features.compute import FeatureSeries, compute_feature
from q_backend.features.leakage import (
    FORWARD_LOOKING_KINDS,
    LeakageError,
    assert_causal,
)
from q_backend.features.matrix import (
    ENGINE_VERSION,
    FeatureMatrix,
    FeatureRequest,
    build_feature_matrix,
    compute_matrix_id,
)
from q_backend.features.registry import (
    FeatureSpec,
    assert_catalog_consistent,
    feature_id,
    get_feature_spec,
    list_categories,
    list_feature_specs,
    resolve_params,
)
from q_backend.features.targets import (
    TargetSpec,
    align_feature_target,
    compute_target,
    embargo_bars,
    list_target_specs,
    purge_embargo,
)
from q_backend.features.evaluation import (
    FeatureEvaluation,
    evaluate_feature,
    evaluate_matrix,
)
from q_backend.features.scoring import (
    FeatureScore,
    RedundancyCluster,
    cluster_redundant,
    recommended_feature_set,
    score_features,
)

__all__ = [
    "ENGINE_VERSION",
    "FeatureEvaluation",
    "FeatureMatrix",
    "FeatureRequest",
    "FeatureScore",
    "FeatureSeries",
    "FeatureSpec",
    "FORWARD_LOOKING_KINDS",
    "LeakageError",
    "RedundancyCluster",
    "TargetSpec",
    "align_feature_target",
    "assert_causal",
    "assert_catalog_consistent",
    "build_feature_matrix",
    "cluster_redundant",
    "compute_feature",
    "compute_matrix_id",
    "compute_target",
    "embargo_bars",
    "evaluate_feature",
    "evaluate_matrix",
    "feature_id",
    "get_feature_spec",
    "list_categories",
    "list_feature_specs",
    "list_target_specs",
    "purge_embargo",
    "recommended_feature_set",
    "resolve_params",
    "score_features",
]
