"""Deployment chart endpoint: parity, payload shape, caching, and degradation."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import MagicMock, patch

import numpy as np
import pandas as pd
import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

import q_backend.backtesting.strategies  # noqa: F401  (register strategies)
from q_backend.api.schemas.execution import (
    DeploymentCreateRequest,
    DeploymentIdentityInput,
    PaperAccountCreateRequest,
)
from q_backend.api.services import execution as execution_service
from q_backend.api.services import execution_chart as chart_service
from q_backend.backtesting.chart_data import serialize_chart_data
from q_backend.backtesting.factory import build_strategy
from q_backend.execution.domain import StrategyIdentity
from q_backend.execution.evaluator import StrategyEvaluator
from q_backend.execution.indicator_frame import augment_indicator_frame
from q_backend.execution.strategy_build import build_strategy_from_compiled
from q_backend.execution.validation import compute_config_hash
from q_backend.market_data.models import OHLCV
from q_backend.storage.db import execution_models  # noqa: F401
from q_backend.storage.db import models  # noqa: F401
from q_backend.storage.db.base import Base

_STRATEGY_PARAMS = {"short_period": 5, "long_period": 20, "threshold": 0.0}


@pytest.fixture
def api_db_session() -> Session:
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    session = sessionmaker(
        bind=engine, autoflush=False, autocommit=False, expire_on_commit=False
    )()
    try:
        yield session
        session.commit()
    finally:
        session.close()
        Base.metadata.drop_all(engine)
        engine.dispose()


def _compiled(symbol: str = "WIN$", timeframe: str = "H1") -> dict:
    return {
        "strategy": "MACrossover",
        "strategy_params": dict(_STRATEGY_PARAMS),
        "symbol": symbol,
        "timeframe": timeframe,
    }


def _synthetic_frame(n: int = 130, *, freq: str = "h") -> pd.DataFrame:
    rng = np.random.default_rng(20240609)
    steps = rng.normal(0.0, 1.0, size=n)
    close = 100.0 + np.cumsum(steps)
    open_ = close - rng.normal(0.0, 0.5, size=n)
    high = np.maximum(open_, close) + np.abs(rng.normal(0.0, 0.5, size=n))
    low = np.minimum(open_, close) - np.abs(rng.normal(0.0, 0.5, size=n))
    volume = rng.integers(1_000, 5_000, size=n).astype(float)
    index = pd.date_range("2023-01-01", periods=n, freq=freq, tz="UTC")
    return pd.DataFrame(
        {"open": open_, "high": high, "low": low, "close": close, "volume": volume},
        index=index,
    )


def _frame_to_ohlcv(frame: pd.DataFrame) -> list[OHLCV]:
    return [
        OHLCV(
            time=ts.to_pydatetime(),
            open=float(row["open"]),
            high=float(row["high"]),
            low=float(row["low"]),
            close=float(row["close"]),
            tick_volume=int(row["volume"]),
        )
        for ts, row in frame.iterrows()
    ]


def _make_deployment(session: Session):
    account = execution_service.create_account(
        session,
        PaperAccountCreateRequest(name="chart-desk", initial_balance=Decimal("100000")),
    )
    compiled = _compiled()
    identity = DeploymentIdentityInput(
        strategy_name="MACrossover",
        strategy_version=1,
        compiled_config=compiled,
        config_hash=compute_config_hash(compiled),
        symbol="WIN$",
        timeframe="H1",
        sizing_config={"type": "fixed_quantity", "quantity": 1.0},
        risk_config={},
    )
    return execution_service.create_deployment(
        session,
        DeploymentCreateRequest(
            paper_account_id=account.id,
            name="chart-dep",
            identity=identity,
        ),
    )


def test_endpoint_indicator_parity_with_evaluator(api_db_session: Session):
    """Endpoint indicator values equal what the StrategyEvaluator sees per bar."""
    deployment = _make_deployment(api_db_session)
    compiled = _compiled()
    frame = _synthetic_frame(130)
    strategy = build_strategy_from_compiled(compiled, symbol="WIN$")

    bars = 30
    with patch.object(
        chart_service, "fetch_ohlcv_rows", return_value=_frame_to_ohlcv(frame)
    ):
        payload = chart_service.get_deployment_chart(
            api_db_session, MagicMock(), deployment.id, bars=bars
        )

    evaluator = StrategyEvaluator(
        deployment_id="dep-parity",
        identity=StrategyIdentity(
            strategy_name="MACrossover",
            strategy_version=1,
            compiled_config=compiled,
            config_hash="hash",
            symbol="WIN$",
            timeframe="H1",
            sizing_config={"type": "fixed_quantity", "quantity": 1.0},
        ),
    )
    evaluator.seed_window(frame)
    evaluator_aug = evaluator._augmented_frame()

    # Both compute through augment_indicator_frame; on the fully-warmed display
    # tail the endpoint and the evaluator agree bar-for-bar.
    payload_by_key = {ind.key: ind.values for ind in payload.indicators}
    for spec in strategy.get_chart_indicators():
        expected = evaluator_aug[spec.key].to_numpy()[-bars:]
        actual = np.array(payload_by_key[spec.key], dtype=float)
        np.testing.assert_allclose(actual, expected, rtol=1e-9, atol=1e-9)


def test_warmup_nans_serialize_as_null():
    compiled = _compiled()
    frame = _synthetic_frame(30)
    strategy = build_strategy(compiled["strategy"], compiled["strategy_params"], "WIN$")
    augmented = augment_indicator_frame(strategy, frame)

    payload = serialize_chart_data(augmented, strategy)
    ma_long = next(ind for ind in payload["indicators"] if ind["key"] == "ma_long")
    assert ma_long["values"][0] is None  # long-period warm-up not yet satisfied
    assert ma_long["values"][-1] is not None


def test_chart_payload_shape_and_trim(api_db_session: Session):
    deployment = _make_deployment(api_db_session)
    frame = _synthetic_frame(130)
    strategy = build_strategy_from_compiled(_compiled(), symbol="WIN$")

    with patch.object(
        chart_service, "fetch_ohlcv_rows", return_value=_frame_to_ohlcv(frame)
    ):
        payload = chart_service.get_deployment_chart(
            api_db_session, MagicMock(), deployment.id, bars=30
        )

    assert payload.symbol == "WIN$"
    assert payload.timeframe == "H1"
    assert payload.window_bound_bars == 65
    assert len(payload.bars) == 30

    specs = strategy.get_chart_indicators()
    assert {ind.key for ind in payload.indicators} == {spec.key for spec in specs}
    by_key = {spec.key: spec for spec in specs}
    for ind in payload.indicators:
        assert ind.pane == by_key[ind.key].pane
        assert ind.color == by_key[ind.key].color
        assert len(ind.values) == 30
        assert all(value is not None for value in ind.values)  # fully warmed

    panes = {ind.pane for ind in payload.indicators}
    assert {"price", "oscillator"}.issubset(panes)

    last_close = payload.last_bar_close_time
    next_close = payload.next_bar_close_time
    assert next_close - last_close == timedelta(hours=1)


def test_chart_cache_recomputes_only_on_new_bar(api_db_session: Session):
    deployment = _make_deployment(api_db_session)
    frame = _synthetic_frame(130)
    rows = {"value": _frame_to_ohlcv(frame)}

    spy = MagicMock(side_effect=augment_indicator_frame)

    def _fetch(*_args, **_kwargs):
        return rows["value"]

    with patch.object(chart_service, "fetch_ohlcv_rows", side_effect=_fetch), patch.object(
        chart_service, "augment_indicator_frame", spy
    ):
        chart_service.get_deployment_chart(
            api_db_session, MagicMock(), deployment.id, bars=30
        )
        chart_service.get_deployment_chart(
            api_db_session, MagicMock(), deployment.id, bars=30
        )
        assert spy.call_count == 1  # unchanged last bar → served from cache

        extended = _synthetic_frame(131)
        rows["value"] = _frame_to_ohlcv(extended)
        chart_service.get_deployment_chart(
            api_db_session, MagicMock(), deployment.id, bars=30
        )
        assert spy.call_count == 2  # new completed bar invalidates the cache


def test_chart_unknown_deployment_returns_404(api_db_session: Session):
    with pytest.raises(HTTPException) as exc:
        chart_service.get_deployment_chart(
            api_db_session, MagicMock(), uuid.uuid4(), bars=30
        )
    assert exc.value.status_code == 404


def test_chart_market_data_empty_returns_503(api_db_session: Session):
    deployment = _make_deployment(api_db_session)
    with patch.object(chart_service, "fetch_ohlcv_rows", return_value=[]):
        with pytest.raises(HTTPException) as exc:
            chart_service.get_deployment_chart(
                api_db_session, MagicMock(), deployment.id, bars=30
            )
    assert exc.value.status_code == 503


def test_chart_market_data_provider_error_returns_503(api_db_session: Session):
    deployment = _make_deployment(api_db_session)
    with patch.object(
        chart_service,
        "fetch_ohlcv_rows",
        side_effect=ConnectionError("MetaTrader 5 terminal is offline."),
    ):
        with pytest.raises(HTTPException) as exc:
            chart_service.get_deployment_chart(
                api_db_session, MagicMock(), deployment.id, bars=30
            )
    assert exc.value.status_code == 503


def test_chart_endpoint_registered_in_openapi():
    from q_backend.api.main import app

    paths = {route.path for route in app.routes if hasattr(route, "path")}
    assert "/api/v1/execution/deployments/{deployment_id}/chart" in paths
