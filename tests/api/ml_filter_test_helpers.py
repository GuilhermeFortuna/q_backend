"""Shared fixtures for the ML-filter backtest API tests (Q-087)."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from typing import Any
from unittest.mock import patch

import numpy as np
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from q_backend.api.backtest_jobs import BacktestJobRequest
from q_backend.market_data.models import OHLCV
from q_backend.ml_filters.compatibility import compatibility_fingerprint, source_equivalent_config
from q_backend.storage.db.base import Base

MODEL_ID = "a" * 64
DATASET_ID = "b" * 64
# Swings that yield four MA(2/4) crossover candidates, at bars 6, 11, 17 and 22.
CLOSES = [
    100.0, 99.0, 98.0, 97.0, 96.0, 97.5, 99.5, 101.5, 103.5, 105.5, 104.0, 102.0, 100.0, 98.0, 96.0,
    94.0, 95.5, 97.5, 99.5, 101.5, 103.5, 102.0, 100.0, 98.0, 96.0, 94.0, 92.0,
]  # fmt: skip
REJECTED_CLOSE = CLOSES[11]
BODY: dict[str, Any] = {
    "symbol": "WIN$",
    "timeframe": "D1",
    "start": "2024-01-01T00:00:00Z",
    "end": "2024-03-01T00:00:00Z",
    "point_value": 0.2,
    "strategy": "MACrossoverMLFilter",
    "strategy_params": {"short_period": 2, "long_period": 4},
    "ml_filter": {"model_version_id": MODEL_ID, "threshold": 0.5},
}


class FakeClassifier:
    """Scores 0.1 for the listed signal-bar closes and 0.9 for every other candidate."""

    feature_names = ("close", "side")

    def __init__(self, reject_closes: tuple[float, ...] = ()) -> None:
        self.reject_closes = tuple(round(value, 4) for value in reject_closes)

    def predict_good_entry_probability(self, X):
        return np.array([0.1 if round(float(close), 4) in self.reject_closes else 0.9 for close in X["close"]])


def manifest_for(body: dict[str, Any], *, train_end: str = "2023-12-01T00:00:00+00:00") -> dict[str, Any]:
    config = BacktestJobRequest.model_validate(body).model_dump(mode="json")
    return {
        "model_version_id": MODEL_ID,
        "dataset_id": DATASET_ID,
        "algorithm": "logistic_regression",
        "train_end": train_end,
        "compatibility_fingerprint": compatibility_fingerprint(source_equivalent_config(config)),
    }


def model_patches(body: dict[str, Any], classifier: FakeClassifier) -> list:
    manifest = manifest_for(body)
    return [
        patch("q_backend.ml_filters.service.read_filter_manifest", return_value=manifest),
        patch("q_backend.ml_filters.service.resolve_filter_model", return_value=(classifier, manifest)),
    ]


def build_ohlcv() -> list[OHLCV]:
    base = datetime(2024, 1, 2, 10, 0, tzinfo=timezone.utc)
    previous = CLOSES[0]
    bars = []
    for index, close in enumerate(CLOSES):
        bars.append(
            OHLCV(
                time=base + timedelta(days=index),
                open=previous,
                high=max(previous, close) + 0.5,
                low=min(previous, close) - 0.5,
                close=close,
                tick_volume=100,
            )
        )
        previous = close
    return bars


@contextmanager
def sqlite_session_factory():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    try:
        yield sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False)
    finally:
        engine.dispose()


def scope_for(factory):
    @contextmanager
    def scope():
        session = factory()
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    return scope
