"""Tests for neural latent evaluation gate (WO144)."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

import numpy as np
import pandas as pd
import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from q_backend.features.compute import compute_feature
from q_backend.features.registry import get_feature_spec, unregister_neural_model_features
from q_backend.features.sync import sync_registry_to_db
from q_backend.market_data.models import OHLCV
from q_backend.neural.gate import (
    evaluate_latents,
    latent_feature_set,
    resolve_oos_evaluation_range,
)
from q_backend.neural.training import default_train_encoder_config, train_encoder
from q_backend.storage.db.base import Base
from q_backend.storage.db.models import (
    EvaluationRun,
    FeatureScoreRow,
    NeuralModelStatus,
    NeuralModelVersion,
)
from q_backend.storage.db.repositories import get_neural_model_version
from q_backend.storage.settings import get_settings


def _synthetic_bars(n: int = 260) -> pd.DataFrame:
    rng = np.random.default_rng(42)
    steps = rng.normal(0.0, 1.0, size=n)
    close = 100.0 + np.cumsum(steps)
    open_ = close - rng.normal(0.0, 0.5, size=n)
    high = np.maximum(open_, close) + np.abs(rng.normal(0.0, 0.5, size=n))
    low = np.minimum(open_, close) - np.abs(rng.normal(0.0, 0.5, size=n))
    volume = rng.integers(1_000, 5_000, size=n).astype(float)
    times = pd.date_range("2023-01-01", periods=n, freq="h", tz="UTC")
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


def _bars_to_ohlcv(df: pd.DataFrame) -> list[OHLCV]:
    return [
        OHLCV(
            time=pd.Timestamp(row.time).to_pydatetime(),
            open=float(row.open),
            high=float(row.high),
            low=float(row.low),
            close=float(row.close),
            tick_volume=int(row.volume),
        )
        for row in df.itertuples(index=False)
    ]


def _classical_input_window(bars: pd.DataFrame) -> pd.DataFrame:
    columns = {}
    for name in ("rsi", "atr"):
        spec = get_feature_spec(name)
        columns[name] = compute_feature(bars, spec, {}).series.to_numpy()
    return pd.DataFrame(columns, index=bars["time"])


@pytest.fixture
def db_engine():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    yield engine
    Base.metadata.drop_all(engine)
    engine.dispose()


@pytest.fixture
def db_session(db_engine) -> Session:
    session_factory = sessionmaker(
        bind=db_engine,
        autoflush=False,
        autocommit=False,
        expire_on_commit=False,
    )
    session = session_factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


@pytest.fixture
def seeded_session(db_session: Session) -> Session:
    sync_registry_to_db(db_session)
    return db_session


@pytest.fixture
def lake_root_path(tmp_path, monkeypatch):
    monkeypatch.setenv("Q_DATA_LAKE_ROOT", str(tmp_path))
    get_settings.cache_clear()
    yield tmp_path
    get_settings.cache_clear()


@pytest.fixture
def sample_market(monkeypatch):
    bars_df = _synthetic_bars(260)
    ohlcv = _bars_to_ohlcv(bars_df)

    def _read_ohlcv(symbol: str, timeframe: str, start_dt: datetime, end_dt: datetime):
        return [bar for bar in ohlcv if start_dt <= bar.time <= end_dt]

    for module in (
        "q_backend.features.matrix",
        "q_backend.features.evaluation_service",
        "q_backend.neural.gate",
    ):
        monkeypatch.setattr(f"{module}.read_ohlcv_fresh", _read_ohlcv)
    return bars_df


def _train_version(
    db_session: Session,
    bars_df: pd.DataFrame,
    *,
    n_latents: int = 1,
) -> tuple[NeuralModelVersion, list[str]]:
    train_start = datetime(2023, 1, 1, tzinfo=timezone.utc)
    train_end = datetime(2023, 1, 5, tzinfo=timezone.utc)
    full_window = _classical_input_window(bars_df)
    train_mask = (full_window.index >= train_start) & (full_window.index <= train_end)

    config = default_train_encoder_config(
        symbol="SYN",
        timeframe="H1",
        train_start=train_start,
        train_end=train_end,
        n_latents=n_latents,
        input_features=("rsi", "atr"),
        model_key="gate_test_pca",
    )
    version = train_encoder(
        db_session,
        config,
        feature_window=full_window.loc[train_mask].dropna(),
    )
    keys = [f"{name}@{version.model_hash[:8]}" for name in version.latent_names]
    return version, keys


def _patch_latent_frame(monkeypatch, bars_df: pd.DataFrame, *, correlate: bool):
    from q_backend.features.targets import TargetSpec, compute_target
    from q_backend.storage.lake.artifacts import read_neural_model

    target = TargetSpec(name="fwd_return", horizon=5, kind="regression")

    def _fake(model_hash: str, bars: pd.DataFrame) -> pd.DataFrame:
        encoder = read_neural_model(model_hash)
        df = bars.sort_values("time").reset_index(drop=True)
        label = compute_target(df, target)
        if correlate:
            values = label.to_numpy().copy()
        else:
            values = np.random.default_rng(99).normal(size=len(df))
        train_end = pd.Timestamp(encoder.config.train_end)
        if train_end.tzinfo is None:
            train_end = train_end.tz_localize("UTC")
        values[df["time"] <= train_end] = np.nan
        columns = {
            name: values if index == 0 else np.full(len(df), np.nan)
            for index, name in enumerate(encoder.latent_names)
        }
        return pd.DataFrame(columns, index=df["time"])

    monkeypatch.setattr(
        "q_backend.features.compute._get_or_compute_latent_frame",
        _fake,
    )


def test_evaluate_latents_uses_read_through_seam(
    seeded_session: Session, sample_market, lake_root_path, monkeypatch
) -> None:
    """WO189: neural gate loads bars through read_ohlcv_fresh."""
    calls: list[tuple[str, str, datetime, datetime]] = []

    def _spy(symbol, timeframe, start_dt, end_dt, *, service=None):
        calls.append((symbol, timeframe, start_dt, end_dt))
        ohlcv = _bars_to_ohlcv(sample_market)
        return [bar for bar in ohlcv if start_dt <= bar.time <= end_dt]

    monkeypatch.setattr("q_backend.neural.gate.read_ohlcv_fresh", _spy)
    version, keys = _train_version(seeded_session, sample_market)
    _patch_latent_frame(monkeypatch, sample_market, correlate=True)
    try:
        evaluate_latents(
            seeded_session,
            version,
            target_name="fwd_return",
            horizon=5,
        )
        assert any(call[0] == "SYN" and call[1] == "H1" for call in calls)
    finally:
        unregister_neural_model_features(keys)


def test_latent_feature_set_one_request_per_latent(
    seeded_session: Session, sample_market, lake_root_path
) -> None:
    version, keys = _train_version(seeded_session, sample_market)
    try:
        requests = latent_feature_set(version)
        assert len(requests) == version.n_latents
        assert {req.name for req in requests} == set(keys)

        oos_start, _oos_end = resolve_oos_evaluation_range(version)
        assert oos_start > version.train_end
    finally:
        unregister_neural_model_features(keys)


def test_evaluate_latents_persists_run_and_scores(
    seeded_session: Session, sample_market, lake_root_path, monkeypatch
) -> None:
    version, keys = _train_version(seeded_session, sample_market)
    _patch_latent_frame(monkeypatch, sample_market, correlate=True)
    try:
        result = evaluate_latents(
            seeded_session,
            version,
            target_name="fwd_return",
            horizon=5,
        )
        assert result.evaluation_run_id is not None

        run = seeded_session.get(EvaluationRun, uuid.UUID(result.evaluation_run_id))
        assert run is not None
        assert run.status == "completed"

        score_rows = seeded_session.execute(
            select(FeatureScoreRow).where(FeatureScoreRow.run_id == run.id)
        ).scalars().all()
        assert len(score_rows) == version.n_latents
        assert {row.feature_name for row in score_rows} == set(keys)
    finally:
        unregister_neural_model_features(keys)


def test_passing_gate_sets_candidate_status(
    seeded_session: Session, sample_market, lake_root_path, monkeypatch
) -> None:
    version, keys = _train_version(seeded_session, sample_market)
    _patch_latent_frame(monkeypatch, sample_market, correlate=True)
    try:
        result = evaluate_latents(
            seeded_session,
            version,
            target_name="fwd_return",
            horizon=5,
        )
        assert result.passed is True
        assert result.best_latent_ic > result.baseline_ic
        assert result.n_latents_beating_baseline >= 1

        refreshed = get_neural_model_version(seeded_session, version.model_hash)
        assert refreshed is not None
        assert refreshed.status == NeuralModelStatus.CANDIDATE.value
    finally:
        unregister_neural_model_features(keys)


def test_failing_gate_leaves_trained_status(
    seeded_session: Session, sample_market, lake_root_path, monkeypatch
) -> None:
    version, keys = _train_version(seeded_session, sample_market)
    _patch_latent_frame(monkeypatch, sample_market, correlate=False)
    try:
        result = evaluate_latents(
            seeded_session,
            version,
            target_name="fwd_return",
            horizon=5,
        )
        assert result.passed is False

        refreshed = get_neural_model_version(seeded_session, version.model_hash)
        assert refreshed is not None
        assert refreshed.status == NeuralModelStatus.TRAINED.value
    finally:
        unregister_neural_model_features(keys)
