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
from q_backend.backtesting.strategy_registry import (
    StrategiesResponse,
    list_registered_strategies,
)
from q_backend.backtesting.position_sizing import (
    FixedQuantityPositionSizing,
    PositionSizingConfig,
    build_position_sizer,
)
from q_backend.backtesting.engine import BacktestEngine, ParallelMode
from q_backend.backtesting.tick.chart_data import serialize_tick_chart_data
from q_backend.backtesting.tick.engine import TickBacktestEngine
from q_backend.backtesting.tick.factory import build_tick_strategy
from q_backend.backtesting.tick.strategy import TickArrays
from q_backend.market_data.clients.metatrader import _to_naive_local
import MetaTrader5 as mt5
from q_backend.market_data.timezone import mt5_datetime_to_utc_iso, unix_seconds_to_utc_iso
from q_backend.optimization import OptimizationConfig
from q_backend.api import optimization_jobs
from q_backend.storage.db.engine import session_scope
from q_backend.storage.db.models import BacktestRun, RunStatus
from q_backend.storage.db.repositories import (
    create_backtest_config,
    create_backtest_run,
    delete_backtest_run,
    delete_backtest_runs,
    delete_optimization_study,
    delete_optimization_studies,
    find_backtest_run_by_config,
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
    bid: float = 0.0
    ask: float = 0.0
    spread: float = 0.0
    changeAbs: float = 0.0
    dayOpen: float = 0.0
    dayHigh: float = 0.0
    dayLow: float = 0.0
    prevClose: float = 0.0
    digits: int = 0
    tickTime: Optional[str] = None


class MarketSnapshotsResponse(BaseModel):
    snapshots: List[MarketSnapshotResponse]


class MarketTapeTickResponse(BaseModel):
    timestamp: str
    bid: float
    ask: float
    last: float
    volume: float
    side: Optional[Literal["buy", "sell"]] = None


class MarketTicksResponse(BaseModel):
    ticks: List[MarketTapeTickResponse]


class InstrumentInfoResponse(BaseModel):
    symbol: str
    description: str
    exchange: str
    currencyBase: str
    currencyProfit: str
    digits: int
    point: float
    tickSize: float
    tickValue: float
    contractSize: float
    volumeMin: float
    volumeMax: float
    volumeStep: float
    spreadFloating: bool


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
    start: str
    end: str
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


class BulkDeleteBacktestsRequest(BaseModel):
    run_ids: List[str]


class BulkDeleteOptimizationsRequest(BaseModel):
    study_ids: List[str]


class BulkDeleteResponse(BaseModel):
    deleted: int
    not_found: List[str]


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
    if tick_flags is None or tick_flags.lower() == "all":
        return mt5.COPY_TICKS_ALL
    if tick_flags.lower() == "trade":
        return mt5.COPY_TICKS_TRADE
    raise ValueError(
        f"Invalid tick_flags '{tick_flags}'. Expected 'all' or 'trade'."
    )


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


def _utc_iso_seconds(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    else:
        dt = dt.astimezone(timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _utc_iso_milliseconds(time_msc: int) -> str:
    seconds = time_msc // 1000
    millis = time_msc % 1000
    base = unix_seconds_to_utc_iso(seconds)
    return f"{base[:-1]}.{millis:03d}Z"


def _build_market_snapshot(symbol: str) -> Optional[dict]:
    """
    Build a market snapshot dict for a resolvable symbol, or None when the symbol
    cannot be selected in MT5.
    """
    import MetaTrader5 as mt5

    symbol = symbol.upper()
    if not mt5.symbol_select(symbol, True):
        return None

    sym_info = mt5.symbol_info(symbol)
    digits = int(sym_info.digits) if sym_info is not None else 0

    tick = mt5.symbol_info_tick(symbol)
    bid = 0.0
    ask = 0.0
    spread = 0.0
    tick_time: Optional[str] = None
    last_price = 0.0
    volume = 0

    if tick:
        bid = float(tick.bid)
        ask = float(tick.ask)
        spread = ask - bid
        last_price = float(tick.last) if tick.last > 0 else float(tick.bid)
        volume = int(tick.volume)
        if tick.time:
            tick_time = unix_seconds_to_utc_iso(int(tick.time))
    else:
        rates = mt5.copy_rates_from_pos(symbol, mt5.TIMEFRAME_M1, 0, 1)
        if rates is not None and len(rates) > 0:
            last_price = float(rates[0]["close"])
            bid = last_price
            ask = last_price
            spread = 0.0
            volume = int(rates[0]["tick_volume"])

    rates_d1 = mt5.copy_rates_from_pos(symbol, mt5.TIMEFRAME_D1, 0, 2)
    change_pct = 0.0
    change_abs = 0.0
    day_open = 0.0
    day_high = 0.0
    day_low = 0.0
    prev_close = 0.0

    if rates_d1 is not None and len(rates_d1) >= 2:
        prev_close = float(rates_d1[0]["close"])
        current_bar = rates_d1[-1]
        day_open = float(current_bar["open"])
        day_high = float(current_bar["high"])
        day_low = float(current_bar["low"])
        current_close = float(current_bar["close"])
        if prev_close > 0:
            change_pct = ((current_close - prev_close) / prev_close) * 100
        if last_price > 0:
            change_abs = last_price - prev_close
    elif rates_d1 is not None and len(rates_d1) == 1:
        bar = rates_d1[0]
        day_open = float(bar["open"])
        day_high = float(bar["high"])
        day_low = float(bar["low"])
        open_price = day_open
        if open_price > 0 and last_price > 0:
            change_pct = ((last_price - open_price) / open_price) * 100
            change_abs = last_price - open_price

    return {
        "symbol": symbol,
        "last": last_price,
        "changePct": change_pct,
        "volume": volume,
        "bid": bid,
        "ask": ask,
        "spread": spread,
        "changeAbs": change_abs,
        "dayOpen": day_open,
        "dayHigh": day_high,
        "dayLow": day_low,
        "prevClose": prev_close,
        "digits": digits,
        "tickTime": tick_time,
    }


def _tick_side(flags: int) -> Optional[str]:
    import MetaTrader5 as mt5

    buy = bool(flags & mt5.TICK_FLAG_BUY)
    sell = bool(flags & mt5.TICK_FLAG_SELL)
    if buy and not sell:
        return "buy"
    if sell and not buy:
        return "sell"
    return None


def _is_trade_tick(tick: Tick) -> bool:
    import MetaTrader5 as mt5

    flags = tick.flags or 0
    has_side = bool(flags & (mt5.TICK_FLAG_BUY | mt5.TICK_FLAG_SELL))
    return (tick.last or 0.0) > 0 or has_side


def _format_tape_ticks(raw_ticks: List[Tick]) -> List[dict]:
    trade_ticks = [tick for tick in raw_ticks if _is_trade_tick(tick)]
    selected = trade_ticks if trade_ticks else raw_ticks

    formatted: List[dict] = []
    for tick in selected:
        time_msc = tick.time_msc or 0
        timestamp = (
            _utc_iso_milliseconds(time_msc)
            if time_msc > 0
            else mt5_datetime_to_utc_iso(tick.time)
        )
        formatted.append(
            {
                "timestamp": timestamp,
                "bid": float(tick.bid),
                "ask": float(tick.ask),
                "last": float(tick.last or 0.0),
                "volume": float(tick.volume or 0.0),
                "side": _tick_side(tick.flags or 0),
            }
        )
    return formatted


def _symbol_info_to_instrument_response(symbol: str, info: Dict[str, Any]) -> dict:
    path = info.get("path", "") or ""
    path_parts = path.split("\\")
    exchange = path_parts[0] if path_parts else ""

    return {
        "symbol": symbol,
        "description": info.get("description") or symbol,
        "exchange": exchange,
        "currencyBase": info.get("currency_base") or "",
        "currencyProfit": info.get("currency_profit") or "",
        "digits": int(info.get("digits") or 0),
        "point": float(info.get("point") or 0.0),
        "tickSize": float(info.get("trade_tick_size") or 0.0),
        "tickValue": float(info.get("trade_tick_value") or 0.0),
        "contractSize": float(info.get("trade_contract_size") or 0.0),
        "volumeMin": float(info.get("volume_min") or 0.0),
        "volumeMax": float(info.get("volume_max") or 0.0),
        "volumeStep": float(info.get("volume_step") or 0.0),
        "spreadFloating": bool(info.get("spread_float")),
    }


@app.get("/api/v1/market/snapshot/{symbol}", response_model=MarketSnapshotResponse)
def get_market_snapshot(symbol: str):
    """
    Retrieve real-time price snapshot for a B3 asset using MT5.
    """
    symbol = symbol.upper()

    connected = market_data_service.mt5_client.connect()
    if not connected:
        raise HTTPException(status_code=503, detail="MetaTrader 5 terminal is offline.")

    snapshot = _build_market_snapshot(symbol)
    if snapshot is None:
        raise HTTPException(
            status_code=404, detail=f"Symbol '{symbol}' not found on MetaTrader 5."
        )
    return snapshot


@app.get("/api/v1/market/snapshots", response_model=MarketSnapshotsResponse)
def get_market_snapshots(
    symbols: str = Query(..., description="Comma-separated symbols (max 50)"),
):
    """
    Retrieve real-time price snapshots for multiple symbols in one MT5 session.
    Unknown symbols are silently skipped.
    """
    symbol_list = [item.strip().upper() for item in symbols.split(",") if item.strip()]
    if len(symbol_list) > 50:
        raise HTTPException(
            status_code=422,
            detail="At most 50 symbols are allowed per batch snapshot request.",
        )

    connected = market_data_service.mt5_client.connect()
    if not connected:
        raise HTTPException(status_code=503, detail="MetaTrader 5 terminal is offline.")

    snapshots = []
    for symbol in symbol_list:
        snapshot = _build_market_snapshot(symbol)
        if snapshot is not None:
            snapshots.append(snapshot)

    return {"snapshots": snapshots}


@app.get("/api/v1/market/ticks/{symbol}", response_model=MarketTicksResponse)
def get_market_ticks(
    symbol: str,
    limit: int = Query(200, ge=1, le=1000, description="Number of recent ticks"),
):
    """
    Retrieve recent time-and-sales ticks for a symbol (newest last).
    """
    symbol = symbol.upper()

    connected = market_data_service.mt5_client.connect()
    if not connected:
        raise HTTPException(status_code=503, detail="MetaTrader 5 terminal is offline.")

    try:
        raw_ticks = market_data_service.get_recent_ticks(symbol, limit)
    except ConnectionError as ce:
        raise HTTPException(status_code=503, detail=str(ce))

    if not raw_ticks:
        info = market_data_service.mt5_client.get_symbol_info(symbol)
        if not info:
            raise HTTPException(
                status_code=404, detail=f"Symbol '{symbol}' not found on MetaTrader 5."
            )
        return {"ticks": []}

    return {"ticks": _format_tape_ticks(raw_ticks[-limit:])}


@app.get(
    "/api/v1/market/instrument-info/{symbol}",
    response_model=InstrumentInfoResponse,
)
def get_market_instrument_info(symbol: str):
    """
    Retrieve curated contract specification fields for a symbol.
    """
    symbol = symbol.upper()

    connected = market_data_service.mt5_client.connect()
    if not connected:
        raise HTTPException(status_code=503, detail="MetaTrader 5 terminal is offline.")

    try:
        info = market_data_service.mt5_client.get_symbol_info(symbol)
    except ConnectionError as ce:
        raise HTTPException(status_code=503, detail=str(ce))

    if not info:
        raise HTTPException(
            status_code=404, detail=f"Symbol '{symbol}' not found on MetaTrader 5."
        )

    return _symbol_info_to_instrument_response(symbol, info)


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
            "timestamp": mt5_datetime_to_utc_iso(timestamp_dt),
            "open": float(row.open),
            "high": float(row.high),
            "low": float(row.low),
            "close": float(row.close),
            "volume": int(volume),
        }

    return {
        "timestamp": unix_seconds_to_utc_iso(int(row["time"])),
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
        "start": mt5_datetime_to_utc_iso(available_range.start),
        "end": mt5_datetime_to_utc_iso(available_range.end),
        "bar_count": available_range.bar_count,
    }


@app.get("/api/v1/strategies", response_model=StrategiesResponse)
def list_strategies():
    """Return registered strategy metadata and parameter schemas."""
    return {"strategies": list_registered_strategies()}


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
            day_trade=request.day_trade,
            day_trade_start_time=request.day_trade_start_time,
            day_trade_end_time=request.day_trade_end_time,
            day_trade_close_time=request.day_trade_close_time,
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

    uvicorn.run("q_backend.api.main:app", host="0.0.0.0", port=port, reload=True)
