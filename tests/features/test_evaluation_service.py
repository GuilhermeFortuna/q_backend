"""Tests for feature evaluation orchestration (WO135)."""

from __future__ import annotations

from datetime import datetime, timezone

import numpy as np
import pandas as pd
import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from q_backend.features.evaluation_service import run_evaluation
from q_backend.features.matrix import FeatureRequest
from q_backend.features.sync import sync_registry_to_db
from q_backend.features.targets import list_target_specs
from q_backend.market_data.models import OHLCV
from q_backend.storage.db.base import Base
from q_backend.storage.db.models import EvaluationRun, FeatureScoreRow, RunStatus
from q_backend.storage.db.repositories import get_feature_definition
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
    bars_df = _synthetic_bars()
    ohlcv = _bars_to_ohlcv(bars_df)
    start = bars_df["time"].iloc[0].to_pydatetime()
    end = bars_df["time"].iloc[-1].to_pydatetime()

    def _read_ohlcv(symbol: str, timeframe: str, start_dt: datetime, end_dt: datetime):
        return [bar for bar in ohlcv if start_dt <= bar.time <= end_dt]

    monkeypatch.setattr(
        "q_backend.features.matrix.read_ohlcv_fresh",
        _read_ohlcv,
    )
    monkeypatch.setattr(
        "q_backend.features.evaluation_service.read_ohlcv_fresh",
        _read_ohlcv,
    )
    return {"start": start, "end": end}


def test_run_evaluation_uses_read_through_seam(
    seeded_session: Session, sample_market, lake_root_path, monkeypatch
) -> None:
    """WO189: evaluation_service loads bars through read_ohlcv_fresh."""
    calls: list[tuple[str, str, datetime, datetime]] = []

    def _spy(symbol, timeframe, start_dt, end_dt, *, service=None):
        calls.append((symbol, timeframe, start_dt, end_dt))
        return [
            bar
            for bar in _bars_to_ohlcv(_synthetic_bars())
            if start_dt <= bar.time <= end_dt
        ]

    monkeypatch.setattr(
        "q_backend.features.evaluation_service.read_ohlcv_fresh",
        _spy,
    )
    target = next(
        spec for spec in list_target_specs([5]) if spec.name == "fwd_return"
    )
    run_evaluation(
        seeded_session,
        symbol="EURUSD",
        timeframe="H1",
        start=sample_market["start"],
        end=sample_market["end"],
        target=target,
        feature_set=[FeatureRequest("rsi", None, {"period": 14})],
    )
    assert ("EURUSD", "H1", sample_market["start"], sample_market["end"]) in calls


def test_run_evaluation_persists_run_and_scores(
    seeded_session: Session, sample_market, lake_root_path
) -> None:
    target = next(
        spec
        for spec in list_target_specs([5])
        if spec.name == "fwd_return"
    )
    feature_set = [
        FeatureRequest("rsi", None, {"period": 14}),
        FeatureRequest("ma", None, {"period": 20, "ma_type": "sma"}),
    ]

    rsi_before = get_feature_definition(seeded_session, "rsi")
    assert rsi_before is not None
    usage_before = rsi_before.usage_count

    run = run_evaluation(
        seeded_session,
        symbol="EURUSD",
        timeframe="H1",
        start=sample_market["start"],
        end=sample_market["end"],
        target=target,
        feature_set=feature_set,
    )

    assert isinstance(run, EvaluationRun)
    assert run.status == "completed"
    assert run.feature_count == 2
    assert run.matrix_id
    assert run.result_summary is not None
    assert "recommended_feature_ids" in run.result_summary
    assert "cluster_count" in run.result_summary
    assert run.result_summary["cluster_count"] >= 1

    score_rows = seeded_session.execute(
        select(FeatureScoreRow).where(FeatureScoreRow.run_id == run.id)
    ).scalars().all()
    assert len(score_rows) == 2
    assert {row.feature_name for row in score_rows} == {"rsi", "ma"}

    rsi_after = get_feature_definition(seeded_session, "rsi")
    assert rsi_after is not None
    assert rsi_after.usage_count == usage_before + 1


def test_run_evaluation_marks_failed_and_reraises_on_error(
    seeded_session: Session, sample_market, lake_root_path, monkeypatch
) -> None:
    """WO179 must-surface: an evaluation failure records FAILED + message and re-raises."""
    target = next(
        spec for spec in list_target_specs([5]) if spec.name == "fwd_return"
    )
    feature_set = [FeatureRequest("rsi", None, {"period": 14})]

    def _boom(*_args, **_kwargs):
        raise RuntimeError("matrix build exploded")

    monkeypatch.setattr(
        "q_backend.features.evaluation_service.build_feature_matrix", _boom
    )

    with pytest.raises(RuntimeError, match="matrix build exploded"):
        run_evaluation(
            seeded_session,
            symbol="EURUSD",
            timeframe="H1",
            start=sample_market["start"],
            end=sample_market["end"],
            target=target,
            feature_set=feature_set,
        )

    runs = seeded_session.execute(select(EvaluationRun)).scalars().all()
    assert len(runs) == 1
    assert runs[0].status == RunStatus.FAILED.value
    assert "matrix build exploded" in (runs[0].error_message or "")
