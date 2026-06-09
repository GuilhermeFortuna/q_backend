import logging
import uuid
from datetime import datetime, timedelta, timezone
from contextlib import asynccontextmanager
from typing import List, Optional, Dict, Any, Literal
from fastapi import Depends, FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from sqlalchemy.orm import Session

from q_backend.api.deps import get_session

from q_backend.market_data.service import MarketDataService
from q_backend.market_data.models import OHLCV, Tick
import pandas as pd
from q_backend.backtesting.factory import build_strategy
from q_backend.backtesting.chart_data import serialize_chart_data
from q_backend.backtesting.position_sizing import (
    PositionSizingConfig,
    build_position_sizer,
)
from q_backend.backtesting.engine import BacktestEngine, ParallelMode
from q_backend.market_data.clients.metatrader import _to_naive_local
from q_backend.optimization import OptimizationConfig
from q_backend.api import optimization_jobs
from q_backend.storage.db.engine import session_scope
from q_backend.storage.db.models import BacktestRun, RunStatus
from q_backend.storage.db.repositories import (
    create_backtest_config,
    create_backtest_run,
    delete_backtest_run,
    get_backtest_run,
    get_or_create_strategy,
    list_backtest_runs,
    list_optimization_studies,
    update_backtest_run,
)
from q_backend.storage.health import storage_status

# Setup logging
logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)


# Pydantic schemas for frontend compatibility
class StorageServiceStatus(BaseModel):
    status: Literal["ok", "error"]
    error: Optional[str] = None


class StorageStatusResponse(BaseModel):
    postgres: StorageServiceStatus
    redis: StorageServiceStatus


class SystemHealthResponse(BaseModel):
    status: str
    backendVersion: str
    dataLakeStatus: str
    lastSyncAt: str
    storageStatus: StorageStatusResponse


class InstrumentResponse(BaseModel):
    symbol: str
    name: str
    exchange: str
    assetClass: str


class MarketSnapshotResponse(BaseModel):
    symbol: str
    last: float
    changePct: float
    volume: int


class OhlcvBarResponse(BaseModel):
    timestamp: str
    open: float
    high: float
    low: float
    close: float
    volume: int


class OhlcvAvailableRangeResponse(BaseModel):
    symbol: str
    timeframe: str
    start: datetime
    end: datetime
    bar_count: int


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


class BacktestRunListItem(BaseModel):
    run_id: str
    symbol: str
    strategy: str
    timeframe: str
    status: str
    created_at: datetime
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
    backtest_config: Optional[Dict[str, Any]] = None
    optimization_config: Optional[Dict[str, Any]] = None


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


# Instantiate global service
market_data_service = MarketDataService()


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
    )


def _start_backtest_run(request: BacktestRequest) -> Optional[str]:
    try:
        config_dict = request.model_dump(mode="json")
        with session_scope() as session:
            get_or_create_strategy(session, name=request.strategy)
            bt_config = create_backtest_config(
                session,
                name=f"{request.symbol}-{request.timeframe}",
                config=config_dict,
            )
            run = create_backtest_run(
                session,
                backtest_config_id=bt_config.id,
                config=config_dict,
                status=RunStatus.RUNNING.value,
                started_at=datetime.now(timezone.utc),
            )
            return str(run.id)
    except Exception as exc:
        logger.warning("Failed to persist backtest run start: %s", exc)
        return None


def _finish_backtest_run(
    run_id: Optional[str],
    *,
    status: str,
    result_summary: Optional[Dict[str, Any]] = None,
    error_message: Optional[str] = None,
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
                finished_at=datetime.now(timezone.utc),
            )
    except Exception as exc:
        logger.warning("Failed to persist backtest run finish: %s", exc)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup: Connect to MetaTrader 5
    logger.info("Starting up API, connecting to MetaTrader 5...")
    success = market_data_service.initialize()
    if not success:
        logger.error("MetaTrader 5 terminal initialization failed on startup!")
    else:
        logger.info("MetaTrader 5 terminal initialized successfully on startup.")
    yield
    # Shutdown: Disconnect from MetaTrader 5
    logger.info("Shutting down API, disconnecting from MetaTrader 5...")
    market_data_service.shutdown()


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


@app.get("/")
def read_root():
    return {
        "status": "online",
        "service": "QuantLauncher Backend API",
        "mt5_connected": market_data_service.mt5_client._is_initialized,
    }


@app.get("/api/v1/market-data/symbol/{symbol}")
def get_symbol_info(symbol: str):
    """
    Get detailed information about a specific financial symbol.
    """
    try:
        info = market_data_service.mt5_client.get_symbol_info(symbol)
        if not info:
            raise HTTPException(
                status_code=404,
                detail=f"Symbol '{symbol}' not found or could not be selected.",
            )
        return info
    except ConnectionError as ce:
        raise HTTPException(status_code=503, detail=str(ce))
    except Exception as e:
        logger.error(f"Error fetching info for {symbol}: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/v1/market-data/ohlcv", response_model=List[OHLCV])
def get_ohlcv(
    symbol: str = Query(..., description="Financial instrument (e.g. EURUSD, AUDUSD)"),
    timeframe: str = Query(
        "M1", description="Candle timeframe (e.g. M1, M5, M15, H1, D1)"
    ),
    start: Optional[datetime] = Query(
        None, description="Start datetime (ISO-8601). Defaults to 1 day ago."
    ),
    end: Optional[datetime] = Query(
        None, description="End datetime (ISO-8601). Defaults to current time."
    ),
):
    """
    Get historical OHLCV data (bars) for a specified symbol.
    """
    if not start:
        start = datetime.now() - timedelta(days=1)
    if not end:
        end = datetime.now()

    start = _to_naive_local(start)
    end = _to_naive_local(end)

    if start >= end:
        raise HTTPException(
            status_code=400, detail="Start datetime must be before end datetime."
        )

    try:
        ohlcv_data = market_data_service.get_ohlcv(symbol, timeframe, start, end)
        return ohlcv_data
    except ValueError as ve:
        raise HTTPException(status_code=400, detail=str(ve))
    except ConnectionError as ce:
        raise HTTPException(status_code=503, detail=str(ce))
    except Exception as e:
        logger.error(f"Error fetching OHLCV for {symbol}: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/v1/market-data/ticks", response_model=List[Tick])
def get_ticks(
    symbol: str = Query(..., description="Financial instrument (e.g. EURUSD, AUDUSD)"),
    start: Optional[datetime] = Query(
        None, description="Start datetime (ISO-8601). Defaults to 1 hour ago."
    ),
    end: Optional[datetime] = Query(
        None, description="End datetime (ISO-8601). Defaults to current time."
    ),
):
    """
    Get tick market data for a specified symbol.
    """
    if not start:
        start = datetime.now() - timedelta(hours=1)
    if not end:
        end = datetime.now()

    if start >= end:
        raise HTTPException(
            status_code=400, detail="Start datetime must be before end datetime."
        )

    try:
        ticks_data = market_data_service.get_ticks(symbol, start, end)
        return ticks_data
    except ConnectionError as ce:
        raise HTTPException(status_code=503, detail=str(ce))
    except Exception as e:
        logger.error(f"Error fetching ticks for {symbol}: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/v1/system/health", response_model=SystemHealthResponse)
def get_system_health():
    """
    Exposes platform health telemetry.
    """
    is_connected = market_data_service.mt5_client._is_initialized
    return {
        "status": "healthy" if is_connected else "degraded",
        "backendVersion": "0.1.0",
        "dataLakeStatus": "online" if is_connected else "offline",
        "lastSyncAt": datetime.now().isoformat(),
        "storageStatus": storage_status(),
    }


@app.get("/api/v1/market/instruments", response_model=List[InstrumentResponse])
def get_market_instruments():
    """
    Retrieve available B3/Bovespa assets from local MT5 environment.
    """
    b3_symbols = [
        {
            "symbol": "PETR4",
            "name": "PETROBRAS PN N2",
            "exchange": "BOVESPA",
            "assetClass": "equity",
        },
        {
            "symbol": "VALE3",
            "name": "VALE ON NM",
            "exchange": "BOVESPA",
            "assetClass": "equity",
        },
        {
            "symbol": "ITUB4",
            "name": "ITAU UNIBANCO PN N1",
            "exchange": "BOVESPA",
            "assetClass": "equity",
        },
        {
            "symbol": "WIN$",
            "name": "IBOVESPA MINI",
            "exchange": "BMF",
            "assetClass": "future",
        },
        {
            "symbol": "WDO$",
            "name": "DOLAR MINI",
            "exchange": "BMF",
            "assetClass": "future",
        },
    ]

    connected = market_data_service.mt5_client._is_initialized
    if not connected:
        logger.warning("MT5 not connected, returning cached asset definitions.")
        return b3_symbols

    import MetaTrader5 as mt5

    active_symbols = []
    for item in b3_symbols:
        # Pre-select in MT5 window to ensure ticks are loaded
        if mt5.symbol_select(item["symbol"], True):
            active_symbols.append(item)
        else:
            logger.warning(f"Symbol '{item['symbol']}' could not be selected in MT5.")
            active_symbols.append(item)  # Fallback to return anyway

    return active_symbols


@app.get("/api/v1/market/symbols/search", response_model=List[InstrumentResponse])
def search_symbols(
    q: str = Query(..., description="Query to search symbols in MetaTrader 5")
):
    """
    Search for symbols available in the MT5 terminal matching a query.
    """
    if not q.strip():
        return []

    connected = market_data_service.mt5_client.connect()
    if not connected:
        raise HTTPException(status_code=503, detail="MetaTrader 5 terminal is offline.")

    try:
        raw_symbols = market_data_service.search_symbols(q.strip())
        results = []
        for s in raw_symbols[:50]:  # Limit to 50 results
            # Parse exchange from path
            path = s.get("path", "")
            path_parts = path.split("\\")
            exchange = path_parts[0] if path_parts else "BOVESPA"

            # Determine asset class
            asset_class = "equity"
            symbol_name = s.get("name", "")
            if "BMF" in path or "@" in symbol_name or "$" in symbol_name:
                asset_class = "future"
            elif "FX" in path or "Forex" in path:
                asset_class = "fx"

            results.append(
                {
                    "symbol": symbol_name,
                    "name": s.get("description") or symbol_name,
                    "exchange": exchange,
                    "assetClass": asset_class,
                }
            )
        return results
    except Exception as e:
        logger.error(f"Error searching symbols for query '{q}': {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/v1/market/snapshot/{symbol}", response_model=MarketSnapshotResponse)
def get_market_snapshot(symbol: str):
    """
    Retrieve real-time price snapshot for a B3 asset using MT5.
    """
    import MetaTrader5 as mt5

    symbol = symbol.upper()

    connected = market_data_service.mt5_client.connect()
    if not connected:
        raise HTTPException(status_code=503, detail="MetaTrader 5 terminal is offline.")

    if not mt5.symbol_select(symbol, True):
        raise HTTPException(
            status_code=404, detail=f"Symbol '{symbol}' not found on MetaTrader 5."
        )

    tick = mt5.symbol_info_tick(symbol)
    if not tick:
        # Fallback to copy_rates if market is closed/inactive
        rates = mt5.copy_rates_from_pos(symbol, mt5.TIMEFRAME_M1, 0, 1)
        if rates is not None and len(rates) > 0:
            last_price = float(rates[0]["close"])
            volume = int(rates[0]["tick_volume"])
        else:
            last_price = 0.0
            volume = 0
    else:
        last_price = float(tick.last) if tick.last > 0 else float(tick.bid)
        volume = int(tick.volume)

    # Calculate change percentage from daily close
    rates_d1 = mt5.copy_rates_from_pos(symbol, mt5.TIMEFRAME_D1, 0, 2)
    change_pct = 0.0
    if rates_d1 is not None and len(rates_d1) >= 2:
        prev_close = float(rates_d1[0]["close"])
        current_close = float(rates_d1[-1]["close"])
        if prev_close > 0:
            change_pct = ((current_close - prev_close) / prev_close) * 100
    elif rates_d1 is not None and len(rates_d1) == 1:
        open_price = float(rates_d1[0]["open"])
        if open_price > 0 and last_price > 0:
            change_pct = ((last_price - open_price) / open_price) * 100

    return {
        "symbol": symbol,
        "last": last_price,
        "changePct": change_pct,
        "volume": volume,
    }


def _normalize_market_timeframe(timeframe: str) -> str:
    """Map UI-style timeframe labels to MT5 codes."""
    mapping = {
        "1M": "M1",
        "M1": "M1",
        "5M": "M5",
        "M5": "M5",
        "15M": "M15",
        "M15": "M15",
        "30M": "M30",
        "M30": "M30",
        "1H": "H1",
        "H1": "H1",
        "4H": "H4",
        "H4": "H4",
        "1D": "D1",
        "D1": "D1",
    }
    return mapping.get(timeframe.upper(), "D1")


def _ohlcv_to_bar_response(row) -> dict:
    """Convert OHLCV model or MT5 rate row to frontend bar shape."""
    if hasattr(row, "time"):
        timestamp_dt = row.time
        if not isinstance(timestamp_dt, datetime):
            timestamp_dt = datetime.fromisoformat(
                str(timestamp_dt).replace("Z", "+00:00")
            )
        volume = row.real_volume if row.real_volume > 0 else row.tick_volume
        return {
            "timestamp": timestamp_dt.isoformat().replace("+00:00", "Z"),
            "open": float(row.open),
            "high": float(row.high),
            "low": float(row.low),
            "close": float(row.close),
            "volume": int(volume),
        }

    timestamp_dt = datetime.fromtimestamp(int(row["time"]))
    return {
        "timestamp": timestamp_dt.isoformat() + "Z",
        "open": float(row["open"]),
        "high": float(row["high"]),
        "low": float(row["low"]),
        "close": float(row["close"]),
        "volume": (
            int(row["real_volume"])
            if row["real_volume"] > 0
            else int(row["tick_volume"])
        ),
    }


@app.get("/api/v1/market/ohlcv/{symbol}", response_model=List[OhlcvBarResponse])
def get_market_ohlcv(
    symbol: str,
    timeframe: str = Query(
        "D1", description="Candle timeframe (e.g. M1, M5, M15, M30, H1, H4, D1)"
    ),
    count: int = Query(
        500, ge=1, le=5000, description="Number of most recent bars to return"
    ),
    start: Optional[datetime] = Query(
        None, description="Start datetime (ISO-8601). Requires end."
    ),
    end: Optional[datetime] = Query(
        None, description="End datetime (ISO-8601). Requires start."
    ),
):
    """
    Retrieve historical OHLCV data for a B3 asset.
    Returns the most recent `count` bars by default, or a date range when start/end are provided.
    """
    import MetaTrader5 as mt5

    symbol = symbol.upper()
    mt5_timeframe = _normalize_market_timeframe(timeframe)

    connected = market_data_service.mt5_client.connect()
    if not connected:
        raise HTTPException(status_code=503, detail="MetaTrader 5 terminal is offline.")

    if not mt5.symbol_select(symbol, True):
        raise HTTPException(
            status_code=404, detail=f"Symbol '{symbol}' not found on MetaTrader 5."
        )

    if (start is None) ^ (end is None):
        raise HTTPException(
            status_code=400,
            detail="Both start and end must be provided for date-range queries.",
        )
    if start is not None and end is not None:
        start = _to_naive_local(start)
        end = _to_naive_local(end)
    if start is not None and end is not None and start >= end:
        raise HTTPException(
            status_code=400, detail="Start datetime must be before end datetime."
        )

    if start is not None and end is not None:
        try:
            ohlcv_data = market_data_service.get_ohlcv(
                symbol, mt5_timeframe, start, end
            )
        except ValueError as ve:
            raise HTTPException(status_code=400, detail=str(ve))
        except ConnectionError as ce:
            raise HTTPException(status_code=503, detail=str(ce))
        except Exception as e:
            logger.error(f"Error fetching OHLCV range for {symbol}: {str(e)}")
            raise HTTPException(status_code=500, detail=str(e))

        if not ohlcv_data:
            raise HTTPException(
                status_code=404, detail=f"No OHLCV data found for symbol '{symbol}'."
            )

        return [_ohlcv_to_bar_response(row) for row in ohlcv_data]

    timeframe_map = {
        "M1": mt5.TIMEFRAME_M1,
        "M5": mt5.TIMEFRAME_M5,
        "M15": mt5.TIMEFRAME_M15,
        "M30": mt5.TIMEFRAME_M30,
        "H1": mt5.TIMEFRAME_H1,
        "H4": mt5.TIMEFRAME_H4,
        "D1": mt5.TIMEFRAME_D1,
    }

    mt5_tf = timeframe_map.get(mt5_timeframe, mt5.TIMEFRAME_D1)
    rates = mt5.copy_rates_from_pos(symbol, mt5_tf, 0, count)
    if rates is None or len(rates) == 0:
        raise HTTPException(
            status_code=404, detail=f"No OHLCV data found for symbol '{symbol}'."
        )

    return [_ohlcv_to_bar_response(row) for row in rates]


@app.get(
    "/api/v1/market/ohlcv/{symbol}/available-range",
    response_model=OhlcvAvailableRangeResponse,
)
def get_market_ohlcv_available_range(
    symbol: str,
    timeframe: str = Query(
        "D1", description="Candle timeframe (e.g. M1, M5, M15, H1, D1)"
    ),
):
    """
    Returns the earliest and latest OHLCV bar timestamps available in MT5 for a symbol.
    """
    symbol = symbol.upper()
    mt5_timeframe = _normalize_market_timeframe(timeframe)

    connected = market_data_service.mt5_client.connect()
    if not connected:
        raise HTTPException(status_code=503, detail="MetaTrader 5 terminal is offline.")

    try:
        available_range = market_data_service.get_available_ohlcv_range(
            symbol, mt5_timeframe
        )
    except ValueError as ve:
        raise HTTPException(status_code=400, detail=str(ve))
    except ConnectionError as ce:
        raise HTTPException(status_code=503, detail=str(ce))
    except Exception as e:
        logger.error(f"Error probing OHLCV history for {symbol}: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))

    if available_range is None:
        raise HTTPException(
            status_code=404,
            detail=f"No OHLCV history found for symbol '{symbol}' on timeframe '{mt5_timeframe}'.",
        )

    return {
        "symbol": available_range.symbol,
        "timeframe": available_range.timeframe,
        "start": available_range.start,
        "end": available_range.end,
        "bar_count": available_range.bar_count,
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
        df.index = pd.to_datetime(df.index)

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
        sizer = build_position_sizer(request.position_sizing)

        # 4. Run Engine
        engine = BacktestEngine(
            strategy,
            sizer,
            initial_capital=request.initial_capital,
            point_values={request.symbol: request.point_value},
        )
        registry = engine.run(df, parallel_mode=ParallelMode.SEQUENTIAL)

        # 5. Extract Results
        metrics = registry.get_performance_metrics(request.initial_capital)
        closed_trades = [t.model_dump() for t in registry.get_closed_trades()]

        _finish_backtest_run(
            run_id,
            status=RunStatus.COMPLETED.value,
            result_summary=metrics,
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


@app.get("/api/v1/backtests", response_model=BacktestRunListResponse)
def list_backtests(
    session: Session = Depends(get_session),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    symbol: Optional[str] = None,
):
    """Return a paginated list of backtest runs, newest first."""
    runs, total = list_backtest_runs(
        session, limit=limit, offset=offset, symbol=symbol
    )
    return {
        "items": [_backtest_run_list_item(run) for run in runs],
        "total": total,
        "limit": limit,
        "offset": offset,
    }


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


@app.delete("/api/v1/backtests/{run_id}", status_code=204)
def delete_backtest(run_id: str, session: Session = Depends(get_session)):
    """Delete a persisted backtest run from history."""
    try:
        run_uuid = uuid.UUID(run_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=f"Backtest run '{run_id}' not found.") from exc

    if not delete_backtest_run(session, run_uuid):
        raise HTTPException(status_code=404, detail=f"Backtest run '{run_id}' not found.")


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


@app.post(
    "/api/v1/optimize/{study_id}/cancel", response_model=OptimizationStatusResponse
)
def cancel_optimization(study_id: str):
    """Request cancellation of a running optimization study."""
    job = optimization_jobs.request_cancel(study_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"Study '{study_id}' not found.")
    payload = optimization_jobs.get_status_payload(study_id)
    if payload is None:
        raise HTTPException(status_code=404, detail=f"Study '{study_id}' not found.")
    return payload


def run_dev():
    """Entry point for running the dev server via `uv run dev`"""
    import uvicorn

    uvicorn.run("q_backend.api.main:app", host="0.0.0.0", port=8000, reload=True)
