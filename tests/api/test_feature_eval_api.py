"""Feature evaluation API tests (WO135)."""

from __future__ import annotations

from datetime import datetime, timezone

import numpy as np
import pandas as pd
import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from contextlib import contextmanager

from fastapi import BackgroundTasks

from q_backend.api.routers.features import (
    _resolve_target_spec,
    get_feature_evaluation,
    get_feature_passport,
    get_features_leaderboard,
    start_feature_evaluation,
)
import q_backend.api.routers.features as features_router
from q_backend.api.schemas.features import FeatureEvalCreateRequest
from q_backend.features.evaluation_service import run_evaluation
from q_backend.features.matrix import FeatureRequest
from q_backend.features.sync import sync_registry_to_db
from q_backend.market_data.models import OHLCV
from q_backend.storage.db.base import Base
from q_backend.storage.settings import get_settings


def _synthetic_bars(n: int = 200) -> pd.DataFrame:
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
    bars: list[OHLCV] = []
    for row in df.itertuples(index=False):
        bars.append(
            OHLCV(
                time=pd.Timestamp(row.time).to_pydatetime(),
                open=float(row.open),
                high=float(row.high),
                low=float(row.low),
                close=float(row.close),
                tick_volume=int(row.volume),
            )
        )
    return bars


@pytest.fixture
def api_db_engine():
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
def api_db_session(api_db_engine) -> Session:
    session_factory = sessionmaker(
        bind=api_db_engine,
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
def seeded_features(api_db_session: Session) -> Session:
    sync_registry_to_db(api_db_session)
    return api_db_session


@pytest.fixture
def lake_root_path(tmp_path, monkeypatch):
    monkeypatch.setenv("Q_DATA_LAKE_ROOT", str(tmp_path))
    get_settings.cache_clear()
    yield tmp_path
    get_settings.cache_clear()


@pytest.fixture
def sample_market(monkeypatch):
    bars_df = _synthetic_bars()
    ohlcv = _bars_to_ohlcv(bars_df)
    start = bars_df["time"].iloc[0].to_pydatetime()
    end = bars_df["time"].iloc[-1].to_pydatetime()

    def _read_ohlcv(symbol: str, timeframe: str, start_dt: datetime, end_dt: datetime):
        return [bar for bar in ohlcv if start_dt <= bar.time <= end_dt]

    monkeypatch.setattr(
        "q_backend.features.matrix.read_ohlcv",
        _read_ohlcv,
    )
    monkeypatch.setattr(
        "q_backend.features.evaluation_service.read_ohlcv",
        _read_ohlcv,
    )
    return {"start": start, "end": end}


def _eval_request(sample_market) -> FeatureEvalCreateRequest:
    return FeatureEvalCreateRequest(
        symbol="EURUSD",
        timeframe="H1",
        start=sample_market["start"],
        end=sample_market["end"],
        target={"name": "fwd_return", "horizon": 5},
        features=[
            {"name": "rsi", "version": None, "params": {"period": 14}},
            {"name": "ma", "version": None, "params": {"period": 20, "ma_type": "sma"}},
        ],
    )


def _run_eval_sync(request: FeatureEvalCreateRequest, session: Session):
    """Drive the evaluation pipeline synchronously (what the endpoint used to do
    inline; it now runs in the background, so tests of the persistence pipeline
    use the kept-synchronous run_evaluation)."""
    target = _resolve_target_spec(request.target.name, request.target.horizon)
    feature_set = [
        FeatureRequest(item.name, item.version, dict(item.params))
        for item in request.features
    ]
    return run_evaluation(
        session,
        symbol=request.symbol,
        timeframe=request.timeframe,
        start=request.start,
        end=request.end,
        target=target,
        feature_set=feature_set,
    )


def test_start_feature_evaluation_is_nonblocking(
    seeded_features: Session, sample_market, lake_root_path, monkeypatch
) -> None:
    # The endpoint must return immediately with a RUNNING run and defer the heavy
    # work to a background task (avoids the client-side HTTP timeout).
    @contextmanager
    def _fake_scope():
        yield seeded_features

    monkeypatch.setattr(features_router, "session_scope", _fake_scope)
    executed: list = []
    monkeypatch.setattr(
        features_router,
        "execute_evaluation_run",
        lambda run_id, **kwargs: executed.append((run_id, kwargs)),
    )

    background = BackgroundTasks()
    started = start_feature_evaluation(_eval_request(sample_market), background)

    assert started["status"] == "running"
    assert started["run_id"]
    # Heavy work was deferred, not run inline.
    assert executed == []
    assert len(background.tasks) == 1
    # Running the scheduled task performs the evaluation.
    background.tasks[0].func(*background.tasks[0].args, **background.tasks[0].kwargs)
    assert len(executed) == 1


def test_feature_eval_post_then_get_returns_leaderboard(
    seeded_features: Session, sample_market, lake_root_path
) -> None:
    run = _run_eval_sync(_eval_request(sample_market), seeded_features)
    assert run.status == "completed"
    run_id = str(run.id)

    payload = get_feature_evaluation(run_id, session=seeded_features)
    assert payload.run_id == run_id
    assert payload.status == "completed"
    assert len(payload.leaderboard) == 2
    scores = [item.global_score or 0.0 for item in payload.leaderboard]
    assert scores == sorted(scores, reverse=True)
    assert payload.clusters
    assert payload.heatmap.metrics == ["ic", "rank_ic", "mutual_info", "stability"]
    assert len(payload.heatmap.rows) == 2


def test_feature_eval_unknown_run_returns_404(seeded_features: Session) -> None:
    with pytest.raises(HTTPException) as exc_info:
        get_feature_evaluation("00000000-0000-0000-0000-000000000000", session=seeded_features)
    assert exc_info.value.status_code == 404


def test_features_leaderboard_returns_latest_scores(
    seeded_features: Session, sample_market, lake_root_path
) -> None:
    _run_eval_sync(_eval_request(sample_market), seeded_features)
    leaderboard = get_features_leaderboard(session=seeded_features)
    assert leaderboard["features"]
    names = {item.feature_name for item in leaderboard["features"]}
    assert "rsi" in names
    assert "ma" in names
    scores = [item.global_score for item in leaderboard["features"]]
    assert scores == sorted(scores, reverse=True)


def test_passport_backfills_score_and_history_after_eval(
    seeded_features: Session, sample_market, lake_root_path
) -> None:
    _run_eval_sync(_eval_request(sample_market), seeded_features)
    passport = get_feature_passport("rsi", session=seeded_features)
    assert passport.score is not None
    assert passport.evaluation_history
    history_item = passport.evaluation_history[0]
    assert "run_id" in history_item
    assert "target" in history_item
    assert history_item["target"] == "fwd_return:5"
