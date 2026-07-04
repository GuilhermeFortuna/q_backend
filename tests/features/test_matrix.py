"""Tests for feature matrix builder and lake cache (WO129)."""

from __future__ import annotations

from datetime import datetime, timezone

import numpy as np
import pandas as pd
import pytest

from q_backend.features.compute import compute_feature
from q_backend.features.matrix import (
    FeatureRequest,
    build_feature_matrix,
    compute_matrix_id,
)
from q_backend.market_data.models import OHLCV
from q_backend.storage.lake.artifacts import read_feature_matrix
from q_backend.storage.settings import get_settings


def _synthetic_bars(n: int = 120) -> pd.DataFrame:
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
        filtered = [
            bar
            for bar in ohlcv
            if start_dt <= bar.time <= end_dt
        ]
        return filtered

    monkeypatch.setattr(
        "q_backend.features.matrix.read_ohlcv_fresh",
        _read_ohlcv,
    )
    return {
        "bars_df": bars_df,
        "start": start,
        "end": end,
    }


def test_build_feature_matrix_uses_read_through_seam(
    sample_market, lake_root_path, monkeypatch
):
    """WO189: matrix loads bars through read_ohlcv_fresh, not local_store."""
    calls: list[tuple[str, str, datetime, datetime]] = []

    def _spy(symbol, timeframe, start_dt, end_dt, *, service=None):
        calls.append((symbol, timeframe, start_dt, end_dt))
        return sample_market["bars_df"].pipe(
            lambda df: [
                OHLCV(
                    time=row.time.to_pydatetime(),
                    open=float(row.open),
                    high=float(row.high),
                    low=float(row.low),
                    close=float(row.close),
                    tick_volume=int(row.volume),
                )
                for row in df.itertuples(index=False)
                if start_dt <= row.time.to_pydatetime() <= end_dt
            ]
        )

    monkeypatch.setattr(
        "q_backend.features.matrix.read_ohlcv_fresh",
        _spy,
    )
    build_feature_matrix(
        "EURUSD",
        "H1",
        sample_market["start"],
        sample_market["end"],
        [FeatureRequest("rsi", None, {"period": 14})],
        use_cache=False,
    )
    assert calls == [
        ("EURUSD", "H1", sample_market["start"], sample_market["end"])
    ]


def test_build_feature_matrix_two_features_valid_from(sample_market, lake_root_path):
    features = [
        FeatureRequest("rsi", None, {"period": 14}),
        FeatureRequest("ma", None, {"period": 20, "ma_type": "sma"}),
    ]
    result = build_feature_matrix(
        "EURUSD",
        "H1",
        sample_market["start"],
        sample_market["end"],
        features,
        use_cache=False,
    )

    assert len(result.frame.columns) == 2
    pd.testing.assert_index_equal(
        result.frame.index, pd.DatetimeIndex(sample_market["bars_df"]["time"])
    )
    assert result.manifest["bar_count"] == len(sample_market["bars_df"])
    assert len(result.manifest["features"]) == 2

    union_warmup = max(row["warmup_bars"] for row in result.manifest["features"])
    assert union_warmup == 20
    valid_from = pd.Timestamp(result.manifest["valid_from"])
    expected = sample_market["bars_df"]["time"].iloc[union_warmup]
    assert valid_from == expected


def test_build_feature_matrix_cache_hit_skips_compute(
    sample_market, lake_root_path, monkeypatch
):
    features = [
        FeatureRequest("rsi", None, {"period": 14}),
        FeatureRequest("atr", None, {"period": 14}),
    ]
    calls = {"count": 0}
    original = compute_feature

    def counting_compute(*args, **kwargs):
        calls["count"] += 1
        return original(*args, **kwargs)

    monkeypatch.setattr("q_backend.features.matrix.compute_feature", counting_compute)

    first = build_feature_matrix(
        "EURUSD",
        "H1",
        sample_market["start"],
        sample_market["end"],
        features,
        use_cache=False,
    )
    assert calls["count"] == 2

    calls["count"] = 0
    second = build_feature_matrix(
        "EURUSD",
        "H1",
        sample_market["start"],
        sample_market["end"],
        features,
        use_cache=True,
    )

    assert calls["count"] == 0
    assert second.matrix_id == first.matrix_id
    pd.testing.assert_frame_equal(second.frame, first.frame)


def test_matrix_id_changes_when_params_change(sample_market):
    base = [
        FeatureRequest("rsi", None, {"period": 14}),
        FeatureRequest("ma", None, {"period": 20, "ma_type": "sma"}),
    ]
    changed = [
        FeatureRequest("rsi", None, {"period": 21}),
        FeatureRequest("ma", None, {"period": 20, "ma_type": "sma"}),
    ]
    start = sample_market["start"]
    end = sample_market["end"]

    first_id = compute_matrix_id("EURUSD", "H1", start, end, base)
    second_id = compute_matrix_id("EURUSD", "H1", start, end, changed)
    assert first_id != second_id


def test_matrix_id_changes_when_engine_version_changes(sample_market, monkeypatch):
    features = [FeatureRequest("rsi", None, {"period": 14})]
    start = sample_market["start"]
    end = sample_market["end"]
    id_v1 = compute_matrix_id("EURUSD", "H1", start, end, features)

    monkeypatch.setattr("q_backend.features.matrix.ENGINE_VERSION", 2)
    id_v2 = compute_matrix_id("EURUSD", "H1", start, end, features)
    assert id_v1 != id_v2


def test_manifest_round_trips_through_lake(sample_market, lake_root_path):
    features = [
        FeatureRequest("rsi", None, {"period": 14}),
        FeatureRequest("realized_vol", None, {"window": 21}),
    ]
    built = build_feature_matrix(
        "EURUSD",
        "H1",
        sample_market["start"],
        sample_market["end"],
        features,
        use_cache=False,
    )

    loaded = read_feature_matrix(built.matrix_id)
    assert loaded.manifest == built.manifest
    pd.testing.assert_frame_equal(loaded.frame, built.frame)
