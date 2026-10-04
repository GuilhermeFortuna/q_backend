from __future__ import annotations

import pandas as pd
import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from q_backend.api.routers import ml_filters as router
from q_backend.api.schemas.ml_filters import ComparisonRequest, TrainingRequest
from q_backend.storage.db.base import Base
from q_backend.storage.db.models import BacktestConfig, BacktestRun


@pytest.fixture
def api_db_engine():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    yield engine
    Base.metadata.drop_all(engine)
    engine.dispose()


@pytest.fixture
def api_db_session(api_db_engine):
    factory = sessionmaker(bind=api_db_engine, expire_on_commit=False)
    session = factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def test_training_and_comparison_contract_validation():
    with pytest.raises(ValueError, match="timezone-aware"):
        TrainingRequest(
            source_run_id="source",
            train_end="2026-01-01T00:00:00",
            validation_end="2026-02-01T00:00:00Z",
            selected_features=["close", "side"],
            algorithms=["random_forest"],
        )
    with pytest.raises(ValueError, match="duplicates"):
        ComparisonRequest(dataset_id="dataset", model_version_ids=["model", "model"])


def test_source_api_reads_frozen_lake_without_market_provider(api_db_session: Session, monkeypatch):
    from q_backend.api import ml_filter_jobs
    from q_backend.market_data.service import MarketDataService

    config = {
        "symbol": "TEST",
        "timeframe": "M5",
        "start": "2026-01-01T00:00:00Z",
        "end": "2026-01-01T00:20:00Z",
        "strategy": "MACrossover",
        "strategy_params": {
            "short_period": 2,
            "long_period": 3,
            "short_ma_type": "sma",
            "long_ma_type": "sma",
            "threshold": 0.0,
        },
        "entry_manager": {"kind": "or", "params": {}},
        "engine": "candle",
    }
    backtest_config = BacktestConfig(name="source", config=config)
    api_db_session.add(backtest_config)
    api_db_session.flush()
    run = BacktestRun(backtest_config_id=backtest_config.id, config=config, status="completed")
    api_db_session.add(run)
    api_db_session.flush()
    times = pd.date_range("2026-01-01T00:00:00Z", periods=4, freq="5min")
    bars = pd.DataFrame(
        {
            "time": times,
            "open": [1, 2, 3, 4],
            "high": [2, 3, 4, 5],
            "low": [0, 1, 2, 3],
            "close": [1, 2, 3, 4],
            "tick_volume": [10, 10, 10, 10],
            "e0__ma_short": [1, 2, 3, 4],
            "e0__ma_long": [0, 1, 2, 3],
            "e0__delta": [1, 1, 1, 1],
            "e0__prev_delta": [0, 0, 1, 1],
        }
    )
    trades = pd.DataFrame([{"entry_time": times[1], "exit_time": times[2], "action": "BUY", "pnl": 2.0}])

    monkeypatch.setattr(
        "q_backend.ml_filters.source.read_backtest_artifact",
        lambda _run_id, kind: bars if kind == "market_data" else trades,
    )
    monkeypatch.setattr(
        MarketDataService,
        "get_ohlcv",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("provider read is forbidden")),
    )
    response = router.list_ml_filter_sources(limit=50, offset=0, session=api_db_session)
    assert response["total"] == 1
    source = response["items"][0]
    assert source["run_id"] == str(run.id)
    assert source["eligible"] is True
    assert source["source_sample_count"] == 1
    assert "real_volume" not in source["available_features"]


def test_start_training_maps_source_eligibility_to_visible_422(monkeypatch):
    body = TrainingRequest(
        source_run_id="source",
        train_end="2026-01-01T00:00:00Z",
        validation_end="2026-02-01T00:00:00Z",
        selected_features=["close", "side"],
        algorithms=["random_forest"],
    )
    monkeypatch.setattr(
        "q_backend.api.ml_filter_jobs.get_source_summary_by_id",
        lambda _source_id: {"eligible": False, "eligibility_reason": "Frozen trades artifact is missing"},
    )
    with pytest.raises(HTTPException) as error:
        router.start_ml_filter_training(body)
    assert error.value.status_code == 422
    assert error.value.detail["code"] == "incompatible_source"


def test_model_detail_never_returns_serialized_pipeline(api_db_session: Session, monkeypatch):
    from q_backend.api.routers import ml_filters as router_module

    config = BacktestConfig(name="source", config={})
    api_db_session.add(config)
    api_db_session.flush()
    row = __import__("q_backend.storage.db.models", fromlist=["MLFilterModelVersion"]).MLFilterModelVersion(
        model_version_id="a" * 64,
        dataset_id="b" * 64,
        source_run_id="c" * 36,
        algorithm="random_forest",
        status="ready",
        manifest_path="manifest.json",
        artifact_path="model.joblib",
        summary={"validation_metrics": {"roc_auc": 0.7}},
    )
    api_db_session.add(row)
    api_db_session.flush()
    monkeypatch.setattr(
        router_module,
        "read_model_manifest",
        lambda _model_id: {
            "selected_features": [{"name": "close"}, {"name": "side"}],
            "dependency_versions": {"scikit-learn": "1.9.0"},
            "hyperparameters": {"n_estimators": 5},
            "seed": 42,
        },
    )
    monkeypatch.setattr(router_module, "load_model_version", lambda _model_id: object())

    response = router.get_ml_filter_model("a" * 64, session=api_db_session)

    assert response["selected_features"] == ["close", "side"]
    assert "pipeline" not in response
    assert response["validation_metrics"]["roc_auc"] == 0.7
