"""Shared helpers for async backtest API tests."""

from __future__ import annotations

from contextlib import ExitStack
from typing import Any
from unittest.mock import MagicMock, patch

from q_backend.api.backtest_jobs import BacktestJobRequest
from q_backend.api.routers.backtest import get_backtest_result, start_backtest


def mock_worker_market_service(
    *,
    ohlcv: list | None = None,
    ticks_columnar: dict[str, Any] | None = None,
    active_provider: str = "mt5",
) -> MagicMock:
    mock = MagicMock()
    if ohlcv is not None:
        mock.get_ohlcv.return_value = ohlcv
    if ticks_columnar is not None:
        mock.get_ticks_columnar.return_value = ticks_columnar
    mock.active_provider.return_value = active_provider
    return mock


def run_async_backtest(
    body: dict[str, Any],
    *,
    api_session_scope,
    sample_ohlcv: list | None = None,
    ticks_columnar: dict[str, Any] | None = None,
    active_provider: str = "mt5",
    extra_patches: list | None = None,
) -> tuple[str, dict[str, Any]]:
    """Start an async backtest (sync harness) and return (run_id, result payload)."""
    request = BacktestJobRequest.model_validate(body)
    mock_service = mock_worker_market_service(
        ohlcv=sample_ohlcv,
        ticks_columnar=ticks_columnar,
        active_provider=active_provider,
    )
    with ExitStack() as stack:
        stack.enter_context(
            patch(
                "q_backend.tasks.worker_context.get_worker_market_data_service",
                return_value=mock_service,
            )
        )
        stack.enter_context(patch("q_backend.api.backtest_jobs.session_scope", api_session_scope))
        for extra in extra_patches or []:
            stack.enter_context(extra)
        start_resp = start_backtest(request)

    run_id = start_resp["run_id"]
    return run_id, get_backtest_result(run_id)
