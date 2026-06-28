"""Tests for per-run latent discovery universe resolution (WO151)."""

from __future__ import annotations

from datetime import datetime, timezone

import numpy as np
import pandas as pd
import pytest

from q_backend.backtesting.genome.latent_universe import (
    empty_latent_universe,
    resolve_latent_universe,
    sample_latent_index,
)
from q_backend.backtesting.genome.operators import INDICATOR_KINDS
from q_backend.neural.promotion import promote_neural_model
from q_backend.neural.training import default_train_encoder_config, train_encoder
from q_backend.storage.db.models import NeuralModelStatus
from q_backend.storage.db.repositories import (
    get_neural_model_version,
    set_neural_model_status,
)


def _train_version(
    db_session,
    lake_root_path,
    *,
    model_key: str,
    symbol: str = "SYN151",
    train_end_day: int = 10,
):
    start = datetime(2024, 1, 1, tzinfo=timezone.utc)
    end = datetime(2024, 1, train_end_day, tzinfo=timezone.utc)
    features = tuple(f"feature_{index:02d}" for index in range(1, 7))
    config = default_train_encoder_config(
        symbol=symbol,
        timeframe="H1",
        train_start=start,
        train_end=end,
        n_latents=4,
        input_features=features,
        model_key=model_key,
    )
    rng = np.random.default_rng(7)
    index = pd.date_range(start, end, freq="h", tz="UTC")
    frame = pd.DataFrame(
        rng.normal(size=(len(index), len(features))),
        index=index,
        columns=features,
    )
    return train_encoder(db_session, config, feature_window=frame)


def _promote(db_session, version) -> None:
    set_neural_model_status(
        db_session,
        model_hash=version.model_hash,
        status=NeuralModelStatus.CANDIDATE.value,
    )
    promote_neural_model(
        db_session,
        model_hash=version.model_hash,
        target_status=NeuralModelStatus.PRODUCTION.value,
    )


def test_resolve_latent_universe_without_production_model(db_session) -> None:
    universe = resolve_latent_universe(db_session, "NOPE", "H1")
    assert universe.indicator_kinds == tuple(INDICATOR_KINDS)
    assert universe.latent_model_hash is None
    assert universe.n_latents == 0


def test_resolve_latent_universe_with_production_model(
    db_session, lake_root_path
) -> None:
    version = _train_version(db_session, lake_root_path, model_key="uni_a")
    _promote(db_session, version)

    universe = resolve_latent_universe(db_session, "SYN151", "H1")

    assert "ind.latent" in universe.indicator_kinds
    assert universe.latent_model_hash == version.model_hash
    assert universe.n_latents == len(version.latent_names)


def test_resolve_latent_universe_uses_newest_when_multiple_production(
    db_session, lake_root_path, caplog
) -> None:
    older = _train_version(db_session, lake_root_path, model_key="uni_old", train_end_day=10)
    newer = _train_version(db_session, lake_root_path, model_key="uni_new", train_end_day=12)
    _promote(db_session, older)
    _promote(db_session, newer)

    # Defensive path: force both to PRODUCTION (promotion normally demotes the prior).
    set_neural_model_status(
        db_session,
        model_hash=older.model_hash,
        status=NeuralModelStatus.PRODUCTION.value,
    )

    with caplog.at_level("WARNING"):
        universe = resolve_latent_universe(db_session, "SYN151", "H1")

    assert universe.latent_model_hash == newer.model_hash
    assert any("Multiple PRODUCTION" in record.message for record in caplog.records)


def test_sample_latent_index_respects_run_bounds() -> None:
    import random

    rng = random.Random(0)
    values = {sample_latent_index(rng, 3) for _ in range(50)}
    assert values.issubset({0, 1, 2})
    assert sample_latent_index(rng, 0) == 0


def test_empty_latent_universe_matches_static_indicator_kinds() -> None:
    universe = empty_latent_universe()
    assert universe.indicator_kinds == tuple(INDICATOR_KINDS)
    assert universe.latent_model_hash is None
    assert universe.n_latents == 0


def _swap_reaches_latent(indicator_kinds, n_latents, *, trials: int = 600) -> bool:
    """True if repeated swap-indicator mutations can introduce an ``ind.latent`` node."""
    import random

    from q_backend.backtesting.genome.operators import (
        _mutate_swap_indicator,
        build_random_genome,
    )
    from q_backend.backtesting.genome.validate import GenomeValidationError

    rng = random.Random(11)
    for _ in range(trials):
        genome = build_random_genome(
            rng,
            genome_id="g",
            generation=0,
            max_nodes=24,
            max_depth=12,
            indicator_kinds=indicator_kinds,
            n_latents=n_latents,
        )
        try:
            mutated = _mutate_swap_indicator(
                rng,
                genome,
                max_nodes=24,
                max_depth=12,
                indicator_kinds=indicator_kinds,
                n_latents=n_latents,
            )
        except GenomeValidationError:
            continue
        if any(node.kind == "ind.latent" for node in mutated.nodes):
            return True
    return False


def test_swap_indicator_can_reach_latent_when_in_universe() -> None:
    # Fix A: _mutate_swap_indicator must honor the per-run indicator_kinds so a
    # production model's latents are reachable via mutation, not only via seeding.
    kinds = tuple(sorted((*INDICATOR_KINDS, "ind.latent")))
    assert _swap_reaches_latent(kinds, n_latents=8)


def test_swap_indicator_never_reaches_latent_without_production_model() -> None:
    # The no-model guarantee at the mutation level: ind.latent is absent from the
    # static universe, so a swap can never conjure it.
    assert not _swap_reaches_latent(tuple(INDICATOR_KINDS), n_latents=0)
