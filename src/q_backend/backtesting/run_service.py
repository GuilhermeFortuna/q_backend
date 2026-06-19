"""Synchronous backtest execution, persistence, and lake artifact helpers."""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Literal, Optional

import pandas as pd
from fastapi import HTTPException
from sqlalchemy.orm import Session

from q_backend.api.dependencies import market_data_service
from q_backend.api.schemas.backtest import (
    BacktestRequest,
    BacktestRunDetailResponse,
    BacktestRunListItem,
    BacktestRunPatchRequest,
)
from q_backend.api.schemas.common import BulkDeleteBacktestsRequest
from q_backend.backtesting.chart_data import serialize_chart_data
from q_backend.backtesting.engine import BacktestEngine, ParallelMode
from q_backend.backtesting.factory import build_strategy
from q_backend.backtesting.position_sizing import (
    FixedQuantityPositionSizing,
    build_position_sizer,
)
from q_backend.backtesting.tick.chart_data import serialize_tick_chart_data
from q_backend.backtesting.tick.engine import TickBacktestEngine
from q_backend.backtesting.tick.factory import build_tick_strategy
from q_backend.backtesting.tick.strategy import TickArrays
from q_backend.market_data.clients.metatrader import (
    _to_naive_local,
    resolve_copy_ticks_flags,
)
from q_backend.market_data.timezone import mt5_datetime_to_utc_iso
from q_backend.optimization.metrics import build_equity_curve
from q_backend.storage.db.engine import session_scope
from q_backend.storage.db.models import BacktestRun, RunStatus
from q_backend.storage.db.repositories import (
    create_backtest_config,
    create_backtest_run,
    delete_backtest_run,
    delete_backtest_runs,
    find_backtest_run_by_config,
    get_backtest_run,
    get_or_create_strategy,
    list_backtest_runs,
    update_backtest_run,
)
from q_backend.storage.lake import (
    delete_backtest_artifacts,
    read_backtest_artifact,
    write_backtest_artifacts,
)
logger = logging.getLogger(__name__)


def backtest_run_fields(config: Dict[str, Any]) -> tuple[str, str, str]:
    return (
        config.get("symbol", ""),
        config.get("strategy", ""),
        config.get("timeframe", "D1"),
    )


def backtest_run_list_item(run: BacktestRun) -> BacktestRunListItem:
    symbol, strategy, timeframe = backtest_run_fields(run.config or {})
    return BacktestRunListItem(
        run_id=str(run.id),
        symbol=symbol,
        strategy=strategy,
        timeframe=timeframe,
        status=run.status,
        created_at=run.created_at,
        is_saved=run.is_saved,
        summary=run.result_summary,
    )


def backtest_run_detail(run: BacktestRun) -> BacktestRunDetailResponse:
    config = run.config or {}
    symbol, strategy, timeframe = backtest_run_fields(config)
    return BacktestRunDetailResponse(
        run_id=str(run.id),
        symbol=symbol,
        strategy=strategy,
        timeframe=timeframe,
        status=run.status,
        config=config,
        result_summary=run.result_summary,
        error_message=run.error_message,
        started_at=run.started_at,
        finished_at=run.finished_at,
        created_at=run.created_at,
        is_saved=run.is_saved,
    )


def backtest_request_config(request: BacktestRequest) -> Dict[str, Any]:
    config_dict = request.model_dump(mode="json")
    if request.engine == "tick":
        config_dict["timeframe"] = "TICK"
    return config_dict


def resolve_tick_flags(tick_flags: Optional[str]) -> int:
    try:
        return resolve_copy_ticks_flags(tick_flags)
    except ValueError as exc:
        raise ValueError(str(exc)) from exc


def columnar_to_tick_arrays(columnar: Dict[str, Any]) -> TickArrays:
    return TickArrays(
        time_msc=columnar["time_msc"],
        bid=columnar["bid"],
        ask=columnar["ask"],
        last=columnar["last"],
        volume=columnar["volume"],
    )


def build_equity_dataframe(
    closed_trades: list,
    initial_capital: float,
    start: datetime,
    end: datetime,
) -> pd.DataFrame:
    equity_series = build_equity_curve(closed_trades, initial_capital, start, end)
    return pd.DataFrame({"time": equity_series.index, "equity": equity_series.values})


def write_backtest_lake_artifacts(
    run_id: str,
    closed_trades: list[Dict[str, Any]],
    equity_curve: pd.DataFrame,
) -> Optional[Dict[str, str]]:
    try:
        trades_df = pd.DataFrame(closed_trades)
        return write_backtest_artifacts(run_id, trades_df, equity_curve)
    except Exception as exc:
        logger.warning("Failed to write backtest lake artifacts: %s", exc)
        return None


def finish_backtest_run(
    run_id: Optional[str],
    *,
    status: str,
    result_summary: Optional[Dict[str, Any]] = None,
    error_message: Optional[str] = None,
    lake_paths: Optional[Dict[str, str]] = None,
) -> None:
    if run_id is None:
        return
    try:
        with session_scope() as session:
            update_backtest_run(
                session,
                uuid.UUID(run_id),
                status=status,
                result_summary=result_summary,
                error_message=error_message,
                lake_paths=lake_paths,
                finished_at=datetime.now(timezone.utc),
            )
    except Exception as exc:
        logger.warning("Failed to persist backtest run finish: %s", exc)


def start_backtest_run(request: BacktestRequest) -> Optional[str]:
    try:
        config_dict = backtest_request_config(request)
        persisted_timeframe = config_dict.get("timeframe", request.timeframe)
        now = datetime.now(timezone.utc)
        with session_scope() as session:
            get_or_create_strategy(session, name=request.strategy)
            existing = find_backtest_run_by_config(session, config_dict)
            if existing is not None:
                update_backtest_run(
                    session,
                    existing.id,
                    status=RunStatus.RUNNING.value,
                    started_at=now,
                    clear_error_message=True,
                )
                return str(existing.id)

            bt_config = create_backtest_config(
                session,
                name=f"{request.symbol}-{persisted_timeframe}",
                config=config_dict,
            )
            run = create_backtest_run(
                session,
                backtest_config_id=bt_config.id,
                config=config_dict,
                status=RunStatus.RUNNING.value,
                started_at=now,
            )
            return str(run.id)
    except Exception as exc:
        logger.warning("Failed to persist backtest run start: %s", exc)
        return None


def artifact_datetime_to_iso(value: Any) -> str:
    if isinstance(value, pd.Timestamp):
        value = value.to_pydatetime()
    if isinstance(value, datetime):
        return mt5_datetime_to_utc_iso(value)
    return str(value)


def serialize_trades_artifact(df: pd.DataFrame) -> List[Dict[str, Any]]:
    records: List[Dict[str, Any]] = []
    for record in df.to_dict(orient="records"):
        serialized: Dict[str, Any] = {}
        for key, value in record.items():
            if value is None or (isinstance(value, float) and pd.isna(value)):
                serialized[key] = None
            elif isinstance(value, (pd.Timestamp, datetime)):
                serialized[key] = artifact_datetime_to_iso(value)
            else:
                serialized[key] = value
        records.append(serialized)
    return records


def serialize_equity_artifact(df: pd.DataFrame) -> List[Dict[str, Any]]:
    time_col = "time" if "time" in df.columns else df.columns[0]
    equity_col = "equity" if "equity" in df.columns else df.columns[1]
    points: List[Dict[str, Any]] = []
    for _, row in df.iterrows():
        points.append(
            {
                "time": artifact_datetime_to_iso(row[time_col]),
                "equity": float(row[equity_col]),
            }
        )
    return points


def delete_backtest_lake_artifacts(run_id: str) -> None:
    try:
        delete_backtest_artifacts(run_id)
    except Exception as exc:
        logger.warning("Failed to delete backtest lake artifacts for %s: %s", run_id, exc)


def run_tick_backtest(
    request: BacktestRequest,
    start: datetime,
    end: datetime,
    run_id: Optional[str],
) -> Dict[str, Any]:
    try:
        tick_flags = resolve_tick_flags(request.tick_flags)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    try:
        arrays = market_data_service.get_ticks_columnar(
            request.symbol, start, end, flags=tick_flags
        )
    except ConnectionError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc

    if len(arrays["time_msc"]) == 0:
        if market_data_service.active_provider() == "local":
            raise HTTPException(
                status_code=404,
                detail=(
                    "No local tick data for the given parameters. "
                    "Ingest ticks via Storage API "
                    '(POST /api/v1/storage/ingest with kind="ticks").'
                ),
            )
        raise HTTPException(
            status_code=404,
            detail="No tick data found for the given parameters.",
        )

    ticks = columnar_to_tick_arrays(arrays)

    try:
        strategy = build_tick_strategy(
            request.strategy, request.strategy_params, request.symbol
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    try:
        chart_data = serialize_tick_chart_data(
            ticks, strategy, display_timeframe=request.display_timeframe
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    sizing_config = request.position_sizing or FixedQuantityPositionSizing()

    engine = TickBacktestEngine(
        strategy=strategy,
        sizing_config=sizing_config,
        initial_capital=request.initial_capital,
        point_value=request.point_value,
        symbol=request.symbol,
    )
    registry = engine.run(ticks, parallel_mode=ParallelMode.DAY_TRADE)

    metrics = registry.get_performance_metrics(request.initial_capital)
    closed_trade_objects = registry.get_closed_trades()
    closed_trades = [t.model_dump() for t in closed_trade_objects]

    lake_paths = None
    if run_id is not None:
        equity_df = build_equity_dataframe(
            closed_trade_objects,
            request.initial_capital,
            start,
            end,
        )
        lake_paths = write_backtest_lake_artifacts(run_id, closed_trades, equity_df)

    finish_backtest_run(
        run_id,
        status=RunStatus.COMPLETED.value,
        result_summary=metrics,
        lake_paths=lake_paths,
    )

    return {
        "metrics": metrics,
        "trades": closed_trades,
        "bars": chart_data["bars"],
        "indicators": chart_data["indicators"],
        "run_id": run_id,
    }


def run_sync(request: BacktestRequest) -> Dict[str, Any]:
    """Run a candle or tick backtest synchronously and return the chart payload."""
    start = request.start or (datetime.now() - timedelta(days=365))
    end = request.end or datetime.now()

    start = _to_naive_local(start)
    end = _to_naive_local(end)

    if start >= end:
        raise HTTPException(
            status_code=400, detail="Start datetime must be before end datetime."
        )

    run_id = start_backtest_run(request)

    try:
        if request.engine == "tick":
            return run_tick_backtest(request, start, end, run_id)

        ohlcv_data = market_data_service.get_ohlcv(
            request.symbol, request.timeframe, start, end
        )
        if not ohlcv_data:
            raise HTTPException(
                status_code=404, detail="No market data found for the given parameters."
            )

        df = pd.DataFrame([b.model_dump() for b in ohlcv_data])
        df.set_index("time", inplace=True)
        df.index = pd.to_datetime(df.index, format="ISO8601")

        try:
            strategy = build_strategy(
                request.strategy, request.strategy_params, request.symbol
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        df_with_indicators = strategy.compute_indicators(df.copy())
        chart_data = serialize_chart_data(df_with_indicators, strategy)

        sizer = build_position_sizer(
            request.position_sizing, point_value=request.point_value
        )

        engine = BacktestEngine(
            strategy,
            sizer,
            initial_capital=request.initial_capital,
            point_values={request.symbol: request.point_value},
            day_trade=request.day_trade,
            day_trade_start_time=request.day_trade_start_time,
            day_trade_end_time=request.day_trade_end_time,
            day_trade_close_time=request.day_trade_close_time,
            costs=request.costs,
        )
        registry = engine.run(df, parallel_mode=ParallelMode.SEQUENTIAL)

        metrics = registry.get_performance_metrics(request.initial_capital)
        closed_trade_objects = registry.get_closed_trades()
        closed_trades = [t.model_dump() for t in closed_trade_objects]

        lake_paths = None
        if run_id is not None:
            equity_df = build_equity_dataframe(
                closed_trade_objects,
                request.initial_capital,
                start,
                end,
            )
            lake_paths = write_backtest_lake_artifacts(run_id, closed_trades, equity_df)

        finish_backtest_run(
            run_id,
            status=RunStatus.COMPLETED.value,
            result_summary=metrics,
            lake_paths=lake_paths,
        )

        return {
            "metrics": metrics,
            "trades": closed_trades,
            "bars": chart_data["bars"],
            "indicators": chart_data["indicators"],
            "run_id": run_id,
        }

    except HTTPException as exc:
        finish_backtest_run(
            run_id,
            status=RunStatus.FAILED.value,
            error_message=str(exc.detail),
        )
        raise
    except Exception as exc:
        finish_backtest_run(
            run_id,
            status=RunStatus.FAILED.value,
            error_message=str(exc),
        )
        logger.error("Error running backtest: %s", exc)
        raise HTTPException(status_code=500, detail=str(exc)) from exc


def list_runs(
    session: Session,
    *,
    limit: int,
    offset: int,
    symbol: Optional[str] = None,
    strategy: Optional[str] = None,
    saved_only: Optional[bool] = None,
    sort: Literal["created_at_desc", "pnl_desc", "pnl_asc"] = "created_at_desc",
) -> Dict[str, Any]:
    runs, total = list_backtest_runs(
        session,
        limit=limit,
        offset=offset,
        symbol=symbol,
        strategy=strategy,
        saved_only=saved_only,
        sort=sort,
    )
    return {
        "items": [backtest_run_list_item(run) for run in runs],
        "total": total,
        "limit": limit,
        "offset": offset,
    }


def get_run(session: Session, run_id: str) -> BacktestRunDetailResponse:
    try:
        run_uuid = uuid.UUID(run_id)
    except ValueError as exc:
        raise HTTPException(
            status_code=404, detail=f"Backtest run '{run_id}' not found."
        ) from exc

    run = get_backtest_run(session, run_uuid)
    if run is None:
        raise HTTPException(status_code=404, detail=f"Backtest run '{run_id}' not found.")
    return backtest_run_detail(run)


def patch_run(
    session: Session,
    run_id: str,
    body: BacktestRunPatchRequest,
) -> BacktestRunDetailResponse:
    try:
        run_uuid = uuid.UUID(run_id)
    except ValueError as exc:
        raise HTTPException(
            status_code=404, detail=f"Backtest run '{run_id}' not found."
        ) from exc

    try:
        run = update_backtest_run(session, run_uuid, is_saved=body.is_saved)
    except ValueError as exc:
        raise HTTPException(
            status_code=404, detail=f"Backtest run '{run_id}' not found."
        ) from exc

    return backtest_run_detail(run)


def delete(session: Session, run_id: str) -> None:
    try:
        run_uuid = uuid.UUID(run_id)
    except ValueError as exc:
        raise HTTPException(
            status_code=404, detail=f"Backtest run '{run_id}' not found."
        ) from exc

    if not delete_backtest_run(session, run_uuid):
        raise HTTPException(status_code=404, detail=f"Backtest run '{run_id}' not found.")

    delete_backtest_lake_artifacts(run_id)


def bulk_delete(session: Session, body: BulkDeleteBacktestsRequest) -> Dict[str, Any]:
    parsed_ids: list[uuid.UUID] = []
    not_found: list[str] = []
    for run_id in body.run_ids:
        try:
            parsed_ids.append(uuid.UUID(run_id))
        except ValueError:
            not_found.append(run_id)

    deleted_count, missing_ids = delete_backtest_runs(session, parsed_ids)
    not_found.extend(str(run_id) for run_id in missing_ids)
    deleted_ids = set(parsed_ids) - set(missing_ids)
    for run_id in deleted_ids:
        delete_backtest_lake_artifacts(str(run_id))
    return {"deleted": deleted_count, "not_found": not_found}


def read_equity_artifact(run_id: str) -> Dict[str, Any]:
    try:
        uuid.UUID(run_id)
    except ValueError as exc:
        raise HTTPException(
            status_code=404, detail=f"Backtest run '{run_id}' not found."
        ) from exc

    try:
        df = read_backtest_artifact(run_id, "equity")
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    return {"run_id": run_id, "points": serialize_equity_artifact(df)}


def read_trades_artifact(run_id: str) -> Dict[str, Any]:
    try:
        uuid.UUID(run_id)
    except ValueError as exc:
        raise HTTPException(
            status_code=404, detail=f"Backtest run '{run_id}' not found."
        ) from exc

    try:
        df = read_backtest_artifact(run_id, "trades")
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    return {"run_id": run_id, "trades": serialize_trades_artifact(df)}
