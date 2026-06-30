"""Profile-scoped feature candidate lists for evidence evaluation (WO162)."""

from __future__ import annotations

from q_backend.features.matrix import FeatureRequest
from q_backend.features.registry import FEATURE_SPECS, get_feature_spec
from q_backend.optimization.hypothesis import HYPOTHESIS_CATALOG, RESEARCH_PROFILES


def _catalog_feature_names(profile_id: str) -> set[str]:
    names: set[str] = set()
    for hypothesis in HYPOTHESIS_CATALOG.values():
        if hypothesis.profile_id != profile_id:
            continue
        for node_kind in hypothesis.required_features:
            if node_kind.startswith("feature."):
                names.add(node_kind[len("feature.") :])
    return names


def profile_evidence_feature_requests(profile_id: str) -> list[FeatureRequest]:
    """Bounded feature list: catalog-required context features for the profile."""
    profile = RESEARCH_PROFILES.get(profile_id)
    if profile is None:
        raise ValueError(f"Unknown profile_id '{profile_id}'.")

    extra = profile.evidence_thresholds.get("candidate_features", [])
    names = sorted(_catalog_feature_names(profile_id) | set(extra))
    requests: list[FeatureRequest] = []
    for name in names:
        if name not in FEATURE_SPECS:
            continue
        requests.append(FeatureRequest(name=name, version=None, params={}))
    return requests


def node_kind_for_feature_name(feature_name: str) -> str:
    spec = get_feature_spec(feature_name)
    if spec.node_kind is not None:
        return spec.node_kind
    return f"feature.{feature_name}"
