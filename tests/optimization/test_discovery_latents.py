"""End-to-end discovery tests for production latent universe (WO151)."""

from __future__ import annotations

import random
from datetime import datetime, timezone

import numpy as np
import pandas as pd
import pytest

from q_backend.backtesting.genome.composite_strategy import CompositeStrategy
from q_backend.backtesting.genome.latent_universe import (
    LatentUniverse,
    resolve_latent_universe,
)
from q_backend.backtesting.genome.operators import INDICATOR_KINDS, build_initial_population
from q_backend.backtesting.genome.schema import Genome
from q_backend.features.compute import clear_model_output_cache, compute_feature
from q_backend.features.leakage import assert_neural_oos_only
from q_backend.features.registry import (
    get_feature_spec,
    register_neural_model_features,
    unregister_neural_model_features,
)
from q_backend.neural.promotion import promote_neural_model
from q_backend.neural.training import default_train_encoder_config, train_encoder
from q_backend.optimization.genetic_search import GeneticCandidateProvider
from q_backend.optimization.models import ObjectiveConfig, ObjectiveMode
from q_backend.optimization.strategy_search import GeneticSearchConfig, StrategySearchConfig
from q_backend.optimization.walkforward import WalkForwardConfig
from q_backend.storage.db.models import NeuralModelStatus
from q_backend.storage.db.repositories import set_neural_model_status
from q_backend.optimization.models import StudyConfig

_SYMBOL = "SYN151E2E"
_TIMEFRAME = "H1"
_N_BARS = 300


def _synthetic_bars() -> pd.DataFrame:
    rng = np.random.default_rng(42)
    steps = rng.normal(0.0, 1.0, size=_N_BARS)
    close = 100.0 + np.cumsum(steps)
    open_ = close - rng.normal(0.0, 0.5, size=_N_BARS)
    high = np.maximum(open_, close) + np.abs(rng.normal(0.0, 0.5, size=_N_BARS))
    low = np.minimum(open_, close) - np.abs(rng.normal(0.0, 0.5, size=_N_BARS))
    volume = rng.integers(1_000, 5_000, size=_N_BARS).astype(float)
    times = pd.date_range("2023-01-01", periods=_N_BARS, freq="h", tz="UTC")
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
    columns = {}
    for name in ("rsi", "atr"):
        spec = get_feature_spec(name)
        columns[name] = compute_feature(bars, spec, {}).series.to_numpy()
    return pd.DataFrame(columns, index=bars["time"])


def _train_and_promote(db_session, *, symbol: str = _SYMBOL) -> object:
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
        n_latents=3,
        input_features=("rsi", "atr"),
        model_key="disc_latent_e2e",
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
        study=StudyConfig(name="latent-e2e", n_trials=1),
        genetic=GeneticSearchConfig(population_size=12, init_seed=4242),
    )


def _latent_entry_genome() -> dict:
    return {
        "version": 1,
        "genome_id": "latent-entry-e2e",
        "nodes": [
            {
                "id": "lat",
                "kind": "ind.latent",
                "params": {"latent_index": 0},
                "inputs": [],
            },
            {
                "id": "entry",
                "kind": "cmp.cross_above",
                "params": {"threshold": 0.0},
                "inputs": ["lat"],
            },
            {
                "id": "exit",
                "kind": "cmp.cross_below",
                "params": {"threshold": 0.0},
                "inputs": ["lat"],
            },
        ],
        "entry_long": {"ref": "entry"},
        "entry_short": {"ref": "exit"},
        "exit_long": {"ref": "exit"},
        "exit_short": {"ref": "entry"},
    }


@pytest.fixture
def discovery_db(db_session, lake_root_path):
    from q_backend.features.sync import sync_registry_to_db

    sync_registry_to_db(db_session)
    db_session.commit()
    clear_model_output_cache()
    yield db_session
    clear_model_output_cache()


def test_population_can_include_latent_with_production_model(
    discovery_db,
) -> None:
    version, _bars = _train_and_promote(discovery_db)
    universe = resolve_latent_universe(discovery_db, _SYMBOL, _TIMEFRAME)
    try:
        assert "ind.latent" in universe.indicator_kinds
        assert universe.latent_model_hash == version.model_hash

        search = _search_config(_SYMBOL)
        provider = GeneticCandidateProvider(
            search.genetic,
            search,
            latent_universe=universe,
        )
        assert provider._latent_universe.n_latents == len(version.latent_names)

        # The *seeded population itself* must be able to carry latent nodes — not just a
        # hand-built genome. Assert against the provider's real population so the bridge
        # (seeding → latent genome) is exercised, not merely the universe resolution.
        population = provider._population
        assert any(
            node.kind == "ind.latent" for genome in population for node in genome.nodes
        ), "discovery seeding produced no latent genome despite a PRODUCTION model"

        candidate = provider.candidates()[0]
        assert candidate.fixed_params.get("latent_model_hash") == version.model_hash
    finally:
        unregister_neural_model_features([f"{name}@{version.model_hash[:8]}" for name in version.latent_names])


def test_latent_entering_genome_backtests_pit_safe(discovery_db) -> None:
    version, bars = _train_and_promote(discovery_db)
    ohlcv = bars.set_index("time")
    try:
        strategy = CompositeStrategy(
            genome=_latent_entry_genome(),
            params={},
            symbol=_SYMBOL,
            timeframe=_TIMEFRAME,
            latent_model_hash=version.model_hash,
        )
        result = strategy.compute_indicators(ohlcv)
        series = result["g_lat"].reset_index(drop=True)
        times = bars["time"].reset_index(drop=True)
        assert_neural_oos_only(series, times, version.train_end)
        train_end_ts = pd.Timestamp(version.train_end)
        if train_end_ts.tzinfo is None:
            train_end_ts = train_end_ts.tz_localize("UTC")
        assert series.loc[times > train_end_ts].notna().any()
    finally:
        unregister_neural_model_features([f"{name}@{version.model_hash[:8]}" for name in version.latent_names])


def test_no_production_model_population_matches_legacy_seed() -> None:
    kwargs = dict(
        population_size=12,
        max_nodes=24,
        max_depth=12,
    )
    legacy_rng = random.Random(5150)
    legacy = build_initial_population(legacy_rng, **kwargs)

    empty = LatentUniverse(
        indicator_kinds=tuple(INDICATOR_KINDS),
        latent_model_hash=None,
        n_latents=0,
    )
    modern_rng = random.Random(5150)
    modern = build_initial_population(
        modern_rng,
        indicator_kinds=empty.indicator_kinds,
        n_latents=empty.n_latents,
        **kwargs,
    )

    assert [genome.model_dump() for genome in legacy] == [genome.model_dump() for genome in modern]

    search = _search_config("NO_MODEL")
    legacy_provider = GeneticCandidateProvider(
        search.genetic.model_copy(update={"init_seed": 5150}),
        search,
    )
    modern_provider = GeneticCandidateProvider(
        search.genetic.model_copy(update={"init_seed": 5150}),
        search,
        latent_universe=empty,
    )
    assert [genome.model_dump() for genome in legacy_provider.initial_population] == [
        genome.model_dump() for genome in modern_provider.initial_population
    ]
