"""Tests for quote features computation, tick store access, matrix and evaluation (Q-113)."""

from __future__ import annotations

from datetime import date, datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from q_backend.features.compute import compute_feature
from q_backend.features.evaluation_service import run_evaluation
from q_backend.features.evidence_service import _matrix_from_segment
from q_backend.features.matrix import FeatureRequest, build_feature_matrix
from q_backend.features.registry import feature_id, get_feature_spec
from q_backend.features.sync import sync_registry_to_db
from q_backend.features.targets import TargetSpec
from q_backend.market_data.models import OHLCV
from q_backend.research.tick_store import _slug_symbol, _write_day_atomic
from q_backend.storage.db.base import Base
from q_backend.storage.db.models import EvaluationRun, FeatureScoreRow, RunStatus
from q_backend.storage.settings import get_settings


def _synthetic_bars(
    start: datetime,
    n: int = 120,
    freq: str = "5min",
) -> pd.DataFrame:
    rng = np.random.default_rng(42)
    closes = 100.0 + np.cumsum(rng.normal(0.0, 0.5, n))
    times = pd.date_range(start, periods=n, freq=freq)
    return pd.DataFrame(
        {
            "time": times,
            "open": closes - 0.2,
            "high": closes + 0.6,
            "low": closes - 0.6,
            "close": closes,
            "volume": np.full(n, 1000.0),
        }
    )


def _write_synthetic_ticks(
    tick_store_dir: Path,
    symbol: str,
    bars: pd.DataFrame,
    bid_val: float = 99.0,
    ask_val: float = 101.0,
    last_val: float = 100.0,
) -> None:
    first_time = bars["time"].iloc[0]
    day = date(first_time.year, first_time.month, first_time.day)
    times = []
    for ts in bars["time"]:
        moment = datetime(ts.year, ts.month, ts.day, ts.hour, ts.minute, tzinfo=timezone.utc)
        times.append(int(moment.timestamp() * 1000))
    count = len(times)
    arrays = {
        "time_msc": np.asarray(times, dtype=np.int64),
        "bid": np.full(count, bid_val),
        "ask": np.full(count, ask_val),
        "last": np.full(count, last_val),
        "volume": np.ones(count, dtype=np.float64),
        "flags": np.full(count, 6, dtype=np.int32),
    }
    directory = tick_store_dir / _slug_symbol(symbol)
    directory.mkdir(parents=True, exist_ok=True)
    _write_day_atomic(directory / f"{day.isoformat()}.parquet", arrays)


@pytest.fixture
def db_session() -> Session:
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    session_factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    session = session_factory()
    sync_registry_to_db(session)
    yield session
    session.close()


def test_compute_feature_without_symbol_raises_for_quote_features():
    bars = _synthetic_bars(datetime(2024, 1, 2, 9, 0, tzinfo=timezone.utc), n=10)
    for name in ("quote_spread", "spread_vs_volatility"):
        spec = get_feature_spec(name)
        with pytest.raises(ValueError, match="requires symbol"):
            compute_feature(bars, spec, {}, symbol=None)


def test_evidence_matrix_builder_passes_symbol_for_quote_features(tmp_path, monkeypatch):
    monkeypatch.setenv("Q_RESEARCH_TICK_STORE", str(tmp_path))
    bars = _synthetic_bars(datetime(2024, 1, 2, 9, 0, tzinfo=timezone.utc), n=10)
    matrix = _matrix_from_segment(
        symbol="WIN$",
        timeframe="M5",
        segment_bars=bars,
        feature_requests=[FeatureRequest("quote_spread", 1, {})],
    )
    assert not matrix.frame.empty
    col = matrix.frame.columns[0]
    assert matrix.frame[col].isna().all()


def test_build_feature_matrix_with_and_without_ticks(tmp_path, monkeypatch):
    """Spec acceptance item 3: returns Q-111 aggregate with ticks, entirely missing without."""
    monkeypatch.setenv("Q_RESEARCH_TICK_STORE", str(tmp_path))
    monkeypatch.setenv("Q_DATA_LAKE_ROOT", str(tmp_path / "lake"))
    get_settings.cache_clear()

    day1_start = datetime(2024, 1, 2, 9, 0, tzinfo=timezone.utc)
    bars_day1 = _synthetic_bars(day1_start, n=60)
    day2_start = datetime(2024, 1, 3, 9, 0, tzinfo=timezone.utc)
    bars_day2 = _synthetic_bars(day2_start, n=20)
    all_bars = pd.concat([bars_day1, bars_day2], ignore_index=True)

    _write_synthetic_ticks(tmp_path, "WIN$", bars_day1, bid_val=99.0, ask_val=101.0, last_val=100.0)

    def _mock_read_ohlcv(symbol, timeframe, start_dt, end_dt, *, service=None):
        return [
            OHLCV(
                time=pd.Timestamp(row.time).to_pydatetime(),
                open=float(row.open),
                high=float(row.high),
                low=float(row.low),
                close=float(row.close),
                tick_volume=int(row.volume),
            )
            for row in all_bars.itertuples(index=False)
            if start_dt <= row.time <= end_dt
        ]

    monkeypatch.setattr("q_backend.features.matrix.read_ohlcv_fresh", _mock_read_ohlcv)

    qs_spec = get_feature_spec("quote_spread")
    sv_spec = get_feature_spec("spread_vs_volatility")
    qs_id = feature_id(qs_spec, qs_spec.default_params)
    sv_id = feature_id(sv_spec, sv_spec.default_params)

    # 1. With ticks (day 1)
    matrix_with_ticks = build_feature_matrix(
        "WIN$",
        "M5",
        bars_day1["time"].iloc[0].to_pydatetime(),
        bars_day1["time"].iloc[-1].to_pydatetime(),
        [FeatureRequest("quote_spread", 1, {}), FeatureRequest("spread_vs_volatility", 1, {})],
        use_cache=False,
    )
    assert not matrix_with_ticks.frame[qs_id].isna().all()
    # Q-111 aggregate for (101 - 99) / 100 is 200 bps
    first_valid_qs = matrix_with_ticks.frame[qs_id].dropna().iloc[0]
    assert first_valid_qs == pytest.approx(200.0)

    # spread_vs_volatility is finite
    valid_sv = matrix_with_ticks.frame[sv_id].dropna()
    assert len(valid_sv) > 0
    assert np.isfinite(valid_sv.iloc[-1])

    # 2. Without ticks (day 2)
    matrix_no_ticks = build_feature_matrix(
        "WIN$",
        "M5",
        bars_day2["time"].iloc[0].to_pydatetime(),
        bars_day2["time"].iloc[-1].to_pydatetime(),
        [FeatureRequest("quote_spread", 1, {}), FeatureRequest("spread_vs_volatility", 1, {})],
        use_cache=False,
    )
    assert matrix_no_ticks.frame[qs_id].isna().all()
    assert matrix_no_ticks.frame[sv_id].isna().all()


def test_quote_feature_evaluation_with_and_without_ticks(db_session, tmp_path, monkeypatch):
    """Spec acceptance item 4: evaluation completes with and without ticks; leakage comes from causal check."""
    monkeypatch.setenv("Q_RESEARCH_TICK_STORE", str(tmp_path))
    monkeypatch.setenv("Q_DATA_LAKE_ROOT", str(tmp_path / "lake"))
    get_settings.cache_clear()

    day1_start = datetime(2024, 1, 2, 9, 0, tzinfo=timezone.utc)
    bars_day1 = _synthetic_bars(day1_start, n=50)

    # Write ticks for WIN$
    _write_synthetic_ticks(tmp_path, "WIN$", bars_day1)

    def _mock_read_ohlcv(symbol, timeframe, start_dt, end_dt, *, service=None):
        return [
            OHLCV(
                time=pd.Timestamp(row.time).to_pydatetime(),
                open=float(row.open),
                high=float(row.high),
                low=float(row.low),
                close=float(row.close),
                tick_volume=int(row.volume),
            )
            for row in bars_day1.itertuples(index=False)
            if start_dt <= row.time <= end_dt
        ]

    monkeypatch.setattr("q_backend.features.matrix.read_ohlcv_fresh", _mock_read_ohlcv)
    monkeypatch.setattr("q_backend.features.evaluation_service.read_ohlcv_fresh", _mock_read_ohlcv)

    from q_backend.features.targets import list_target_specs

    target = next(spec for spec in list_target_specs([5]) if spec.name == "fwd_return")
    start_dt = bars_day1["time"].iloc[0].to_pydatetime()
    end_dt = bars_day1["time"].iloc[-1].to_pydatetime()

    # 1. With ticks:
    run_with_ticks = run_evaluation(
        db_session,
        symbol="WIN$",
        timeframe="M5",
        start=start_dt,
        end=end_dt,
        target=target,
        feature_set=[FeatureRequest("quote_spread", 1, {})],
    )
    assert run_with_ticks.status == RunStatus.COMPLETED.value
    rows = db_session.query(FeatureScoreRow).filter(FeatureScoreRow.run_id == run_with_ticks.id).all()
    assert len(rows) == 1
    assert rows[0].feature_name == "quote_spread"
    assert rows[0].leakage_status == "clean"

    # 2. Without ticks (different symbol or range):
    run_no_ticks = run_evaluation(
        db_session,
        symbol="WDO$",
        timeframe="M5",
        start=start_dt,
        end=end_dt,
        target=target,
        feature_set=[FeatureRequest("quote_spread", 1, {})],
    )
    assert run_no_ticks.status == RunStatus.COMPLETED.value
    rows_no_ticks = db_session.query(FeatureScoreRow).filter(FeatureScoreRow.run_id == run_no_ticks.id).all()
    assert len(rows_no_ticks) == 1
    assert rows_no_ticks[0].feature_name == "quote_spread"
    assert rows_no_ticks[0].leakage_status in ("clean", "unverified")
