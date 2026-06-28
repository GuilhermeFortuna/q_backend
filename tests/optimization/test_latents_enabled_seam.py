"""Regression tests for the latents_enabled control-arm seam (WO153)."""

from __future__ import annotations

from contextlib import contextmanager
from unittest.mock import patch

import numpy as np
import pandas as pd
import pytest

from q_backend.features.compute import clear_model_output_cache, compute_feature
from q_backend.features.registry import (
    get_feature_spec,
    register_neural_model_features,
    unregister_neural_model_features,
)
from q_backend.neural.promotion import promote_neural_model
from q_backend.neural.training import default_train_encoder_config, train_encoder
from q_backend.optimization.genetic_search import create_genetic_candidate_provider
from q_backend.optimization.models import ObjectiveConfig, ObjectiveMode, StudyConfig
from q_backend.optimization.strategy_search import GeneticSearchConfig, StrategySearchConfig
from q_backend.optimization.walkforward import WalkForwardConfig
from q_backend.storage.db.models import NeuralModelStatus
from q_backend.storage.db.repositories import set_neural_model_status

_SYMBOL = "SYN153SEAM"
_NO_MODEL_SYMBOL = "SYN153NOMODEL"
_TIMEFRAME = "H1"
_INIT_SEED = 4242
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
        n_latents=3,
        input_features=("rsi", "atr"),
        model_key="latents_enabled_seam",
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


def _search_config(symbol: str, *, latents_enabled: bool = True) -> StrategySearchConfig:
    return StrategySearchConfig(
        backtest={
            "symbol": symbol,
            "timeframe": _TIMEFRAME,
            "start": "2023-01-01",
            "end": "2023-02-01",
        },
        objective=ObjectiveConfig(mode=ObjectiveMode.MAXIMIZE_NET_PROFIT),
        walkforward=WalkForwardConfig(train_days=30, test_days=10, min_windows=1),
        study=StudyConfig(name="latents-enabled-seam", n_trials=1),
        genetic=GeneticSearchConfig(population_size=12, init_seed=_INIT_SEED),
        latents_enabled=latents_enabled,
    )


def _population_dumps(provider) -> list[dict]:
    return [genome.model_dump() for genome in provider.initial_population]


def _population_has_latent(provider) -> bool:
    return any(
        node.kind == "ind.latent"
        for genome in provider.initial_population
        for node in genome.nodes
    )


@pytest.fixture
def discovery_db(db_session, lake_root_path):
    from q_backend.features.sync import sync_registry_to_db

    sync_registry_to_db(db_session)
    db_session.commit()
    clear_model_output_cache()
    yield db_session
    clear_model_output_cache()


@pytest.fixture
def genetic_session_scope(discovery_db):
    @contextmanager
    def test_session_scope():
        yield discovery_db

    return test_session_scope


def test_latents_enabled_false_is_byte_identical_to_no_model_path(
    discovery_db, genetic_session_scope
) -> None:
    version, _bars = _train_and_promote(discovery_db)
    try:
        search_with_model = _search_config(_SYMBOL)
        search_control = _search_config(_SYMBOL, latents_enabled=False)
        search_no_model = _search_config(_NO_MODEL_SYMBOL)

        with patch(
            "q_backend.optimization.genetic_search.session_scope",
            genetic_session_scope,
        ):
            with_latents = create_genetic_candidate_provider(
                search_with_model.genetic,
                search_with_model,
                latents_enabled=True,
            )
            control_override = create_genetic_candidate_provider(
                search_control.genetic,
                search_control,
                latents_enabled=False,
            )
            no_model = create_genetic_candidate_provider(
                search_no_model.genetic,
                search_no_model,
                latents_enabled=True,
            )

        assert with_latents._latent_universe.latent_model_hash == version.model_hash
        assert _population_has_latent(with_latents), (
            "latents_enabled=True must seed latent genomes when a PRODUCTION model exists"
        )
        assert not _population_has_latent(control_override)
        assert not _population_has_latent(no_model)

        assert control_override._latent_universe.n_latents == 0
        assert control_override._latent_universe.latent_model_hash is None
        assert control_override._kind_weights == {}

        assert _population_dumps(control_override) == _population_dumps(no_model)

        # Revert-guard: ignoring the seam would keep latents on and break this assertion.
        assert _population_dumps(with_latents) != _population_dumps(control_override)
    finally:
        unregister_neural_model_features(
            [f"{name}@{version.model_hash[:8]}" for name in version.latent_names]
        )


def test_latents_disabled_skips_db_resolution(discovery_db) -> None:
    _train_and_promote(discovery_db)
    search = _search_config(_SYMBOL, latents_enabled=False)

    with patch("q_backend.optimization.genetic_search.session_scope") as mock_scope:
        provider = create_genetic_candidate_provider(
            search.genetic,
            search,
            latents_enabled=False,
        )

    mock_scope.assert_not_called()
    assert provider._latent_universe.n_latents == 0
    assert provider._latent_universe.latent_model_hash is None
