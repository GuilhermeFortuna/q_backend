"""One-way, idempotent sync from neural model lake artifacts to the DB (WO142)."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy.orm import Session

from q_backend.storage.db.models import NeuralModelStatus
from q_backend.storage.db.repositories import (
    create_neural_model,
    create_neural_model_version,
    get_neural_model_version,
)
from q_backend.storage.lake.artifacts import (
    list_neural_model_hashes,
    read_neural_model_manifest,
)


def _parse_manifest_datetime(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def sync_neural_models_to_db(session: Session) -> None:
    """Reconcile lake neural-model artifacts into the model registry.

    Idempotent and one-way: refreshes metadata from immutable lake artifacts but
    never downgrades a human-promoted version status.
    """
    for model_hash in list_neural_model_hashes():
        manifest = read_neural_model_manifest(model_hash)
        model = create_neural_model(
            session,
            model_key=manifest.get("model_key")
            or f"{manifest['kind']}_{manifest['symbol'].lower()}_{manifest['timeframe'].lower()}",
            kind=manifest["kind"],
            symbol=manifest["symbol"],
            timeframe=manifest["timeframe"],
        )

        existing = get_neural_model_version(session, model_hash)
        status = existing.status if existing is not None else NeuralModelStatus.TRAINED.value

        create_neural_model_version(
            session,
            model_id=model.id,
            model_hash=model_hash,
            status=status,
            train_start=_parse_manifest_datetime(manifest["train_start"]),
            train_end=_parse_manifest_datetime(manifest["train_end"]),
            n_latents=int(manifest["n_latents"]),
            input_features=list(manifest["input_features"]),
            hyperparams=dict(manifest.get("hyperparams", {})),
            val_metrics=dict(manifest.get("val_metrics", {})),
            latent_names=list(manifest["latent_names"]),
            artifact_path=f"neural_models/{model_hash}/encoder.joblib",
        )
    session.flush()
