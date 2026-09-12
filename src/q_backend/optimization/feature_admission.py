"""Feature admission resolver backed by persisted feature evidence (WO162)."""

from __future__ import annotations

from sqlalchemy.orm import Session

from q_backend.features.evidence import normalize_feature_name, normalize_node_kind
from q_backend.features.registry import feature_id, get_feature_spec

# NB: `list_profile_evidence` is imported lazily in `_load_cache` to break the
# import cycle with `features.evidence_service` (which imports this module).
from q_backend.optimization.hypothesis import (
    FeatureAdmissionResolver,
    InstrumentResearchProfile,
    RESEARCH_PROFILES,
)


class ProfileFeatureAdmissionResolver:
    """Query admitted feature evidence for a profile/version/fingerprint."""

    def __init__(
        self,
        session: Session,
        *,
        profile: InstrumentResearchProfile,
        split_manifest_hash: str,
        data_fingerprint: str,
        target_name: str = "fwd_return",
    ) -> None:
        self._session = session
        self._profile = profile
        self._split_manifest_hash = split_manifest_hash
        self._data_fingerprint = data_fingerprint
        self._target_name = target_name
        self._cache = self._load_cache()

    def _load_cache(self) -> dict[str, set[int]]:
        from q_backend.features.evidence_service import list_profile_evidence

        evidences = list_profile_evidence(
            self._session,
            profile_id=self._profile.profile_id,
            profile_version=self._profile.version,
            split_manifest_hash=self._split_manifest_hash,
            data_fingerprint=self._data_fingerprint,
        )
        admitted: dict[str, set[int]] = {}
        for evidence in evidences:
            if evidence.decision != "admitted":
                continue
            if evidence.key.target != self._target_name:
                continue
            admitted.setdefault(evidence.feature_name, set()).add(evidence.key.horizon)
        return admitted

    def is_feature_admitted(self, feature_id: str, symbol: str, timeframe: str) -> bool:
        del symbol, timeframe
        feature_name = normalize_feature_name(feature_id)
        horizons = self._profile.target_horizons
        admitted_horizons = self._cache.get(feature_name, set())
        return any(horizon in admitted_horizons for horizon in horizons)

    def admitted_horizons(self, feature_key: str) -> set[int]:
        feature_name = normalize_feature_name(feature_key)
        return set(self._cache.get(feature_name, set()))


def create_profile_admission_resolver(
    session: Session,
    *,
    profile_id: str,
    split_manifest_hash: str,
    data_fingerprint: str,
    target_name: str = "fwd_return",
) -> ProfileFeatureAdmissionResolver:
    profile = RESEARCH_PROFILES.get(profile_id)
    if profile is None:
        raise ValueError(f"Unknown profile_id '{profile_id}'.")
    return ProfileFeatureAdmissionResolver(
        session,
        profile=profile,
        split_manifest_hash=split_manifest_hash,
        data_fingerprint=data_fingerprint,
        target_name=target_name,
    )


def feature_key_matches_evidence(feature_key: str, evidence_feature_id: str) -> bool:
    """True when a genome/catalog feature key matches a stored evidence feature_id."""
    feature_name = normalize_feature_name(feature_key)
    spec = get_feature_spec(feature_name)
    default_id = feature_id(spec, spec.default_params)
    return evidence_feature_id == default_id or evidence_feature_id.startswith(f"{feature_name}.v")


def node_kind_matches(feature_key: str, node_kind: str | None) -> bool:
    if node_kind is None:
        return False
    return normalize_node_kind(feature_key) == node_kind
