"""Validated neural model status transitions (WO146).

``set_neural_model_status`` remains the low-level setter; operator-facing paths
(REST, CLI) must call ``promote_neural_model`` so illegal transitions raise.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from q_backend.storage.db.models import NeuralModel, NeuralModelStatus, NeuralModelVersion
from q_backend.storage.db.repositories import get_neural_model_version, set_neural_model_status

ALLOWED_TRANSITIONS: dict[str, frozenset[str]] = {
    NeuralModelStatus.TRAINED.value: frozenset({NeuralModelStatus.CANDIDATE.value, NeuralModelStatus.ARCHIVED.value}),
    NeuralModelStatus.CANDIDATE.value: frozenset(
        {
            NeuralModelStatus.PRODUCTION.value,
            NeuralModelStatus.ARCHIVED.value,
            NeuralModelStatus.TRAINED.value,
        }
    ),
    NeuralModelStatus.PRODUCTION.value: frozenset(
        {NeuralModelStatus.ARCHIVED.value, NeuralModelStatus.CANDIDATE.value}
    ),
    NeuralModelStatus.ARCHIVED.value: frozenset(),
}


class NeuralModelNotFoundError(ValueError):
    """Raised when ``model_hash`` does not match a persisted version."""


class IllegalNeuralStatusTransition(ValueError):
    """Raised when ``target_status`` is not allowed from the current status."""


def _demote_other_production_versions(
    session: Session,
    *,
    symbol: str,
    timeframe: str,
    exclude_hash: str,
) -> None:
    """Demote other PRODUCTION versions for the same instrument to CANDIDATE."""
    others = session.execute(
        select(NeuralModelVersion)
        .join(NeuralModel, NeuralModelVersion.model_id == NeuralModel.id)
        .where(
            NeuralModel.symbol == symbol,
            NeuralModel.timeframe == timeframe,
            NeuralModelVersion.status == NeuralModelStatus.PRODUCTION.value,
            NeuralModelVersion.model_hash != exclude_hash,
        )
    ).scalars()
    for other in others:
        set_neural_model_status(
            session,
            model_hash=other.model_hash,
            status=NeuralModelStatus.CANDIDATE.value,
        )


def promote_neural_model(
    session: Session,
    *,
    model_hash: str,
    target_status: str,
) -> NeuralModelVersion:
    """Transition a neural model version after validating the status graph."""
    version = get_neural_model_version(session, model_hash)
    if version is None:
        raise NeuralModelNotFoundError(f"NeuralModelVersion with hash '{model_hash}' not found")

    allowed = ALLOWED_TRANSITIONS.get(version.status, frozenset())
    if target_status not in allowed:
        raise IllegalNeuralStatusTransition(f"Illegal status transition from '{version.status}' to '{target_status}'")

    if target_status == NeuralModelStatus.PRODUCTION.value:
        if version.model is None:
            raise ValueError(f"Neural model version '{model_hash}' has no parent model.")
        _demote_other_production_versions(
            session,
            symbol=version.model.symbol,
            timeframe=version.model.timeframe,
            exclude_hash=model_hash,
        )

    return set_neural_model_status(
        session,
        model_hash=model_hash,
        status=target_status,
    )
