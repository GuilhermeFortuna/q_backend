"""Backtest run history, persistence, and lake artifact helpers."""

from __future__ import annotations

import logging
import uuid
from datetime import datetime
from typing import Any, Dict, List, Literal, Optional

import pandas as pd
from fastapi import HTTPException
from sqlalchemy.orm import Session

from q_backend.api.schemas.backtest import (
    BacktestRunDetailResponse,
    BacktestRunListItem,
    BacktestRunPatchRequest,
)
from q_backend.api.schemas.common import BulkDeleteBacktestsRequest
from q_backend.market_data.timezone import mt5_datetime_to_utc_iso
from q_backend.storage.db.models import BacktestRun
from q_backend.storage.db.repositories import (
    delete_backtest_run,
    delete_backtest_runs,
    get_backtest_run,
    list_backtest_runs,
    update_backtest_run,
)
from q_backend.storage.lake import delete_backtest_artifacts, read_backtest_artifact

logger = logging.getLogger(__name__)


def _strategy_display_name(config: Dict[str, Any]) -> str:
    entries = config.get("entries")
    if entries:
        from q_backend.api.schemas.backtest import EntryInstance, EntryManagerConfig
        from q_backend.backtesting.entry_config import format_entry_strategy_label

        entry_instances = [
            EntryInstance.model_validate(entry) for entry in entries
        ]
        manager = EntryManagerConfig.model_validate(
            config.get("entry_manager", {"kind": "or", "params": {}})
        )
        return format_entry_strategy_label(entry_instances, manager)
    return config.get("strategy", "")


def backtest_run_fields(config: Dict[str, Any]) -> tuple[str, str, str]:
    return (
        config.get("symbol", ""),
        _strategy_display_name(config),
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
        logger.warning(
            "Failed to delete backtest lake artifacts for %s: %s", run_id, exc
        )


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
        raise HTTPException(
            status_code=404, detail=f"Backtest run '{run_id}' not found."
        )
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
        raise HTTPException(
            status_code=404, detail=f"Backtest run '{run_id}' not found."
        )

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
