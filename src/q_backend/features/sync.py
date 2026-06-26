"""One-way, idempotent sync from the in-code FeatureSpec registry to the DB (WO130)."""

from __future__ import annotations

from sqlalchemy.orm import Session

from q_backend.features.registry import list_feature_specs
from q_backend.storage.db.models import FeatureStatus
from q_backend.storage.db.repositories import (
    upsert_feature_definition,
    upsert_feature_version,
)

_REGISTRY_PROVENANCE = {"source_wo": "WO127", "author": "registry"}


def sync_registry_to_db(session: Session) -> None:
    """Seed or refresh feature definitions and versions from the code catalog.

    Idempotent: repeated calls update recipe metadata but never downgrade a
    human-promoted version status.
    """
    for spec in list_feature_specs():
        if spec.source == "neural":
            continue
        definition = upsert_feature_definition(
            session,
            name=spec.name,
            category=spec.category,
            description=spec.description,
        )
        upsert_feature_version(
            session,
            definition_id=definition.id,
            version=spec.version,
            status=FeatureStatus.EXPERIMENTAL.value,
            node_kind=spec.node_kind,
            param_keys=sorted(spec.param_keys),
            default_params=dict(spec.default_params),
            forward_window=spec.forward_window,
            leakage_status=spec.leakage_status,
            provenance=dict(_REGISTRY_PROVENANCE),
        )
    session.flush()
