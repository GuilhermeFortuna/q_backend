import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import List, Optional, Dict, Any, Literal
import base64
import urllib.request
import xml.etree.ElementTree as ET
import email.utils
from fastapi import Depends, FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from sqlalchemy.orm import Session

from q_backend.api.deps import get_session
from q_backend.api.dependencies import market_data_service
from q_backend.api.lifespan import lifespan
from q_backend.api.routers import strategies as strategies_router
from q_backend.api.routers import system as system_router
from q_backend.api.routers import market as market_router
from q_backend.api.schemas.market import OhlcvBarResponse
from q_backend.api.schemas.common import (
    BulkDeleteBacktestsRequest,
    BulkDeleteOptimizationsRequest,
    BulkDeleteResponse,
)
from q_backend.market_data.models import OHLCV, Tick
import pandas as pd
from q_backend.backtesting.factory import build_strategy
from q_backend.backtesting.chart_data import serialize_chart_data
from q_backend.backtesting.position_sizing import (
    FixedQuantityPositionSizing,
    PositionSizingConfig,
    build_position_sizer,
)
from q_backend.backtesting.costs import TransactionCostConfig
from q_backend.backtesting.engine import BacktestEngine, ParallelMode
from q_backend.backtesting.tick.chart_data import serialize_tick_chart_data
from q_backend.backtesting.tick.engine import TickBacktestEngine
from q_backend.backtesting.tick.factory import build_tick_strategy
from q_backend.backtesting.tick.strategy import TickArrays
from q_backend.market_data.clients.metatrader import (
    _to_naive_local,
    resolve_copy_ticks_flags,
)
from q_backend.market_data.timezone import mt5_datetime_to_utc_iso, unix_seconds_to_utc_iso
from q_backend.storage.runtime_config import get_data_source
from q_backend.optimization import OptimizationConfig
from q_backend.optimization.metrics import build_equity_curve
from q_backend.api import backtest_jobs
from q_backend.api import storage_jobs
from q_backend.api import optimization_jobs
from q_backend.api import strategy_search_jobs
from q_backend.api import walkforward_jobs
from q_backend.api.storage_jobs import IngestJobRequest
from q_backend.api.backtest_jobs import BacktestJobRequest
from q_backend.market_data import local_store
from q_backend.api.walkforward_jobs import WalkForwardRequest
from q_backend.optimization.strategy_search import StrategySearchConfig
from q_backend.storage.lake import (
    delete_backtest_artifacts,
    read_backtest_artifact,
    read_backtest_result,
    read_strategy_search_candidate_artifact,
    read_walkforward_artifact,
    write_backtest_artifacts,
)
from q_backend.storage.db.engine import session_scope
from q_backend.storage.db.models import BacktestRun, RunStatus
from q_backend.storage.db.repositories import (
    create_backtest_config,
    create_backtest_run,
    delete_backtest_run,
    delete_backtest_runs,
    delete_optimization_study,
    delete_optimization_studies,
    delete_walkforward_run,
    delete_strategy_search_run,
    find_backtest_run_by_config,
    get_backtest_run,
    get_or_create_strategy,
    list_backtest_runs,
    list_optimization_studies,
    list_walkforward_runs,
    list_strategy_search_runs,
    update_backtest_run,
)

# Setup logging
logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)


# Pydantic schemas for frontend compatibility
class NewsArticleResponse(BaseModel):
    id: str
    title: str
    source: str
    publishedAt: str
    summary: str
    content: str
    videoUrl: Optional[str] = None
    imageUrl: Optional[str] = None


class StorageInventoryItem(BaseModel):
    symbol: str
    timeframe: str
    start: str
    end: str
    rows: int
    bytes: int
    updated_at: str


class StorageInventoryResponse(BaseModel):
    root: str
    items: List[StorageInventoryItem]


class StorageIngestStartResponse(BaseModel):
    job_id: str
    status: Literal["queued"]


class StorageIngestStatusResponse(BaseModel):
    job_id: str
    status: Literal["queued", "running", "completed", "failed"]
    progress: float
    detail: str
    results: Optional[List[Dict[str, Any]]] = None
    error: Optional[str] = None


class StorageDeleteResponse(BaseModel):
    deleted: bool
    symbol: str
    timeframe: str


class BacktestRequest(BaseModel):
    symbol: str
    timeframe: str = "D1"
    start: Optional[datetime] = None
    end: Optional[datetime] = None
    initial_capital: float = 100000.0
    point_value: float = 1.0
    strategy: str = "MACrossover"  # Support for multiple strategies in the future
    strategy_params: Dict[str, Any] = {}
    position_sizing: Optional[PositionSizingConfig] = None
    costs: Optional[TransactionCostConfig] = None
    engine: Literal["candle", "tick"] = "candle"
    display_timeframe: str = "M1"
    tick_flags: Optional[str] = None
    day_trade: bool = False
    day_trade_start_time: str = "09:00"
    day_trade_end_time: str = "16:00"
    day_trade_close_time: str = "17:00"


class ChartIndicatorSeries(BaseModel):
    key: str
    label: str
    pane: Literal["price", "oscillator"]
    color: Optional[str] = None
    values: List[Optional[float]]


class BacktestResponse(BaseModel):
    metrics: Dict[str, Any]
    trades: List[Dict[str, Any]]
    bars: List[OhlcvBarResponse]
    indicators: List[ChartIndicatorSeries]
    run_id: Optional[str] = None


class BacktestStartResponse(BaseModel):
    run_id: str
    status: str


class BacktestStatusResponse(BaseModel):
    run_id: str
    status: str
    error: Optional[str] = None


class BacktestRunListItem(BaseModel):
    run_id: str
    symbol: str
    strategy: str
    timeframe: str
    status: str
    created_at: datetime
    is_saved: bool = False
    summary: Optional[Dict[str, Any]] = None


class BacktestRunListResponse(BaseModel):
    items: List[BacktestRunListItem]
    total: int
    limit: int
    offset: int


class BacktestRunDetailResponse(BaseModel):
    run_id: str
    symbol: str
    strategy: str
    timeframe: str
    status: str
    config: Dict[str, Any]
    result_summary: Optional[Dict[str, Any]] = None
    error_message: Optional[str] = None
    started_at: Optional[datetime] = None
    finished_at: Optional[datetime] = None
    created_at: datetime
    is_saved: bool = False


class BacktestRunPatchRequest(BaseModel):
    is_saved: bool


class EquityArtifactPoint(BaseModel):
    time: str
    equity: float


class BacktestEquityArtifactResponse(BaseModel):
    run_id: str
    points: List[EquityArtifactPoint]


class BacktestTradesArtifactResponse(BaseModel):
    run_id: str
    trades: List[Dict[str, Any]]


class OptimizationStartResponse(BaseModel):
    study_id: str
    status: str


class OptimizationStatusResponse(BaseModel):
    study_id: str
    status: str
    completed_trials: int
    n_trials: int
    best_value: Optional[float] = None
    best_params: Dict[str, Any] = {}
    error: Optional[str] = None
    workers: int = 1
    backtest_config: Optional[Dict[str, Any]] = None
    optimization_config: Optional[Dict[str, Any]] = None
    trials: Optional[List[Dict[str, Any]]] = None
    best_trial: Optional[Dict[str, Any]] = None


class OptimizationResultsResponse(BaseModel):
    study_id: str
    objective_mode: str
    is_multi_objective: bool
    best_params: Dict[str, Any]
    best_trial: Optional[Dict[str, Any]] = None
    trials: List[Dict[str, Any]]
    pareto_trials: List[Dict[str, Any]]
    failures: List[Dict[str, Any]]


class OptimizationStudyListItem(BaseModel):
    study_id: str
    name: str
    status: str
    best_value: Optional[float] = None
    n_trials: int
    completed_trials: int
    created_at: datetime


class OptimizationStudyListResponse(BaseModel):
    items: List[OptimizationStudyListItem]
    total: int
    limit: int
    offset: int


class WalkForwardStartResponse(BaseModel):
    run_id: str
    status: str


class WalkForwardStatusResponse(BaseModel):
    run_id: str
    status: str
    current_window: int
    total_windows: int
    phase: Optional[Literal["optimizing", "testing"]] = None
    windows_completed: int
    error: Optional[str] = None
    optimization_config: Optional[Dict[str, Any]] = None
    walkforward_config: Optional[Dict[str, Any]] = None
    backtest_config: Optional[Dict[str, Any]] = None


class WalkForwardWindowResultResponse(BaseModel):
    index: int
    train_start: str
    train_end: str
    test_start: str
    test_end: str
    status: str
    best_params: Dict[str, Any] = {}
    is_metrics: Optional[Dict[str, Any]] = None
    oos_metrics: Optional[Dict[str, Any]] = None


class WalkForwardResultsResponse(BaseModel):
    run_id: str
    status: str
    windows: List[WalkForwardWindowResultResponse]
    oos_metrics: Dict[str, Any]
    efficiency: Optional[float] = None
    equity_curve: List[EquityArtifactPoint]
    optimization_config: Optional[Dict[str, Any]] = None
    walkforward_config: Optional[Dict[str, Any]] = None
    lake_paths: Optional[Dict[str, str]] = None


class WalkForwardRunListItem(BaseModel):
    run_id: str
    name: str
    status: str
    symbol: Optional[str] = None
    strategy: Optional[str] = None
    efficiency: Optional[float] = None
    window_count: int = 0
    created_at: datetime


class WalkForwardRunListResponse(BaseModel):
    items: List[WalkForwardRunListItem]
    total: int
    limit: int
    offset: int


class StrategySearchStartResponse(BaseModel):
    run_id: str
    status: str


class StrategySearchStatusResponse(BaseModel):
    run_id: str
    status: str
    current_candidate: float
    total_candidates: int
    candidate_id: Optional[str] = None
    strategy: Optional[str] = None
    phase: Optional[Literal["optimizing", "testing", "done"]] = None
    window_index: Optional[int] = None
    total_windows: Optional[int] = None
    generation: Optional[int] = None
    total_generations: Optional[int] = None
    error: Optional[str] = None
    search_config: Optional[Dict[str, Any]] = None
    backtest_config: Optional[Dict[str, Any]] = None
    logs: Optional[List[str]] = None


class StrategySearchCandidateResponse(BaseModel):
    candidate_id: str
    strategy: str
    status: str
    rank: Optional[int] = None
    objective_value: Optional[float] = None
    robustness_score: Optional[float] = None
    efficiency: Optional[float] = None
    gate_flags: List[str] = []
    passed_gates: bool = False
    oos_metrics: Optional[Dict[str, Any]] = None
    is_metrics_summary: Optional[Dict[str, Any]] = None
    best_params: Optional[Dict[str, Any]] = None
    window_count: int = 0
    completed_windows: int = 0
    error: Optional[str] = None
    generation: Optional[int] = None
    genome: Optional[Dict[str, Any]] = None
    genome_node_count: Optional[int] = None
    dsr: Optional[float] = None
    complexity_penalty: Optional[float] = None


class StrategySearchResultsResponse(BaseModel):
    run_id: str
    status: str
    objective_mode: Optional[str] = None
    summary: Dict[str, Any] = {}
    candidates: List[StrategySearchCandidateResponse]
    best: Optional[StrategySearchCandidateResponse] = None
    search_config: Optional[Dict[str, Any]] = None
    lake_paths: Optional[Dict[str, Any]] = None


class StrategySearchRunListItem(BaseModel):
    run_id: str
    name: str
    status: str
    symbol: Optional[str] = None
    candidate_count: int = 0
    best_strategy: Optional[str] = None
    best_objective_value: Optional[float] = None
    created_at: datetime


class StrategySearchRunListResponse(BaseModel):
    items: List[StrategySearchRunListItem]
    total: int
    limit: int
    offset: int


class StrategySearchCandidateEquityArtifactResponse(BaseModel):
    run_id: str
    candidate_id: str
    points: List[EquityArtifactPoint]


class StrategySearchCandidateGenomeResponse(BaseModel):
    run_id: str
    candidate_id: str
    genome: Dict[str, Any]


def _backtest_run_fields(config: Dict[str, Any]) -> tuple[str, str, str]:
    return (
        config.get("symbol", ""),
        config.get("strategy", ""),
        config.get("timeframe", "D1"),
    )


def _backtest_run_list_item(run: BacktestRun) -> BacktestRunListItem:
    symbol, strategy, timeframe = _backtest_run_fields(run.config or {})
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


def _backtest_run_detail(run: BacktestRun) -> BacktestRunDetailResponse:
    config = run.config or {}
    symbol, strategy, timeframe = _backtest_run_fields(config)
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


def _backtest_request_config(request: BacktestRequest) -> Dict[str, Any]:
    config_dict = request.model_dump(mode="json")
    if request.engine == "tick":
        config_dict["timeframe"] = "TICK"
    return config_dict


def _resolve_tick_flags(tick_flags: Optional[str]) -> int:
    try:
        return resolve_copy_ticks_flags(tick_flags)
    except ValueError as exc:
        raise ValueError(str(exc)) from exc


def _columnar_to_tick_arrays(columnar: Dict[str, Any]) -> TickArrays:
    return TickArrays(
        time_msc=columnar["time_msc"],
        bid=columnar["bid"],
        ask=columnar["ask"],
        last=columnar["last"],
        volume=columnar["volume"],
    )


def _run_tick_backtest(
    request: BacktestRequest,
    start: datetime,
    end: datetime,
    run_id: Optional[str],
) -> Dict[str, Any]:
    try:
        tick_flags = _resolve_tick_flags(request.tick_flags)
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
                    "(POST /api/v1/storage/ingest with kind=\"ticks\")."
                ),
            )
        raise HTTPException(
            status_code=404,
            detail="No tick data found for the given parameters.",
        )

    ticks = _columnar_to_tick_arrays(arrays)

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
        equity_df = _build_equity_dataframe(
            closed_trade_objects,
            request.initial_capital,
            start,
            end,
        )
        lake_paths = _write_backtest_lake_artifacts(run_id, closed_trades, equity_df)

    _finish_backtest_run(
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


def _start_backtest_run(request: BacktestRequest) -> Optional[str]:
    try:
        config_dict = _backtest_request_config(request)
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


def _build_equity_dataframe(
    closed_trades: list,
    initial_capital: float,
    start: datetime,
    end: datetime,
) -> pd.DataFrame:
    equity_series = build_equity_curve(closed_trades, initial_capital, start, end)
    return pd.DataFrame({"time": equity_series.index, "equity": equity_series.values})


def _write_backtest_lake_artifacts(
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


def _finish_backtest_run(
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


def _artifact_datetime_to_iso(value: Any) -> str:
    if isinstance(value, pd.Timestamp):
        value = value.to_pydatetime()
    if isinstance(value, datetime):
        return mt5_datetime_to_utc_iso(value)
    return str(value)


def _serialize_trades_artifact(df: pd.DataFrame) -> List[Dict[str, Any]]:
    records: List[Dict[str, Any]] = []
    for record in df.to_dict(orient="records"):
        serialized: Dict[str, Any] = {}
        for key, value in record.items():
            if value is None or (isinstance(value, float) and pd.isna(value)):
                serialized[key] = None
            elif isinstance(value, (pd.Timestamp, datetime)):
                serialized[key] = _artifact_datetime_to_iso(value)
            else:
                serialized[key] = value
        records.append(serialized)
    return records


def _serialize_equity_artifact(df: pd.DataFrame) -> List[Dict[str, Any]]:
    time_col = "time" if "time" in df.columns else df.columns[0]
    equity_col = "equity" if "equity" in df.columns else df.columns[1]
    points: List[Dict[str, Any]] = []
    for _, row in df.iterrows():
        points.append(
            {
                "time": _artifact_datetime_to_iso(row[time_col]),
                "equity": float(row[equity_col]),
            }
        )
    return points


def _delete_backtest_lake_artifacts(run_id: str) -> None:
    try:
        delete_backtest_artifacts(run_id)
    except Exception as exc:
        logger.warning("Failed to delete backtest lake artifacts for %s: %s", run_id, exc)


app = FastAPI(
    title="QuantLauncher API Backend",
    description="Backend API for fetching market data and executing orders using MetaTrader 5",
    version="0.1.0",
    lifespan=lifespan,
)

# Enable CORS for frontend connection
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # Allow all origins for local development
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(system_router.router)
app.include_router(strategies_router.router)
app.include_router(market_router.router)


@app.get("/api/v1/storage/inventory", response_model=StorageInventoryResponse)
def get_storage_inventory():
    return {
        "root": str(local_store.market_data_root()),
        "items": local_store.list_inventory(),
    }


@app.post("/api/v1/storage/ingest", response_model=StorageIngestStartResponse)
def start_storage_ingest(request: IngestJobRequest):
    if not market_data_service.mt5_available():
        raise HTTPException(
            status_code=503,
            detail=(
                "Ingestion requires MetaTrader 5 as the source. "
                "MT5 is not available on this machine."
            ),
        )
    try:
        if request.kind == "bars":
            storage_jobs.validate_timeframes(request.timeframes)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    job_id = storage_jobs.start_job(request)
    return {"job_id": job_id, "status": "queued"}


@app.get(
    "/api/v1/storage/ingest/{job_id}",
    response_model=StorageIngestStatusResponse,
)
def get_storage_ingest_status(job_id: str):
    payload = storage_jobs.get_status_payload(job_id)
    if payload is None:
        raise HTTPException(
            status_code=404, detail=f"Storage ingest job '{job_id}' not found."
        )
    return payload


@app.delete(
    "/api/v1/storage/{symbol}/{timeframe}",
    response_model=StorageDeleteResponse,
)
def delete_storage_series(symbol: str, timeframe: str):
    local_store.delete_ohlcv(symbol.upper(), timeframe.upper())
    return {
        "deleted": True,
        "symbol": symbol.upper(),
        "timeframe": timeframe.upper(),
    }


@app.post("/api/v1/backtest/run", response_model=BacktestResponse)
def run_backtest(request: BacktestRequest):
    """
    Run a backtest for a specific symbol and strategy.
    """
    start = request.start or (datetime.now() - timedelta(days=365))
    end = request.end or datetime.now()

    start = _to_naive_local(start)
    end = _to_naive_local(end)

    if start >= end:
        raise HTTPException(
            status_code=400, detail="Start datetime must be before end datetime."
        )

    run_id = _start_backtest_run(request)

    try:
        if request.engine == "tick":
            return _run_tick_backtest(request, start, end, run_id)

        # 1. Fetch data
        ohlcv_data = market_data_service.get_ohlcv(
            request.symbol, request.timeframe, start, end
        )
        if not ohlcv_data:
            raise HTTPException(
                status_code=404, detail="No market data found for the given parameters."
            )

        # Convert to DataFrame
        df = pd.DataFrame([b.model_dump() for b in ohlcv_data])
        df.set_index("time", inplace=True)
        # Ensure index is datetime
        df.index = pd.to_datetime(df.index, format="ISO8601")

        # 2. Setup Strategy
        try:
            strategy = build_strategy(
                request.strategy, request.strategy_params, request.symbol
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        # Compute indicators for chart payload (same logic used inside the engine)
        df_with_indicators = strategy.compute_indicators(df.copy())
        chart_data = serialize_chart_data(df_with_indicators, strategy)

        # 3. Setup Position Sizer
        sizer = build_position_sizer(
            request.position_sizing, point_value=request.point_value
        )

        # 4. Run Engine
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

        # 5. Extract Results
        metrics = registry.get_performance_metrics(request.initial_capital)
        closed_trade_objects = registry.get_closed_trades()
        closed_trades = [t.model_dump() for t in closed_trade_objects]

        lake_paths = None
        if run_id is not None:
            equity_df = _build_equity_dataframe(
                closed_trade_objects,
                request.initial_capital,
                start,
                end,
            )
            lake_paths = _write_backtest_lake_artifacts(run_id, closed_trades, equity_df)

        _finish_backtest_run(
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
        _finish_backtest_run(
            run_id,
            status=RunStatus.FAILED.value,
            error_message=str(exc.detail),
        )
        raise
    except Exception as e:
        _finish_backtest_run(
            run_id,
            status=RunStatus.FAILED.value,
            error_message=str(e),
        )
        logger.error(f"Error running backtest: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/v1/backtest", response_model=BacktestStartResponse)
def start_backtest(request: BacktestJobRequest):
    """Dispatch a backtest to the worker pool and return its run id for polling."""
    run_id = backtest_jobs.start_job(request)
    return {"run_id": run_id, "status": "running"}


@app.get("/api/v1/backtest/{run_id}", response_model=BacktestStatusResponse)
def get_backtest_status(run_id: str):
    """Return the current status of an async backtest run."""
    payload = backtest_jobs.get_status_payload(run_id)
    if payload is None:
        raise HTTPException(status_code=404, detail=f"Backtest run '{run_id}' not found.")
    return payload


@app.get("/api/v1/backtest/{run_id}/result", response_model=BacktestResponse)
def get_backtest_result(run_id: str):
    """Return the full chart payload for a completed async backtest run."""
    try:
        return read_backtest_result(run_id)
    except FileNotFoundError as exc:
        raise HTTPException(
            status_code=404,
            detail=f"Backtest result not ready or not found for run '{run_id}'.",
        ) from exc


@app.get("/api/v1/backtests", response_model=BacktestRunListResponse)
def list_backtests(
    session: Session = Depends(get_session),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    symbol: Optional[str] = None,
    strategy: Optional[str] = None,
    saved_only: Optional[bool] = None,
    sort: Literal["created_at_desc", "pnl_desc", "pnl_asc"] = "created_at_desc",
):
    """Return a paginated list of backtest runs."""
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
        "items": [_backtest_run_list_item(run) for run in runs],
        "total": total,
        "limit": limit,
        "offset": offset,
    }


@app.post("/api/v1/backtests/bulk-delete", response_model=BulkDeleteResponse)
def bulk_delete_backtests(
    body: BulkDeleteBacktestsRequest,
    session: Session = Depends(get_session),
):
    """Delete multiple backtest runs in one request."""
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
        _delete_backtest_lake_artifacts(str(run_id))
    return {"deleted": deleted_count, "not_found": not_found}


@app.get("/api/v1/backtests/{run_id}", response_model=BacktestRunDetailResponse)
def get_backtest(run_id: str, session: Session = Depends(get_session)):
    """Return a single backtest run by id."""
    try:
        run_uuid = uuid.UUID(run_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=f"Backtest run '{run_id}' not found.") from exc

    run = get_backtest_run(session, run_uuid)
    if run is None:
        raise HTTPException(status_code=404, detail=f"Backtest run '{run_id}' not found.")
    return _backtest_run_detail(run)


@app.patch("/api/v1/backtests/{run_id}", response_model=BacktestRunDetailResponse)
def patch_backtest(
    run_id: str,
    body: BacktestRunPatchRequest,
    session: Session = Depends(get_session),
):
    """Update bookmark state for a backtest run."""
    try:
        run_uuid = uuid.UUID(run_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=f"Backtest run '{run_id}' not found.") from exc

    try:
        run = update_backtest_run(session, run_uuid, is_saved=body.is_saved)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=f"Backtest run '{run_id}' not found.") from exc

    return _backtest_run_detail(run)


@app.delete("/api/v1/backtests/{run_id}", status_code=204)
def delete_backtest(run_id: str, session: Session = Depends(get_session)):
    """Delete a persisted backtest run from history."""
    try:
        run_uuid = uuid.UUID(run_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=f"Backtest run '{run_id}' not found.") from exc

    if not delete_backtest_run(session, run_uuid):
        raise HTTPException(status_code=404, detail=f"Backtest run '{run_id}' not found.")

    _delete_backtest_lake_artifacts(run_id)


@app.get(
    "/api/v1/backtests/{run_id}/artifacts/equity",
    response_model=BacktestEquityArtifactResponse,
)
def get_backtest_equity_artifact(run_id: str):
    """Return the persisted equity curve for a backtest run."""
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

    return {"run_id": run_id, "points": _serialize_equity_artifact(df)}


@app.get(
    "/api/v1/backtests/{run_id}/artifacts/trades",
    response_model=BacktestTradesArtifactResponse,
)
def get_backtest_trades_artifact(run_id: str):
    """Return the persisted closed trades for a backtest run."""
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

    return {"run_id": run_id, "trades": _serialize_trades_artifact(df)}


@app.post("/api/v1/optimize", response_model=OptimizationStartResponse)
def start_optimization(config: OptimizationConfig):
    """
    Launch an asynchronous Optuna optimization study and return its id.

    The run executes on a background worker; poll the status endpoint for
    progress and fetch results once the study is done.
    """
    try:
        job = optimization_jobs.start_job(
            config, market_data_service=market_data_service
        )
    except Exception as e:
        logger.error(f"Error starting optimization: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))
    return {"study_id": job.study_id, "status": job.status}


@app.get(
    "/api/v1/optimize/{study_id}", response_model=OptimizationStatusResponse
)
def get_optimization_status(study_id: str):
    """Return progress/status for an optimization study."""
    payload = optimization_jobs.get_status_payload(study_id)
    if payload is None:
        raise HTTPException(status_code=404, detail=f"Study '{study_id}' not found.")
    return payload


@app.get(
    "/api/v1/optimize/{study_id}/results",
    response_model=OptimizationResultsResponse,
)
def get_optimization_results(study_id: str):
    """Return full study results once the optimization has finished."""
    job = optimization_jobs.get_job(study_id)
    if job is not None:
        payload = optimization_jobs.results_payload(job)
        if payload is None:
            raise HTTPException(
                status_code=409,
                detail=f"Study '{study_id}' has no results yet (status: {job.status}).",
            )
        return payload

    payload = optimization_jobs.results_payload_from_db(study_id)
    if payload is None:
        persisted_status = optimization_jobs.get_persisted_study_status(study_id)
        if persisted_status is not None:
            raise HTTPException(
                status_code=409,
                detail=(
                    f"Study '{study_id}' has no results yet "
                    f"(status: {persisted_status})."
                ),
            )
        raise HTTPException(status_code=404, detail=f"Study '{study_id}' not found.")
    return payload


@app.get("/api/v1/optimizations", response_model=OptimizationStudyListResponse)
def list_optimizations(
    session: Session = Depends(get_session),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
):
    """Return a paginated list of optimization studies, newest first."""
    studies, total = list_optimization_studies(
        session, limit=limit, offset=offset
    )
    return {
        "items": [
            OptimizationStudyListItem(**optimization_jobs.study_list_item_from_db(study))
            for study in studies
        ],
        "total": total,
        "limit": limit,
        "offset": offset,
    }


@app.post("/api/v1/optimizations/bulk-delete", response_model=BulkDeleteResponse)
def bulk_delete_optimizations(
    body: BulkDeleteOptimizationsRequest,
    session: Session = Depends(get_session),
):
    """Delete multiple optimization studies in one request."""
    parsed_ids: list[uuid.UUID] = []
    not_found: list[str] = []
    for study_id in body.study_ids:
        try:
            parsed_ids.append(uuid.UUID(study_id))
        except ValueError:
            not_found.append(study_id)

    deleted_count, missing_ids = delete_optimization_studies(session, parsed_ids)
    not_found.extend(str(study_id) for study_id in missing_ids)
    missing_set = set(missing_ids)
    for parsed_id in parsed_ids:
        if parsed_id not in missing_set:
            optimization_jobs.evict_study(str(parsed_id))
    return {"deleted": deleted_count, "not_found": not_found}


@app.delete("/api/v1/optimizations/{study_id}", status_code=204)
def delete_optimization(study_id: str, session: Session = Depends(get_session)):
    """Delete a persisted optimization study from history."""
    try:
        study_uuid = uuid.UUID(study_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=f"Study '{study_id}' not found.") from exc

    if not delete_optimization_study(session, study_uuid):
        raise HTTPException(status_code=404, detail=f"Study '{study_id}' not found.")

    optimization_jobs.evict_study(study_id)


@app.post(
    "/api/v1/optimize/{study_id}/cancel", response_model=OptimizationStatusResponse
)
def cancel_optimization(study_id: str):
    """Request cancellation of a running optimization study.

    Cancels the live job if present; otherwise cancels an orphaned study left
    active in the DB by a previous process. A 404 only means the study does not
    exist anywhere.
    """
    optimization_jobs.request_cancel(study_id)
    payload = optimization_jobs.get_status_payload(study_id)
    if payload is None:
        raise HTTPException(status_code=404, detail=f"Study '{study_id}' not found.")
    return payload


@app.post("/api/v1/walkforward", response_model=WalkForwardStartResponse)
def start_walkforward(body: WalkForwardRequest):
    """Launch an asynchronous walk-forward analysis run."""
    try:
        job = walkforward_jobs.start_job(
            body, market_data_service=market_data_service
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except Exception as exc:
        logger.error("Error starting walk-forward run: %s", exc)
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    return {"run_id": job.run_id, "status": job.status}


@app.get(
    "/api/v1/walkforward/{run_id}",
    response_model=WalkForwardStatusResponse,
)
def get_walkforward_status(run_id: str):
    """Return progress/status for a walk-forward run."""
    payload = walkforward_jobs.get_status_payload(run_id)
    if payload is None:
        raise HTTPException(
            status_code=404, detail=f"Walk-forward run '{run_id}' not found."
        )
    return payload


@app.get(
    "/api/v1/walkforward/{run_id}/results",
    response_model=WalkForwardResultsResponse,
)
def get_walkforward_results(run_id: str):
    """Return full walk-forward results once the run has finished."""
    job = walkforward_jobs.get_job(run_id)
    if job is not None:
        payload = walkforward_jobs.results_payload(job)
        if payload is None:
            raise HTTPException(
                status_code=409,
                detail=(
                    f"Walk-forward run '{run_id}' has no results yet "
                    f"(status: {job.status})."
                ),
            )
        return payload

    payload = walkforward_jobs.results_payload_from_db(run_id)
    if payload is None:
        persisted_status = walkforward_jobs.get_persisted_run_status(run_id)
        if persisted_status is not None:
            raise HTTPException(
                status_code=409,
                detail=(
                    f"Walk-forward run '{run_id}' has no results yet "
                    f"(status: {persisted_status})."
                ),
            )
        raise HTTPException(
            status_code=404, detail=f"Walk-forward run '{run_id}' not found."
        )
    return payload


@app.post(
    "/api/v1/walkforward/{run_id}/cancel",
    response_model=WalkForwardStatusResponse,
)
def cancel_walkforward(run_id: str):
    """Request cancellation of a running walk-forward analysis.

    Cancels the live job if present; otherwise cancels an orphaned run left
    active in the DB by a previous process. A 404 only means the run does not
    exist anywhere.
    """
    walkforward_jobs.request_cancel(run_id)
    payload = walkforward_jobs.get_status_payload(run_id)
    if payload is None:
        raise HTTPException(
            status_code=404, detail=f"Walk-forward run '{run_id}' not found."
        )
    return payload


@app.get("/api/v1/walkforwards", response_model=WalkForwardRunListResponse)
def list_walkforwards(
    session: Session = Depends(get_session),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
):
    """Return a paginated list of walk-forward runs, newest first."""
    runs, total = list_walkforward_runs(session, limit=limit, offset=offset)
    return {
        "items": [
            WalkForwardRunListItem(**walkforward_jobs.run_list_item_from_db(run))
            for run in runs
        ],
        "total": total,
        "limit": limit,
        "offset": offset,
    }


@app.delete("/api/v1/walkforwards/{run_id}", status_code=204)
def delete_walkforward(run_id: str, session: Session = Depends(get_session)):
    """Delete a persisted walk-forward run and its lake artifacts."""
    try:
        run_uuid = uuid.UUID(run_id)
    except ValueError as exc:
        raise HTTPException(
            status_code=404, detail=f"Walk-forward run '{run_id}' not found."
        ) from exc

    if not delete_walkforward_run(session, run_uuid):
        raise HTTPException(
            status_code=404, detail=f"Walk-forward run '{run_id}' not found."
        )

    walkforward_jobs.evict_run(run_id)
    walkforward_jobs.delete_run_lake_artifacts(run_id)


@app.get(
    "/api/v1/walkforward/{run_id}/artifacts/equity",
    response_model=BacktestEquityArtifactResponse,
)
def get_walkforward_equity_artifact(run_id: str):
    """Return the stitched out-of-sample equity curve for a walk-forward run."""
    try:
        uuid.UUID(run_id)
    except ValueError as exc:
        raise HTTPException(
            status_code=404, detail=f"Walk-forward run '{run_id}' not found."
        ) from exc

    job = walkforward_jobs.get_job(run_id)
    if job is not None and job.result is not None:
        return {
            "run_id": run_id,
            "points": walkforward_jobs.serialize_equity_points(
                job.result.oos_equity_curve
            ),
        }

    try:
        df = read_walkforward_artifact(run_id, "oos_equity")
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    return {"run_id": run_id, "points": _serialize_equity_artifact(df)}


@app.post("/api/v1/strategy-search", response_model=StrategySearchStartResponse)
def start_strategy_search(body: StrategySearchConfig):
    """Launch an asynchronous strategy search run."""
    try:
        job = strategy_search_jobs.start_job(
            body, market_data_service=market_data_service
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except Exception as exc:
        logger.error("Error starting strategy search run: %s", exc)
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    return {"run_id": job.run_id, "status": job.status}


@app.get(
    "/api/v1/strategy-search/{run_id}",
    response_model=StrategySearchStatusResponse,
)
def get_strategy_search_status(run_id: str):
    """Return progress/status for a strategy search run."""
    payload = strategy_search_jobs.get_status_payload(run_id)
    if payload is None:
        raise HTTPException(
            status_code=404, detail=f"Strategy search run '{run_id}' not found."
        )
    return payload


@app.get(
    "/api/v1/strategy-search/{run_id}/results",
    response_model=StrategySearchResultsResponse,
)
def get_strategy_search_results(run_id: str):
    """Return full strategy search results once the run has finished."""
    job = strategy_search_jobs.get_job(run_id)
    if job is not None:
        payload = strategy_search_jobs.results_payload(job)
        if payload is None:
            raise HTTPException(
                status_code=409,
                detail=(
                    f"Strategy search run '{run_id}' has no results yet "
                    f"(status: {job.status})."
                ),
            )
        return payload

    payload = strategy_search_jobs.results_payload_from_db(run_id)
    if payload is None:
        persisted_status = strategy_search_jobs.get_persisted_run_status(run_id)
        if persisted_status is not None:
            raise HTTPException(
                status_code=409,
                detail=(
                    f"Strategy search run '{run_id}' has no results yet "
                    f"(status: {persisted_status})."
                ),
            )
        raise HTTPException(
            status_code=404, detail=f"Strategy search run '{run_id}' not found."
        )
    return payload


@app.post(
    "/api/v1/strategy-search/{run_id}/cancel",
    response_model=StrategySearchStatusResponse,
)
def cancel_strategy_search(run_id: str):
    """Request cancellation of a running strategy search.

    Cancels the live job if present; otherwise cancels an orphaned run left
    active in the DB by a previous process. The response reflects the current
    status, so a 404 only means the run does not exist anywhere.
    """
    strategy_search_jobs.request_cancel(run_id)
    payload = strategy_search_jobs.get_status_payload(run_id)
    if payload is None:
        raise HTTPException(
            status_code=404, detail=f"Strategy search run '{run_id}' not found."
        )
    return payload


@app.get("/api/v1/strategy-searches", response_model=StrategySearchRunListResponse)
def list_strategy_searches(
    session: Session = Depends(get_session),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
):
    """Return a paginated list of strategy search runs, newest first."""
    runs, total = list_strategy_search_runs(session, limit=limit, offset=offset)
    return {
        "items": [
            StrategySearchRunListItem(
                **strategy_search_jobs.run_list_item_from_db(run)
            )
            for run in runs
        ],
        "total": total,
        "limit": limit,
        "offset": offset,
    }


@app.delete("/api/v1/strategy-searches/{run_id}", status_code=204)
def delete_strategy_search(run_id: str, session: Session = Depends(get_session)):
    """Delete a persisted strategy search run and its lake artifacts."""
    try:
        run_uuid = uuid.UUID(run_id)
    except ValueError as exc:
        raise HTTPException(
            status_code=404, detail=f"Strategy search run '{run_id}' not found."
        ) from exc

    if not delete_strategy_search_run(session, run_uuid):
        raise HTTPException(
            status_code=404, detail=f"Strategy search run '{run_id}' not found."
        )

    strategy_search_jobs.evict_run(run_id)
    strategy_search_jobs.delete_run_lake_artifacts(run_id)


@app.get(
    "/api/v1/strategy-search/{run_id}/candidates/{candidate_id}/artifacts/equity",
    response_model=StrategySearchCandidateEquityArtifactResponse,
)
def get_strategy_search_candidate_equity_artifact(run_id: str, candidate_id: str):
    """Return stitched out-of-sample equity points for one search candidate."""
    try:
        uuid.UUID(run_id)
    except ValueError as exc:
        raise HTTPException(
            status_code=404, detail=f"Strategy search run '{run_id}' not found."
        ) from exc

    if not strategy_search_jobs.candidate_exists_in_run(run_id, candidate_id):
        raise HTTPException(
            status_code=404,
            detail=(
                f"Candidate '{candidate_id}' not found in strategy search run "
                f"'{run_id}'."
            ),
        )

    job = strategy_search_jobs.get_job(run_id)
    if job is not None and job.result is not None:
        candidate = next(
            (
                item
                for item in job.result.candidates
                if item.candidate_id == candidate_id
            ),
            None,
        )
        if candidate is not None and candidate.oos_equity_curve is not None:
            return {
                "run_id": run_id,
                "candidate_id": candidate_id,
                "points": strategy_search_jobs.serialize_equity_points(
                    candidate.oos_equity_curve
                ),
            }

    try:
        df = read_strategy_search_candidate_artifact(
            run_id, candidate_id, "oos_equity"
        )
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    return {
        "run_id": run_id,
        "candidate_id": candidate_id,
        "points": _serialize_equity_artifact(df),
    }


@app.get(
    "/api/v1/strategy-search/{run_id}/candidates/{candidate_id}/genome",
    response_model=StrategySearchCandidateGenomeResponse,
)
def get_strategy_search_candidate_genome(run_id: str, candidate_id: str):
    """Return the stored genome document for a genetic search candidate."""
    try:
        uuid.UUID(run_id)
    except ValueError as exc:
        raise HTTPException(
            status_code=404, detail=f"Strategy search run '{run_id}' not found."
        ) from exc

    if not strategy_search_jobs.candidate_exists_in_run(run_id, candidate_id):
        raise HTTPException(
            status_code=404,
            detail=(
                f"Candidate '{candidate_id}' not found in strategy search run "
                f"'{run_id}'."
            ),
        )

    genome = strategy_search_jobs.get_candidate_genome(run_id, candidate_id)
    if genome is None:
        raise HTTPException(
            status_code=404,
            detail=(
                f"Genome not available for candidate '{candidate_id}' "
                f"in strategy search run '{run_id}'."
            ),
        )

    return {
        "run_id": run_id,
        "candidate_id": candidate_id,
        "genome": genome,
    }


@app.get("/api/v1/news", response_model=List[NewsArticleResponse])
def get_news_articles():
    articles = []
    seen_urls = set()
    
    feeds = [
        {"url": "https://valor.globo.com/rss/valor/financas/", "source": "Valor Finanças"},
        {"url": "https://valor.globo.com/rss/valor/empresas/", "source": "Valor Empresas"},
        {"url": "https://valor.globo.com/rss/valor/agronegocios/", "source": "Valor Agro"},
        {"url": "https://www.cnbc.com/id/100003114/device/rss/rss.html", "source": "CNBC"}
    ]
    
    for feed in feeds:
        try:
            req = urllib.request.Request(
                feed["url"],
                headers={"User-Agent": "Mozilla/5.0"}
            )
            with urllib.request.urlopen(req, timeout=3) as response:
                xml_data = response.read()
                
            root = ET.fromstring(xml_data)
            items = root.findall(".//item")
            
            for item in items:
                title = item.find("title").text if item.find("title") is not None else ""
                link = item.find("link").text if item.find("link") is not None else ""
                description = item.find("description").text if item.find("description") is not None else ""
                pub_date_str = item.find("pubDate").text if item.find("pubDate") is not None else ""
                
                if not title or not link or link in seen_urls:
                    continue
                    
                seen_urls.add(link)
                
                # Extract image from description
                image_url = None
                img_match = re.search(r'<img[^>]+src=["\']([^"\']+)["\']', description)
                if img_match:
                    image_url = img_match.group(1)
                    
                # Clean description of HTML tags
                clean_description = re.sub(r'<img[^>]+>', '', description)
                clean_description = re.sub(r'<br\s*/?>', '', clean_description).strip()
                
                # Parse RFC 822 pubDate to ISO 8601 format
                try:
                    dt = email.utils.parsedate_to_datetime(pub_date_str)
                    pub_date_iso = dt.isoformat()
                except Exception:
                    pub_date_iso = datetime.now(timezone.utc).isoformat()
                    
                # Encode URL as a safe base64 ID
                article_id = base64.urlsafe_b64encode(link.encode("utf-8")).decode("utf-8")
                
                articles.append({
                    "id": article_id,
                    "title": title,
                    "source": feed["source"],
                    "publishedAt": pub_date_iso,
                    "summary": clean_description,
                    "content": clean_description,
                    "imageUrl": image_url,
                })
        except Exception as e:
            logger.error(f"Failed to fetch news from feed {feed['url']}: {e}")
            
    # Sort descending by publishedAt and take the top 25 articles
    articles.sort(key=lambda x: x["publishedAt"], reverse=True)
    return articles[:25]


@app.get("/api/v1/news/{article_id}", response_model=NewsArticleResponse)
def get_news_article(article_id: str):
    # Attempt to decode the base64 ID to get the URL
    try:
        url = base64.urlsafe_b64decode(article_id.encode("utf-8")).decode("utf-8")
    except Exception as exc:
        raise HTTPException(status_code=400, detail="Invalid article ID format.") from exc
        
    is_valor = "valor.globo.com" in url
    source_name = "Valor Econômico" if is_valor else "CNBC"
    
    # Try to fetch RSS metadata
    title = ""
    description = ""
    pub_date_iso = datetime.now(timezone.utc).isoformat()
    image_url = None
    
    feeds = [
        {"url": "https://valor.globo.com/rss/valor/financas/"},
        {"url": "https://valor.globo.com/rss/valor/empresas/"},
        {"url": "https://valor.globo.com/rss/valor/agronegocios/"},
        {"url": "https://www.cnbc.com/id/100003114/device/rss/rss.html"}
    ]
    
    for feed in feeds:
        try:
            req = urllib.request.Request(
                feed["url"],
                headers={"User-Agent": "Mozilla/5.0"}
            )
            with urllib.request.urlopen(req, timeout=4) as response:
                xml_data = response.read()
                
            root = ET.fromstring(xml_data)
            items = root.findall(".//item")
            found = False
            for item in items:
                link = item.find("link").text if item.find("link") is not None else ""
                if link == url:
                    title = item.find("title").text if item.find("title") is not None else ""
                    description = item.find("description").text if item.find("description") is not None else ""
                    pub_date_str = item.find("pubDate").text if item.find("pubDate") is not None else ""
                    
                    # Extract image from description
                    img_match = re.search(r'<img[^>]+src=["\']([^"\']+)["\']', description)
                    if img_match:
                        image_url = img_match.group(1)
                        
                    # Clean description
                    description = re.sub(r'<img[^>]+>', '', description)
                    description = re.sub(r'<br\s*/?>', '', description).strip()
                    
                    try:
                        dt = email.utils.parsedate_to_datetime(pub_date_str)
                        pub_date_iso = dt.isoformat()
                    except Exception:
                        pass
                    found = True
                    break
            if found:
                break
        except Exception as e:
            logger.error(f"Error checking RSS metadata during detail fetch: {e}")
            
    # Scrape the full article body
    content = ""
    try:
        req_art = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req_art, timeout=6) as response:
            html = response.read().decode("utf-8")
            
        import re
        
        # 1. First try parsing using Valor class if it is a Valor link
        if is_valor:
            paragraphs = re.findall(r"<p[^>]*content-text__container[^>]*>(.*?)</p>", html, re.DOTALL)
            clean_paragraphs = []
            for p in paragraphs:
                p_clean = re.sub(r"<[^>]+>", "", p).strip()
                p_clean = p_clean.replace("&nbsp;", " ").replace("&amp;", "&").replace("&quot;", '"').replace("&#39;", "'").replace("&apos;", "'")
                if len(p_clean) > 40:
                    clean_paragraphs.append(p_clean)
            if clean_paragraphs:
                content = "\n\n".join(clean_paragraphs)
                
        # 2. If it is not Valor or if Valor parsing yielded nothing, parse all <p> tags
        if not content:
            paragraphs = re.findall(r"<p[^>]*>(.*?)</p>", html, re.DOTALL)
            clean_paragraphs = []
            bad_words = ["livestream", "sign in", "create free account", "watch live", "privacy policy", "terms of service", "all rights reserved", "cnbc.com", "subscribe to", "inscreva-se", "todos os direitos reservados", "leia mais"]
            
            for p in paragraphs:
                p_clean = re.sub(r"<[^>]+>", "", p).strip()
                p_clean = p_clean.replace("&nbsp;", " ").replace("&amp;", "&").replace("&quot;", '"').replace("&#39;", "'").replace("&apos;", "'")
                
                if len(p_clean) > 85 and not any(bw in p_clean.lower() for bw in bad_words) and not any(x in p_clean for x in ["var ", "window.", "document.", "function()", "adsbygoogle"]):
                    clean_paragraphs.append(p_clean)
            if clean_paragraphs:
                content = "\n\n".join(clean_paragraphs)
                
        # Fallback search for og:image in scraped html
        if not image_url:
            og_img = re.search(r'<meta[^>]+property=["\']og:image["\'][^>]+content=["\']([^"\']+)["\']', html)
            if og_img:
                image_url = og_img.group(1)
            else:
                tw_img = re.search(r'<meta[^>]+name=["\']twitter:image["\'][^>]+content=["\']([^"\']+)["\']', html)
                if tw_img:
                    image_url = tw_img.group(1)
                
        if not title:
            title_match = re.search(r"<title>(.*?)</title>", html, re.IGNORECASE)
            if title_match:
                title = title_match.group(1).replace(" - CNBC", "").replace(" | Valor Econômico", "").strip()
    except Exception as e:
        logger.error(f"Failed to scrape article content for {url}: {e}")
        
    # Fallback if scraping failed or returned nothing
    if not content:
        content = description if description else "Unable to fetch article body. Please read the full article on the publisher website."
    if not title:
        title = "News Article"
        
    return {
        "id": article_id,
        "title": title,
        "source": source_name,
        "publishedAt": pub_date_iso,
        "summary": description if description else title,
        "content": content,
        "imageUrl": image_url,
    }


def run_dev():
    """Entry point for running the dev server via `uv run dev`"""
    import os
    import uvicorn
    from q_backend.storage.settings import get_settings

    # Try standard PORT env first, then get_settings().port, then fallback to 8000
    port_env = os.environ.get("PORT")
    if port_env:
        port = int(port_env)
    else:
        try:
            port = get_settings().port
        except Exception:
            port = 8000

    uvicorn.run(
        "q_backend.api.main:app",
        host="0.0.0.0",
        port=port,
        reload=True,
        reload_excludes=["data", ".venv", "**/__pycache__"],
    )
