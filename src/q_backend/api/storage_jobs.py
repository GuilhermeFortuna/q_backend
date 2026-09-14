"""Background jobs for MT5 → local parquet ingestion (WO48 / WO50)."""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timedelta
from typing import Any, Literal, Optional

from pydantic import BaseModel, Field, model_validator

from q_backend.market_data import local_store
from q_backend.market_data.clients.metatrader import TIMEFRAME_MAP, _to_naive_local
from q_backend.storage.db.engine import session_scope
from q_backend.storage.redis.client import get_redis
from q_backend.storage.redis.progress import get_job_progress, set_job_progress
from q_backend.streaming.jobs import record_job_terminal

logger = logging.getLogger(__name__)

PROGRESS_NAMESPACE = "storage_ingest"


class IngestJobRequest(BaseModel):
    symbol: str
    timeframes: list[str] = Field(default_factory=list)
    start: datetime
    end: datetime
    kind: Literal["bars", "ticks"] = "bars"

    @model_validator(mode="after")
    def validate_timeframes_for_kind(self) -> "IngestJobRequest":
        if self.kind == "bars" and len(self.timeframes) < 1:
            raise ValueError("At least one timeframe is required for bar ingestion.")
        return self


class IngestTimeframeResult(BaseModel):
    timeframe: str
    rows: int = 0
    start: Optional[str] = None
    end: Optional[str] = None
    status: Literal["completed", "failed"]
    error: Optional[str] = None


def validate_timeframes(timeframes: list[str]) -> list[str]:
    normalized: list[str] = []
    for tf in timeframes:
        key = tf.strip().upper()
        if key not in TIMEFRAME_MAP:
            raise ValueError(f"Invalid timeframe '{tf}'. Choose from: {list(TIMEFRAME_MAP.keys())}")
        normalized.append(key)
    return normalized


def _resolve_range(start: datetime, end: datetime) -> tuple[datetime, datetime]:
    start = _to_naive_local(start)
    end = _to_naive_local(end)
    if start >= end:
        raise ValueError("Start datetime must be before end datetime.")
    return start, end


def _iter_month_chunks(start: datetime, end: datetime) -> list[tuple[datetime, datetime, str]]:
    chunks: list[tuple[datetime, datetime, str]] = []
    cursor = datetime(start.year, start.month, 1)
    end_month = datetime(end.year, end.month, 1)
    while cursor <= end_month:
        if cursor.month == 12:
            next_month = datetime(cursor.year + 1, 1, 1)
        else:
            next_month = datetime(cursor.year, cursor.month + 1, 1)
        month_end = next_month - timedelta(milliseconds=1)
        chunk_start = max(start, cursor)
        chunk_end = min(end, month_end)
        if chunk_start <= chunk_end:
            label = f"{cursor.year}-{cursor.month:02d}"
            chunks.append((chunk_start, chunk_end, label))
        cursor = next_month
    return chunks


def _persist_progress(job_id: str, payload: dict[str, Any]) -> None:
    try:
        set_job_progress(
            get_redis(),
            job_id,
            payload,
            namespace=PROGRESS_NAMESPACE,
        )
    except Exception:  # noqa: BLE001 - best-effort Redis progress; logged
        logger.debug("Redis progress unavailable for storage ingest job %s", job_id)


def start_job(request: IngestJobRequest) -> str:
    job_id = str(uuid.uuid4())
    _persist_progress(
        job_id,
        {
            "job_id": job_id,
            "status": "queued",
            "progress": 0.0,
            "detail": "Queued",
            "results": None,
            "error": None,
        },
    )

    from q_backend.tasks import actors

    actors.run_storage_ingest.send(job_id, request.model_dump_json())
    return job_id


def _provider_label(provider: Any) -> str:
    """Human name for ingest source, for progress/error messages."""
    return type(provider).__name__.replace("Client", "") or "provider"


def _run_bars_ingest(
    job_id: str,
    symbol: str,
    timeframes: list[str],
    start: datetime,
    end: datetime,
    provider: Any,
) -> tuple[list[dict[str, Any]], str, str, Optional[str]]:
    results: list[dict[str, Any]] = []
    total = len(timeframes)
    source_label = _provider_label(provider)

    for index, timeframe in enumerate(timeframes):
        detail = f"Ingesting {symbol} {timeframe} bars ({index + 1}/{total})"
        _persist_progress(
            job_id,
            {
                "job_id": job_id,
                "status": "running",
                "progress": index / total,
                "detail": detail,
                "results": results,
                "error": None,
            },
        )
        try:
            bars = provider.get_ohlcv(symbol, timeframe, start, end)
            if not bars:
                raise ValueError(f"No OHLCV bars returned from {source_label} for " f"{symbol}/{timeframe}.")
            catalog_entry = local_store.write_ohlcv(symbol, timeframe, bars)
            results.append(
                IngestTimeframeResult(
                    timeframe=timeframe,
                    rows=int(catalog_entry.get("rows", len(bars))),
                    start=catalog_entry.get("start"),
                    end=catalog_entry.get("end"),
                    status="completed",
                ).model_dump()
            )
        except Exception as exc:  # noqa: BLE001 — isolate per timeframe
            logger.warning("Storage ingest failed for %s/%s: %s", symbol, timeframe, exc)
            results.append(
                IngestTimeframeResult(
                    timeframe=timeframe,
                    status="failed",
                    error=str(exc),
                ).model_dump()
            )

    completed = sum(1 for row in results if row.get("status") == "completed")
    failed = total - completed
    status = "completed" if completed > 0 else "failed"
    terminal_error: Optional[str] = None
    if failed and completed:
        detail = f"Ingested {completed}/{total} timeframes for {symbol}"
    elif failed:
        detail = f"Ingestion failed for all timeframes on {symbol}"
        terminal_error = detail
    else:
        detail = f"Ingestion completed for {symbol}"
    return results, detail, status, terminal_error


def _run_ticks_ingest(
    job_id: str,
    symbol: str,
    start: datetime,
    end: datetime,
    provider: Any,
) -> tuple[list[dict[str, Any]], str, str, Optional[str]]:
    results: list[dict[str, Any]] = []
    source_label = _provider_label(provider)
    month_chunks = _iter_month_chunks(start, end)
    total = len(month_chunks)
    if total == 0:
        raise ValueError("Tick ingest range must span at least one calendar month.")

    for index, (chunk_start, chunk_end, month_label) in enumerate(month_chunks):
        detail = f"Ingesting {symbol} ticks {month_label} ({index + 1}/{total})"
        _persist_progress(
            job_id,
            {
                "job_id": job_id,
                "status": "running",
                "progress": index / total,
                "detail": detail,
                "results": results,
                "error": None,
            },
        )
        try:
            arrays = provider.get_ticks_columnar(symbol, chunk_start, chunk_end, use_cache=False)
            if len(arrays.get("time_msc", [])) == 0:
                raise ValueError(f"No ticks returned from {source_label} for {symbol} in " f"{month_label}.")
            catalog_entry = local_store.write_ticks(symbol, arrays)
            results.append(
                IngestTimeframeResult(
                    timeframe=month_label,
                    rows=len(arrays["time_msc"]),
                    start=catalog_entry.get("start"),
                    end=catalog_entry.get("end"),
                    status="completed",
                ).model_dump()
            )
        except Exception as exc:  # noqa: BLE001 — isolate per month
            logger.warning("Tick ingest failed for %s/%s: %s", symbol, month_label, exc)
            results.append(
                IngestTimeframeResult(
                    timeframe=month_label,
                    status="failed",
                    error=str(exc),
                ).model_dump()
            )

    completed = sum(1 for row in results if row.get("status") == "completed")
    failed = total - completed
    status = "completed" if completed > 0 else "failed"
    terminal_error: Optional[str] = None
    if failed and completed:
        detail = f"Ingested {completed}/{total} months of ticks for {symbol}"
    elif failed:
        detail = f"Tick ingestion failed for all months on {symbol}"
        terminal_error = detail
    else:
        detail = f"Tick ingestion completed for {symbol}"
    return results, detail, status, terminal_error


def run_ingest_job(job_id: str, request_json: str) -> None:
    request = IngestJobRequest.model_validate_json(request_json)
    from q_backend.tasks.worker_context import get_worker_market_data_service

    service = get_worker_market_data_service()
    results: list[dict[str, Any]] = []
    terminal_error: Optional[str] = None

    try:
        symbol = request.symbol.upper()
        start, end = _resolve_range(request.start, request.end)

        _persist_progress(
            job_id,
            {
                "job_id": job_id,
                "status": "running",
                "progress": 0.0,
                "detail": f"Ingesting {symbol} ({request.kind})",
                "results": [],
                "error": None,
            },
        )

        try:
            provider = service.acquisition_provider()
        except ConnectionError as exc:
            raise RuntimeError(f"Ingestion requires a reachable acquisition provider: {exc}") from exc

        if request.kind == "ticks":
            results, detail, status, terminal_error = _run_ticks_ingest(job_id, symbol, start, end, provider)
        else:
            timeframes = validate_timeframes(request.timeframes)
            results, detail, status, terminal_error = _run_bars_ingest(job_id, symbol, timeframes, start, end, provider)

        try:
            with session_scope() as session:
                record_job_terminal(
                    session,
                    kind="storage_ingest",
                    job_id=job_id,
                    raw_status=status,
                    error=terminal_error,
                )
        except Exception as term_exc:  # noqa: BLE001
            logger.warning("Failed to record terminal event for storage ingest job %s: %s", job_id, term_exc)
        _persist_progress(
            job_id,
            {
                "job_id": job_id,
                "status": status,
                "progress": 1.0,
                "detail": detail,
                "results": results,
                "error": terminal_error,
            },
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("Storage ingest job %s failed", job_id)
        try:
            with session_scope() as session:
                record_job_terminal(
                    session,
                    kind="storage_ingest",
                    job_id=job_id,
                    raw_status="failed",
                    error=str(exc),
                )
        except Exception as term_exc:  # noqa: BLE001
            logger.warning("Failed to record terminal failure for storage ingest job %s: %s", job_id, term_exc)
        _persist_progress(
            job_id,
            {
                "job_id": job_id,
                "status": "failed",
                "progress": 1.0,
                "detail": str(exc),
                "results": results or None,
                "error": str(exc),
            },
        )


def get_status_payload(job_id: str) -> Optional[dict[str, Any]]:
    try:
        return get_job_progress(get_redis(), job_id, namespace=PROGRESS_NAMESPACE)
    except Exception:  # noqa: BLE001 - best-effort Redis progress; logged
        logger.debug("Redis progress unavailable for storage ingest job %s", job_id)
        return None
