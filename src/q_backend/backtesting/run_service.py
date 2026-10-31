"""Backtest run history, persistence, and lake artifact helpers."""

from __future__ import annotations

import logging
import uuid
from datetime import datetime
from typing import Any, Dict, List, Literal, Optional, Tuple

import pandas as pd
from fastapi import HTTPException
from sqlalchemy.orm import Session

from q_backend.api.schemas.backtest import (
    BacktestOrigin,
    BacktestProvenance,
    BacktestRunDetailResponse,
    BacktestRunListItem,
    BacktestRunPatchRequest,
    DayTradeSession,
    PostExitAnalysis,
    PostExitSideStats,
    PostExitTradeAnalysis,
    MLFilterReference,
)
from q_backend.api.schemas.common import BulkDeleteBacktestsRequest
from q_backend.backtesting.analytics import compute_backtest_performance, naive_utc
from q_backend.backtesting.excursions import post_exit_from_stored
from q_backend.backtesting.models import OrderAction, Trade
from q_backend.market_data.timezone import mt5_datetime_to_utc_iso
from q_backend.storage.db.engine import session_scope
from q_backend.research.strategy_versions import strategy_version_summaries
from q_backend.storage.db.models import BacktestRun, ResearchStrategyVersion
from q_backend.storage.db.repositories import (
    delete_backtest_run,
    delete_backtest_runs,
    get_backtest_run,
    get_ml_filter_model_version,
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

        entry_instances = [EntryInstance.model_validate(entry) for entry in entries]
        manager = EntryManagerConfig.model_validate(config.get("entry_manager", {"kind": "or", "params": {}}))
        return format_entry_strategy_label(entry_instances, manager)
    return config.get("strategy", "")


def backtest_run_fields(config: Dict[str, Any]) -> tuple[str, str, str]:
    return (
        config.get("symbol", ""),
        _strategy_display_name(config),
        config.get("timeframe", "D1"),
    )


def day_trade_session_from_config(config: Dict[str, Any]) -> DayTradeSession | None:
    if "day_trade" not in config:
        return None
    return DayTradeSession(
        start_time=str(config.get("day_trade_start_time", "09:00")),
        end_time=str(config.get("day_trade_end_time", "16:00")),
        close_time=str(config.get("day_trade_close_time", "17:00")),
        day_trade=bool(config.get("day_trade", False)),
    )


def backtest_run_list_item(run: BacktestRun, version: Optional[tuple[str, str]] = None) -> BacktestRunListItem:
    """A history row. ``version`` is the run's (strategy id, bundle hash) when it ran a strategy file."""
    config = run.config or {}
    symbol, strategy, timeframe = backtest_run_fields(config)
    version_strategy, bundle_hash = version if version is not None else (None, None)
    order_kinds = (run.result_summary or {}).get("order_kinds")
    day_trade_session = day_trade_session_from_config(config)
    return BacktestRunListItem(
        run_id=str(run.id),
        symbol=symbol,
        strategy=strategy or version_strategy or "",
        timeframe=timeframe,
        status=run.status,
        created_at=run.created_at,
        is_saved=run.is_saved,
        origin=run.origin,
        summary=run.result_summary,
        strategy_version_id=str(run.strategy_version_id) if run.strategy_version_id else None,
        strategy_bundle_hash=bundle_hash,
        order_kinds=order_kinds,
        day_trade_session=day_trade_session,
    )


def _ml_filter_reference(config: Dict[str, Any], session: Session | None) -> MLFilterReference | None:
    """Restore the exact pinned version; a deleted or unready model is reported unavailable."""
    pinned = config.get("ml_filter")
    if not pinned:
        return None
    model = get_ml_filter_model_version(session, pinned["model_version_id"]) if session is not None else None
    return MLFilterReference(
        model_version_id=pinned["model_version_id"],
        threshold=pinned["threshold"],
        available=model is not None and model.status == "ready",
    )


def backtest_run_detail(
    run: BacktestRun,
    session: Session | None = None,
    version: Optional[tuple[str, str]] = None,
) -> BacktestRunDetailResponse:
    config = dict(run.config or {})
    resolved_mode = (run.result_summary or {}).get("research_data_mode")
    if run.strategy_version_id is not None and run.status == "completed" and resolved_mode in ("bars", "ticks"):
        # Preserve the submitted config for deduplication; history reruns pin the actual source.
        config["research_data_mode"] = resolved_mode
    symbol, strategy, timeframe = backtest_run_fields(config)
    if version is None and session is not None and run.strategy_version_id is not None:
        stored = session.get(ResearchStrategyVersion, run.strategy_version_id)
        version = (stored.strategy_id, stored.bundle_hash) if stored is not None else None
    version_strategy, bundle_hash = version if version is not None else (None, None)
    order_kinds = (run.result_summary or {}).get("order_kinds")
    day_trade_session = day_trade_session_from_config(config)
    return BacktestRunDetailResponse(
        run_id=str(run.id),
        symbol=symbol,
        strategy=strategy or version_strategy or "",
        timeframe=timeframe,
        status=run.status,
        config=config,
        result_summary=run.result_summary,
        error_message=run.error_message,
        started_at=run.started_at,
        finished_at=run.finished_at,
        created_at=run.created_at,
        is_saved=run.is_saved,
        origin=run.origin,
        provenance=BacktestProvenance.model_validate(run.provenance) if run.provenance else None,
        ml_filter=_ml_filter_reference(config, session),
        strategy_version_id=str(run.strategy_version_id) if run.strategy_version_id else None,
        strategy_bundle_hash=bundle_hash,
        order_kinds=order_kinds,
        day_trade_session=day_trade_session,
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
    except Exception as exc:  # noqa: BLE001 - best-effort lake cleanup; logged
        logger.warning("Failed to delete backtest lake artifacts for %s: %s", run_id, exc)


def list_runs(
    session: Session,
    *,
    limit: int,
    offset: int,
    symbol: Optional[str] = None,
    strategy: Optional[str] = None,
    saved_only: Optional[bool] = None,
    origin: Optional[BacktestOrigin] = None,
    sort: Literal["created_at_desc", "pnl_desc", "pnl_asc"] = "created_at_desc",
) -> Dict[str, Any]:
    runs, total = list_backtest_runs(
        session,
        limit=limit,
        offset=offset,
        symbol=symbol,
        strategy=strategy,
        saved_only=saved_only,
        origin=origin,
        sort=sort,
    )
    versions = strategy_version_summaries(session, {run.strategy_version_id for run in runs if run.strategy_version_id})
    return {
        "items": [
            backtest_run_list_item(run, versions.get(run.strategy_version_id) if run.strategy_version_id else None)
            for run in runs
        ],
        "total": total,
        "limit": limit,
        "offset": offset,
    }


def get_run(session: Session, run_id: str) -> BacktestRunDetailResponse:
    try:
        run_uuid = uuid.UUID(run_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=f"Backtest run '{run_id}' not found.") from exc

    run = get_backtest_run(session, run_uuid)
    if run is None:
        raise HTTPException(status_code=404, detail=f"Backtest run '{run_id}' not found.")
    return backtest_run_detail(run, session)


def _config_datetime(value: Any) -> Optional[datetime]:
    return datetime.fromisoformat(value) if isinstance(value, str) else None


def _run_span(config: Dict[str, Any], trades: List[Trade]) -> Optional[Tuple[datetime, datetime]]:
    """The run's span from its config; a missing bound falls back to the first entry or the last exit."""
    start = _config_datetime(config.get("start"))
    end = _config_datetime(config.get("end"))
    if start is None and trades:
        start = min(naive_utc(t.entry_time) for t in trades)
    if end is None and any(t.exit_time is not None for t in trades):
        end = max(naive_utc(t.exit_time) for t in trades if t.exit_time is not None)
    if start is None or end is None:
        return None
    start, end = naive_utc(start), naive_utc(end)
    return (start, end) if start < end else None


def stored_result_performance(run_id: str, trades: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """Compute ``performance`` for a stored result that predates it, from the run's config and trades.

    Nothing is written back. Returns ``None`` when the run's initial capital or span is unknown, or
    when the stored data cannot be computed, so the result read still serves the chart payload.
    """
    try:
        with session_scope() as session:
            run = get_backtest_run(session, uuid.UUID(run_id))
            config = dict(run.config) if run is not None else {}
    except Exception:  # noqa: BLE001 - optional field; a lookup failure must not fail the result read
        logger.warning("Could not load run %s to compute its performance", run_id, exc_info=True)
        return None

    initial_capital = config.get("initial_capital")
    if initial_capital is None:
        return None
    try:
        closed = [Trade.model_validate(trade) for trade in trades]
        span = _run_span(config, closed)
        if span is None:
            return None
        performance = compute_backtest_performance(
            closed,
            float(initial_capital),
            *span,
            post_exit_bars=int(config.get("post_exit_bars", 10)),
        )
    except Exception:  # noqa: BLE001 - optional field; malformed stored trades degrade to null
        logger.warning("Could not compute performance for run %s", run_id, exc_info=True)
        return None
    return performance.model_dump(mode="json")


def patch_run(
    session: Session,
    run_id: str,
    body: BacktestRunPatchRequest,
) -> BacktestRunDetailResponse:
    try:
        run_uuid = uuid.UUID(run_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=f"Backtest run '{run_id}' not found.") from exc

    try:
        run = update_backtest_run(session, run_uuid, is_saved=body.is_saved)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=f"Backtest run '{run_id}' not found.") from exc

    return backtest_run_detail(run, session)


def delete(session: Session, run_id: str) -> None:
    try:
        run_uuid = uuid.UUID(run_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=f"Backtest run '{run_id}' not found.") from exc

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
        raise HTTPException(status_code=404, detail=f"Backtest run '{run_id}' not found.") from exc

    try:
        df = read_backtest_artifact(run_id, "equity")
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    return {"run_id": run_id, "points": serialize_equity_artifact(df)}


def read_trades_artifact(run_id: str) -> Dict[str, Any]:
    try:
        uuid.UUID(run_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=f"Backtest run '{run_id}' not found.") from exc

    try:
        df = read_backtest_artifact(run_id, "trades")
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    return {"run_id": run_id, "trades": serialize_trades_artifact(df)}


TRADES_CSV_COLUMNS: Dict[str, str] = {
    "id": "trade_id",
    "symbol": "symbol",
    "action": "side",
    "entry_time": "entry_time",
    "entry_price": "entry_price",
    "exit_time": "exit_time",
    "exit_price": "exit_price",
    "pnl": "pnl",
    "quantity": "quantity",
    "commission": "commission",
    "point_value": "point_value",
    "entry_reason": "entry_reason",
    "exit_reason": "exit_reason",
    "mfe_points": "mfe_points",
    "mae_points": "mae_points",
    "mfe_value": "mfe_value",
    "mae_value": "mae_value",
    "mfe_value": "mfe_value",
    "mae_value": "mae_value",
    "mfe_time": "mfe_time",
    "mae_time": "mae_time",
    "mfe_capture": "mfe_capture",
    "post_exit_mfe_points": "post_exit_mfe_points",
    "post_exit_mae_points": "post_exit_mae_points",
    "post_exit_return_points": "post_exit_return_points",
    "post_exit_bars_observed": "post_exit_bars_observed",
    "excursion_resolution": "excursion_resolution",
}


def _read_export_artifact(run_id: str, kind: Literal["trades", "market_data"]) -> pd.DataFrame:
    try:
        uuid.UUID(run_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=f"Backtest run '{run_id}' not found.") from exc

    try:
        return read_backtest_artifact(run_id, kind)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


def export_market_data_csv(run_id: str) -> str:
    """Return the bars and every computed indicator column of a run as CSV."""
    return _read_export_artifact(run_id, "market_data").to_csv(index=False)


def export_trades_csv(run_id: str, post_exit_bars: int | None = None) -> str:
    """Return the closed trades of a run as CSV."""
    records = serialize_trades_artifact(_read_export_artifact(run_id, "trades"))
    for record in records:
        record["mfe_value"] = (
            None
            if record.get("mfe_points") is None
            else record["mfe_points"] * record.get("quantity", 0) * record.get("point_value", 1)
        )
        record["mae_value"] = (
            None
            if record.get("mae_points") is None
            else record["mae_points"] * record.get("quantity", 0) * record.get("point_value", 1)
        )
    if post_exit_bars is not None:
        records = _replace_post_exit_values(run_id, records, post_exit_bars)
    frame = pd.DataFrame(records, columns=list(TRADES_CSV_COLUMNS)).rename(columns=TRADES_CSV_COLUMNS)
    return frame.to_csv(index=False)


def _stored_run_config(run_id: str) -> Dict[str, Any]:
    try:
        run_uuid = uuid.UUID(run_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=f"Backtest run '{run_id}' not found.") from exc
    with session_scope() as session:
        run = get_backtest_run(session, run_uuid)
        if run is None:
            raise HTTPException(status_code=404, detail=f"Backtest run '{run_id}' not found.")
        return dict(run.config)


def _market_data_frame(run_id: str) -> pd.DataFrame:
    try:
        frame = _read_export_artifact(run_id, "market_data")
    except HTTPException:
        raise
    if "time" not in frame.columns:
        raise HTTPException(status_code=404, detail="Backtest market_data artifact is missing its time column.")
    frame.index = pd.DatetimeIndex(pd.to_datetime(frame.pop("time"), utc=True), name="time")
    return frame


def _replace_post_exit_values(run_id: str, records: List[Dict[str, Any]], bars: int) -> List[Dict[str, Any]]:
    if not 1 <= bars <= 500:
        raise HTTPException(status_code=422, detail="post_exit_bars must be between 1 and 500")
    config = _stored_run_config(run_id)
    frame = _market_data_frame(run_id)
    trades = [Trade.model_validate(record) for record in records]
    values = post_exit_from_stored(
        trades,
        frame,
        post_exit_bars=bars,
        day_trade_close=config.get("day_trade_close_time") if config.get("day_trade") else None,
    )
    by_id = {value["trade_id"]: value for value in values}
    return [
        {**record, **{k: value for k, value in by_id[record["id"]].items() if k != "trade_id"}} for record in records
    ]


def post_exit_analysis(run_id: str, bars: int) -> PostExitAnalysis:
    records = serialize_trades_artifact(_read_export_artifact(run_id, "trades"))
    records = _replace_post_exit_values(run_id, records, bars)
    trades = [Trade.model_validate(record) for record in records]

    def side_stats(group: list[Trade]) -> PostExitSideStats:
        measured = [trade for trade in group if trade.post_exit_return_points is not None]
        money = [trade.post_exit_return_points * trade.quantity * trade.point_value for trade in measured]
        return PostExitSideStats(
            avg_post_exit_return=sum(money) / len(money) if money else None,
            post_exit_continuation_rate=(
                sum(t.post_exit_return_points > 0 for t in measured) / len(measured) if measured else None
            ),
        )

    return PostExitAnalysis(
        post_exit_bars=bars,
        trades=[
            PostExitTradeAnalysis(
                trade_id=t.id,
                post_exit_mfe_points=t.post_exit_mfe_points,
                post_exit_mae_points=t.post_exit_mae_points,
                post_exit_return_points=t.post_exit_return_points,
                post_exit_bars_observed=t.post_exit_bars_observed or 0,
            )
            for t in trades
        ],
        all=side_stats(trades),
        long=side_stats([t for t in trades if t.action == OrderAction.BUY]),
        short=side_stats([t for t in trades if t.action == OrderAction.SELL]),
    )
