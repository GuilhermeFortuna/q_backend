"""Record a finished backtest produced outside the stack as a completed script run."""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import numpy as np
import pandas as pd
from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy.orm import Session

from q_backend.api.schemas.backtest import BacktestImportRequest
from q_backend.backtesting.chart_data import market_data_frame
from q_backend.backtesting.models import Trade, TradeStatus
from q_backend.backtesting.run_service import delete_backtest_lake_artifacts
from q_backend.market_data.timezone import mt5_datetime_to_utc_iso, to_brasilia_naive, unix_seconds_to_brasilia_naive
from q_backend.optimization.metrics import build_equity_curve
from q_backend.storage.db.models import BacktestOrigin, RunStatus
from q_backend.storage.db.repositories import (
    create_backtest_config,
    create_backtest_run,
    delete_backtest_run,
    get_or_create_strategy,
    update_backtest_run,
)
from q_backend.storage.lake.artifacts import write_backtest_artifacts, write_backtest_result

logger = logging.getLogger(__name__)

_BAR_COLUMNS = ("open", "high", "low", "close", "volume")
_RESERVED_KEYS = ("time", *_BAR_COLUMNS)


@dataclass(frozen=True)
class _ImportedResult:
    bar_times: list[datetime]
    trades: list[Trade]
    trades_frame: pd.DataFrame
    equity_frame: pd.DataFrame
    market_frame: pd.DataFrame
    bars_payload: list[dict[str, Any]]


def _invalid(message: str) -> HTTPException:
    return HTTPException(status_code=422, detail=message)


def _bar_time(value: int | str) -> datetime:
    if isinstance(value, int):
        return unix_seconds_to_brasilia_naive(value)
    return to_brasilia_naive(datetime.fromisoformat(value.replace("Z", "+00:00")))


def _validate_config(request: BacktestImportRequest) -> None:
    config = request.config
    if not config.strategy.strip():
        raise _invalid("config.strategy must not be empty")
    if config.engine != "candle":
        raise _invalid("config.engine must be 'candle' for an imported run")
    if config.ml_filter is not None:
        raise _invalid("config.ml_filter must be absent for an imported run")
    if config.entries is not None:
        raise _invalid("config.entries must be absent for an imported run")


def _validate_bars(request: BacktestImportRequest) -> list[datetime]:
    bars = request.result.bars
    if not bars:
        raise _invalid("result.bars must not be empty")
    try:
        times = [_bar_time(bar.timestamp) for bar in bars]
    except (ValueError, OverflowError, OSError) as exc:
        raise _invalid(f"result.bars timestamps must be epoch seconds or ISO-8601 times: {exc}") from exc
    if len(set(times)) != len(times):
        raise _invalid("result.bars holds a duplicate timestamp")
    if any(later <= earlier for earlier, later in zip(times, times[1:])):
        raise _invalid("result.bars must be strictly ascending by timestamp")
    return times


def _validate_indicators(request: BacktestImportRequest, bar_count: int) -> None:
    seen: set[str] = set()
    for series in request.result.indicators:
        if series.key in _RESERVED_KEYS:
            raise _invalid(f"indicator key '{series.key}' is reserved for a bar column")
        if series.key in seen:
            raise _invalid(f"duplicate indicator key '{series.key}'")
        if len(series.values) != bar_count:
            raise _invalid(f"indicator '{series.key}' must have one value per bar ({bar_count})")
        seen.add(series.key)


def _validate_trades(request: BacktestImportRequest, first: datetime, last: datetime) -> list[Trade]:
    trades: list[Trade] = []
    for index, raw in enumerate(request.result.trades):
        try:
            trade = Trade.model_validate(raw)
        except ValidationError as exc:
            raise _invalid(f"result.trades[{index}] is not a valid trade: {exc.errors()[0]['msg']}") from exc
        if trade.status != TradeStatus.CLOSED:
            raise _invalid(f"result.trades[{index}] is not closed")
        if trade.exit_time is None:
            raise _invalid(f"result.trades[{index}] lacks an exit time")
        if trade.pnl is None:
            raise _invalid(f"result.trades[{index}] lacks a pnl")
        entry_time = to_brasilia_naive(trade.entry_time)
        exit_time = to_brasilia_naive(trade.exit_time)
        if not (first <= entry_time <= last and first <= exit_time <= last):
            raise _invalid(f"result.trades[{index}] has an entry or exit time outside the bar range")
        trades.append(trade.model_copy(update={"entry_time": entry_time, "exit_time": exit_time}))
    return trades


def _build_frames(
    request: BacktestImportRequest,
    bar_times: list[datetime],
    trades: list[Trade],
) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame, list[dict[str, Any]]]:
    config = request.config
    bars = request.result.bars
    index = pd.DatetimeIndex(bar_times)

    market = pd.DataFrame(
        {
            "open": [bar.open for bar in bars],
            "high": [bar.high for bar in bars],
            "low": [bar.low for bar in bars],
            "close": [bar.close for bar in bars],
            "volume": [bar.volume for bar in bars],
        },
        index=index,
    )
    for series in request.result.indicators:
        market[series.key] = pd.Series(
            [np.nan if value is None else value for value in series.values],
            index=index,
            dtype="float64",
        )

    start = to_brasilia_naive(config.start) if config.start else bar_times[0]
    end = to_brasilia_naive(config.end) if config.end else bar_times[-1]
    equity = build_equity_curve(trades, config.initial_capital, start, end)
    equity_frame = pd.DataFrame({"time": equity.index, "equity": equity.values})
    trades_frame = pd.DataFrame([trade.model_dump() for trade in trades])
    bars_payload = [
        {
            "timestamp": mt5_datetime_to_utc_iso(bar_time),
            "open": bar.open,
            "high": bar.high,
            "low": bar.low,
            "close": bar.close,
            "volume": bar.volume,
        }
        for bar_time, bar in zip(bar_times, bars)
    ]
    return trades_frame, equity_frame, market_data_frame(market), bars_payload


def _validate(request: BacktestImportRequest) -> _ImportedResult:
    _validate_config(request)
    bar_times = _validate_bars(request)
    _validate_indicators(request, len(bar_times))
    trades = _validate_trades(request, bar_times[0], bar_times[-1])
    trades_frame, equity_frame, market_frame, bars_payload = _build_frames(request, bar_times, trades)
    return _ImportedResult(
        bar_times=bar_times,
        trades=trades,
        trades_frame=trades_frame,
        equity_frame=equity_frame,
        market_frame=market_frame,
        bars_payload=bars_payload,
    )


def import_backtest_run(session: Session, request: BacktestImportRequest) -> str:
    """Persist a validated import as a completed script run and return its run id."""
    imported = _validate(request)
    config = request.config.model_dump(mode="json")
    now = datetime.now(timezone.utc)

    get_or_create_strategy(session, name=request.config.strategy)
    bt_config = create_backtest_config(
        session,
        name=f"{request.config.symbol}-{request.config.timeframe}",
        config=config,
    )
    run = create_backtest_run(
        session,
        backtest_config_id=bt_config.id,
        config=config,
        status=RunStatus.COMPLETED.value,
        started_at=now,
        origin=BacktestOrigin.SCRIPT.value,
        provenance=request.provenance.model_dump(mode="json"),
    )
    run_id = str(run.id)

    try:
        lake_paths = write_backtest_artifacts(
            run_id, imported.trades_frame, imported.equity_frame, imported.market_frame
        )
        lake_paths["result"] = write_backtest_result(
            run_id,
            {
                "metrics": request.result.metrics,
                "trades": request.result.trades,
                "bars": imported.bars_payload,
                "indicators": [series.model_dump(mode="json") for series in request.result.indicators],
                "run_id": run_id,
            },
        )
    except Exception as exc:  # noqa: BLE001 - any lake failure must leave neither a row nor files
        logger.exception("Failed to write lake files for imported backtest %s", run_id)
        delete_backtest_run(session, run.id)
        delete_backtest_lake_artifacts(run_id)
        raise HTTPException(status_code=500, detail=f"Failed to store imported backtest run '{run_id}'.") from exc

    update_backtest_run(
        session,
        run.id,
        result_summary=request.result.metrics,
        lake_paths=lake_paths,
        finished_at=now,
    )
    return run_id
