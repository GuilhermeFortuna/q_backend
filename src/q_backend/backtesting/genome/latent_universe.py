"""Per-run GA discovery universe when a PRODUCTION neural model exists (WO151)."""

from __future__ import annotations

import logging
import random
from dataclasses import dataclass

from sqlalchemy.orm import Session

from q_backend.backtesting.genome.node_specs import NODE_SPECS
from q_backend.storage.db.models import NeuralModelStatus
from q_backend.storage.db.repositories import list_neural_model_versions

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class LatentUniverse:
    indicator_kinds: tuple[str, ...]
    latent_model_hash: str | None
    n_latents: int


def empty_latent_universe() -> LatentUniverse:
    """Static indicator universe with no production neural model."""
    from q_backend.backtesting.genome.operators import INDICATOR_KINDS

    return LatentUniverse(
        indicator_kinds=tuple(INDICATOR_KINDS),
        latent_model_hash=None,
        n_latents=0,
    )


def clamp_latent_index(latent_index: int, n_latents: int) -> int:
    if n_latents <= 0:
        return 0
    return min(max(0, int(latent_index)), n_latents - 1)


def sample_latent_index(rng: random.Random, n_latents: int) -> int:
    if n_latents <= 0:
        return 0
    return rng.randint(0, n_latents - 1)


def resolve_latent_universe(
    session: Session,
    symbol: str,
    timeframe: str,
) -> LatentUniverse:
    """Resolve the GA indicator universe for one discovery run's instrument."""
    from q_backend.backtesting.genome.operators import INDICATOR_KINDS

    production = [
        version
        for version in list_neural_model_versions(session, status=NeuralModelStatus.PRODUCTION.value)
        if version.model.symbol == symbol and version.model.timeframe == timeframe
    ]
    if not production:
        return empty_latent_universe()

    if len(production) > 1:
        logger.warning(
            "Multiple PRODUCTION neural models for %s/%s; using newest (hash=%s)",
            symbol,
            timeframe,
            production[0].model_hash[:8],
        )

    version = production[0]
    return LatentUniverse(
        indicator_kinds=tuple(sorted((*INDICATOR_KINDS, "ind.latent"))),
        latent_model_hash=version.model_hash,
        n_latents=len(version.latent_names),
    )


def latent_index_bounds(n_latents: int) -> tuple[int, int] | None:
    """Inclusive ``[min, max]`` for ``latent_index`` sampling, or ``None`` when unavailable."""
    if n_latents <= 0:
        return None
    return 0, n_latents - 1


def is_latent_node_kind(kind: str) -> bool:
    return kind == "ind.latent" and "ind.latent" in NODE_SPECS
