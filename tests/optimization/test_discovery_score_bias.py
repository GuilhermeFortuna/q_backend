"""End-to-end discovery tests for score-biased seeding (WO152)."""

from __future__ import annotations

import random
import uuid
from datetime import datetime, timezone

import numpy as np
import pandas as pd
import pytest

from q_backend.backtesting.genome.latent_universe import resolve_latent_universe
from q_backend.backtesting.genome.operators import build_initial_population
from q_backend.backtesting.genome.score_bias import build_kind_weights
from q_backend.features.registry import (
    neural_catalog_key,
    register_neural_model_features,
    unregister_neural_model_features,
)
from q_backend.neural.promotion import promote_neural_model
from q_backend.neural.training import default_train_encoder_config, train_encoder
from q_backend.optimization.genetic_search import GeneticCandidateProvider
from q_backend.optimization.models import ObjectiveConfig, ObjectiveMode, StudyConfig
from q_backend.optimization.strategy_search import GeneticSearchConfig, StrategySearchConfig
from q_backend.optimization.walkforward import WalkForwardConfig
from q_backend.storage.db.models import EvaluationRun, FeatureScoreRow, NeuralModelStatus, RunStatus
from q_backend.storage.db.repositories import set_neural_model_status

_SYMBOL = "SYN152E2E"
_TIMEFRAME = "H1"
_MASTER_SEED = 4242
_N_SEEDS = 40


def _synthetic_bars() -> pd.DataFrame:
    rng = np.random.default_rng(42)
    n_bars = 300
    steps = rng.normal(0.0, 1.0, size=n_bars)
    close = 100.0 + np.cumsum(steps)
    open_ = close - rng.normal(0.0, 0.5, size=n_bars)
    high = np.maximum(open_, close) + np.abs(rng.normal(0.0, 0.5, size=n_bars))
    low = np.minimum(open_, close) - np.abs(rng.normal(0.0, 0.5, size=n_bars))
    volume = rng.integers(1_000, 5_000, size=n_bars).astype(float)
    times = pd.date_range("2023-01-01", periods=n_bars, freq="h", tz="UTC")
    return pd.DataFrame(
        {
            "time": times,
            "open": open_,
            "high": high,
            "low": low,
            "close": close,
            "volume": volume,
        }
    )


def _classical_input_window(bars: pd.DataFrame) -> pd.DataFrame:
    from q_backend.features.compute import compute_feature
    from q_backend.features.registry import get_feature_spec

    columns = {}
    for name in ("rsi", "atr"):
        spec = get_feature_spec(name)
        columns[name] = compute_feature(bars, spec, {}).series.to_numpy()
    return pd.DataFrame(columns, index=bars["time"])


def _train_and_promote(db_session, *, symbol: str = _SYMBOL):
    bars = _synthetic_bars()
    train_start = bars["time"].iloc[0].to_pydatetime()
    train_end = bars["time"].iloc[80].to_pydatetime()
    full_window = _classical_input_window(bars)
    train_mask = (full_window.index >= train_start) & (full_window.index <= train_end)
    config = default_train_encoder_config(
        symbol=symbol,
        timeframe=_TIMEFRAME,
        train_start=train_start,
        train_end=train_end,
        n_latents=4,
        input_features=("rsi", "atr"),
        model_key="score_bias_e2e",
    )
    version = train_encoder(
        db_session,
        config,
        feature_window=full_window.loc[train_mask].dropna(),
    )
    register_neural_model_features(version)
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
    return version, bars


def _seed_latent_dominant_scores(db_session, version) -> EvaluationRun:
    run = EvaluationRun(
        id=uuid.uuid4(),
        symbol=_SYMBOL,
        timeframe=_TIMEFRAME,
        start=datetime(2023, 1, 1, tzinfo=timezone.utc),
        end=datetime(2023, 2, 1, tzinfo=timezone.utc),
        target_name="fwd_return",
        target_horizon=5,
        matrix_id="matrix-score-bias",
        status=RunStatus.COMPLETED.value,
        feature_count=len(version.latent_names),
        finished_at=datetime(2023, 2, 2, tzinfo=timezone.utc),
    )
    db_session.add(run)
    db_session.flush()

    dominant_name = version.latent_names[3]
    dominant_key = neural_catalog_key(dominant_name, version.model_hash)
    for index, latent_name in enumerate(version.latent_names):
        catalog = neural_catalog_key(latent_name, version.model_hash)
        ic = 0.17 if catalog == dominant_key else 0.03
        db_session.add(
            FeatureScoreRow(
                run_id=run.id,
                feature_id=f"latent-{index}",
                feature_name=catalog,
                ic=ic,
                rank_ic=ic,
                mutual_info=ic,
                stability=0.9,
                global_score=ic,
                cluster_id=0,
                is_representative=catalog == dominant_key,
                leakage_status="clean",
                regime_ics={},
            )
        )
    for classical_name, ic in (("rsi", 0.04), ("macd", 0.02)):
        db_session.add(
            FeatureScoreRow(
                run_id=run.id,
                feature_id=f"classical-{classical_name}",
                feature_name=classical_name,
                ic=ic,
                rank_ic=ic,
                mutual_info=ic,
                stability=0.9,
                global_score=ic,
                cluster_id=0,
                is_representative=False,
                leakage_status="clean",
                regime_ics={},
            )
        )
    db_session.flush()
    return run


def _search_config(symbol: str) -> StrategySearchConfig:
    return StrategySearchConfig(
        backtest={
            "symbol": symbol,
            "timeframe": _TIMEFRAME,
            "start": "2023-01-01",
            "end": "2023-02-01",
        },
        objective=ObjectiveConfig(mode=ObjectiveMode.MAXIMIZE_NET_PROFIT),
        walkforward=WalkForwardConfig(train_days=30, test_days=10, min_windows=1),
        study=StudyConfig(name="score-bias-e2e", n_trials=1),
        genetic=GeneticSearchConfig(population_size=24, init_seed=_MASTER_SEED),
    )


def _count_latent_nodes(population) -> int:
    return sum(
        1
        for genome in population
        if any(node.kind == "ind.latent" for node in genome.nodes)
    )


def _population_latent_counts(
    *,
    universe,
    kind_weights: dict[str, float],
    seed_start: int,
    count: int,
) -> list[int]:
    counts: list[int] = []
    for offset in range(count):
        rng = random.Random(seed_start + offset)
        population = build_initial_population(
            rng,
            population_size=24,
            max_nodes=24,
            max_depth=12,
            indicator_kinds=universe.indicator_kinds,
            n_latents=universe.n_latents,
            kind_weights=kind_weights,
        )
        counts.append(_count_latent_nodes(population))
    return counts


@pytest.fixture
def discovery_db(db_session, lake_root_path):
    from q_backend.features.sync import sync_registry_to_db

    sync_registry_to_db(db_session)
    db_session.commit()
    yield db_session


def test_dominant_latent_kind_seeded_more_often_than_uniform(discovery_db) -> None:
    version, _bars = _train_and_promote(discovery_db)
    _seed_latent_dominant_scores(discovery_db, version)
    universe = resolve_latent_universe(discovery_db, _SYMBOL, _TIMEFRAME)
    kind_weights = build_kind_weights(
        discovery_db,
        symbol=_SYMBOL,
        timeframe=_TIMEFRAME,
        n_latents=universe.n_latents,
        start=datetime(2023, 1, 1, tzinfo=timezone.utc),
        end=datetime(2023, 2, 1, tzinfo=timezone.utc),
        latent_model_hash=universe.latent_model_hash,
    )
    try:
        assert kind_weights["ind.latent"] > kind_weights.get("ind.rsi", 1.0)

        biased_counts = _population_latent_counts(
            universe=universe,
            kind_weights=kind_weights,
            seed_start=_MASTER_SEED,
            count=_N_SEEDS,
        )
        uniform_counts = _population_latent_counts(
            universe=universe,
            kind_weights={},
            seed_start=_MASTER_SEED,
            count=_N_SEEDS,
        )
        assert sum(biased_counts) > sum(uniform_counts)
        assert sum(biased_counts) / len(biased_counts) > sum(uniform_counts) / len(
            uniform_counts
        )
    finally:
        unregister_neural_model_features(
            [neural_catalog_key(name, version.model_hash) for name in version.latent_names]
        )


def test_no_scores_population_matches_uniform_for_fixed_seed(discovery_db) -> None:
    version, _bars = _train_and_promote(discovery_db)
    universe = resolve_latent_universe(discovery_db, _SYMBOL, _TIMEFRAME)
    search = _search_config(_SYMBOL)
    genetic = search.genetic
    population_kwargs = dict(
        population_size=genetic.population_size,
        max_nodes=genetic.max_nodes,
        max_depth=genetic.max_depth,
        min_seed_signals=genetic.min_seed_signals,
        repair_max_attempts=genetic.repair_max_attempts,
        seed_exit_policies=genetic.seed_exit_policies,
        exit_policy_preset_ids=genetic.exit_policy_preset_ids,
        exit_policy_seed_fraction=genetic.exit_policy_seed_fraction,
        indicator_kinds=universe.indicator_kinds,
        n_latents=universe.n_latents,
    )
    try:
        uniform_rng = random.Random(_MASTER_SEED)
        uniform = build_initial_population(uniform_rng, **population_kwargs)

        no_scores_rng = random.Random(_MASTER_SEED)
        no_scores = build_initial_population(
            no_scores_rng,
            kind_weights={},
            **population_kwargs,
        )

        assert [genome.model_dump() for genome in uniform] == [
            genome.model_dump() for genome in no_scores
        ]

        provider = GeneticCandidateProvider(
            search.genetic,
            search,
            latent_universe=universe,
            kind_weights={},
        )
        assert [
            genome.model_dump() for genome in provider.initial_population
        ] == [genome.model_dump() for genome in uniform]
    finally:
        unregister_neural_model_features(
            [neural_catalog_key(name, version.model_hash) for name in version.latent_names]
        )
