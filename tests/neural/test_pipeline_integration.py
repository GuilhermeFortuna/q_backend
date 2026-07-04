"""End-to-end neural pipeline test: build_feature_matrix -> encoder -> gate.

Unlike the unit/job tests, this stubs ONLY the raw OHLCV data source and exercises
the real feature matrix, real encoder fit/transform, and real gate. It is the
regression net for the bugs that slipped through stubbed boundaries on first live
use: feature_id-vs-name columns, warm-up NaNs, and gate-failure rollback.
"""

from __future__ import annotations

from datetime import datetime, timezone

import numpy as np
import pandas as pd
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from q_backend.market_data.models import OHLCV
from q_backend.neural.training import default_train_encoder_config
from q_backend.neural.training_pipeline import (
    TrainEncoderEvaluateSpec,
    run_train_encoder_pipeline,
)
from q_backend.storage.db.base import Base
from q_backend.storage.db.repositories import get_neural_model_version

_INPUT_FEATURES = ("rsi", "ma", "macd", "realized_vol")
_N_BARS = 600
# train_start/train_end are tz-aware UTC (as stored in the DB / encoder config), but
# bar times are tz-naive — exactly how the data lake (local_store) delivers OHLCV.
# This mismatch is what broke the gate in production; keep it here so the path is real.
_INDEX = pd.date_range("2024-01-01", periods=_N_BARS, freq="h", tz="UTC")
_BAR_INDEX = _INDEX.tz_localize(None)


def _synthetic_bars() -> list[OHLCV]:
    steps = np.arange(_N_BARS)
    close = 100.0 + 10.0 * np.sin(steps / 5.0) + 0.05 * steps
    return [
        OHLCV(
            time=_BAR_INDEX[i].to_pydatetime(),
            open=float(close[i]),
            high=float(close[i] + 1.0),
            low=float(close[i] - 1.0),
            close=float(close[i]),
            tick_volume=1000,
        )
        for i in range(_N_BARS)
    ]


@pytest.fixture
def integration_session():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    session = factory()
    # Seed FeatureDefinition rows the way the API does on startup — the gate's
    # classical baseline evaluation looks them up.
    from q_backend.features.sync import sync_registry_to_db

    sync_registry_to_db(session)
    session.commit()
    try:
        yield session
    finally:
        session.close()
        Base.metadata.drop_all(engine)
        engine.dispose()


@pytest.fixture
def lake_root_path(tmp_path, monkeypatch):
    monkeypatch.setenv("Q_DATA_LAKE_ROOT", str(tmp_path))
    from q_backend.storage.settings import get_settings

    get_settings.cache_clear()
    yield tmp_path
    get_settings.cache_clear()


@pytest.fixture
def stub_ohlcv(monkeypatch):
    bars = _synthetic_bars()

    def _strip_tz(value) -> pd.Timestamp:
        ts = pd.Timestamp(value)
        return ts.tz_localize(None) if ts.tzinfo is not None else ts

    def _read_ohlcv(symbol, timeframe, start_dt, end_dt):
        # local_store returns tz-naive bar times; callers pass tz-aware bounds.
        # Mirror its tz-robust filtering rather than comparing naive vs aware.
        lo = _strip_tz(start_dt)
        hi = _strip_tz(end_dt)
        return [bar for bar in bars if lo <= pd.Timestamp(bar.time) <= hi]

    monkeypatch.setattr("q_backend.features.matrix.read_ohlcv_fresh", _read_ohlcv)
    monkeypatch.setattr("q_backend.neural.gate.read_ohlcv_fresh", _read_ohlcv)
    return bars


def _config(train_end_index: int, model_key: str):
    return default_train_encoder_config(
        kind="pca",
        symbol="SYNINT",
        timeframe="H1",
        train_start=_INDEX[0].to_pydatetime(),
        train_end=_INDEX[train_end_index].to_pydatetime(),
        n_latents=2,
        input_features=_INPUT_FEATURES,
        model_key=model_key,
        hyperparams={},
    )


def test_pipeline_trains_and_gates_end_to_end(
    integration_session, lake_root_path, stub_ohlcv
) -> None:
    """Real matrix -> encoder -> gate produces a registered model and a gate verdict."""
    # train_end leaves ~250 OOS bars (> the 100-bar gate minimum).
    config = _config(train_end_index=350, model_key="int_pca_ok")

    result = run_train_encoder_pipeline(
        integration_session,
        config,
        input_features=_INPUT_FEATURES,
        evaluate=TrainEncoderEvaluateSpec(target="fwd_return", horizon=3),
    )

    assert result.model_hash
    assert result.gate_error is None
    assert result.gate is not None
    # The gate ran for real: finite baseline + latent ICs from feature_score_rows.
    assert np.isfinite(result.gate.baseline_ic)
    assert np.isfinite(result.gate.best_latent_ic)

    version = get_neural_model_version(integration_session, result.model_hash)
    assert version is not None
    assert version.n_latents == 2


def test_pipeline_keeps_model_when_gate_cannot_run(
    integration_session, lake_root_path, stub_ohlcv
) -> None:
    """A gate failure (no OOS bars after train_end) must NOT discard the trained model."""
    # train_end at the last bar -> zero OOS bars -> gate raises inside the pipeline.
    config = _config(train_end_index=_N_BARS - 1, model_key="int_pca_no_oos")

    result = run_train_encoder_pipeline(
        integration_session,
        config,
        input_features=_INPUT_FEATURES,
        evaluate=TrainEncoderEvaluateSpec(target="fwd_return", horizon=3),
    )

    # Model survives; gate is reported as skipped with a reason.
    assert result.model_hash
    assert result.gate is None
    assert result.gate_error is not None
    assert "OOS" in result.gate_error or "oos" in result.gate_error.lower()

    version = get_neural_model_version(integration_session, result.model_hash)
    assert version is not None
