import logging
from datetime import datetime, timedelta
from contextlib import asynccontextmanager
from typing import List, Optional
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from q_backend.market_data.service import MarketDataService
from q_backend.market_data.models import OHLCV, Tick

# Setup logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

# Pydantic schemas for frontend compatibility
class SystemHealthResponse(BaseModel):
    status: str
    backendVersion: str
    dataLakeStatus: str
    lastSyncAt: str

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

# Instantiate global service
market_data_service = MarketDataService()

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
    lifespan=lifespan
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
        "mt5_connected": market_data_service.mt5_client._is_initialized
    }

@app.get("/api/v1/market-data/symbol/{symbol}")
def get_symbol_info(symbol: str):
    """
    Get detailed information about a specific financial symbol.
    """
    try:
        info = market_data_service.mt5_client.get_symbol_info(symbol)
        if not info:
            raise HTTPException(status_code=404, detail=f"Symbol '{symbol}' not found or could not be selected.")
        return info
    except ConnectionError as ce:
        raise HTTPException(status_code=503, detail=str(ce))
    except Exception as e:
        logger.error(f"Error fetching info for {symbol}: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/v1/market-data/ohlcv", response_model=List[OHLCV])
def get_ohlcv(
    symbol: str = Query(..., description="Financial instrument (e.g. EURUSD, AUDUSD)"),
    timeframe: str = Query("M1", description="Candle timeframe (e.g. M1, M5, M15, H1, D1)"),
    start: Optional[datetime] = Query(None, description="Start datetime (ISO-8601). Defaults to 1 day ago."),
    end: Optional[datetime] = Query(None, description="End datetime (ISO-8601). Defaults to current time.")
):
    """
    Get historical OHLCV data (bars) for a specified symbol.
    """
    if not start:
        start = datetime.now() - timedelta(days=1)
    if not end:
        end = datetime.now()

    if start >= end:
        raise HTTPException(status_code=400, detail="Start datetime must be before end datetime.")

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
    start: Optional[datetime] = Query(None, description="Start datetime (ISO-8601). Defaults to 1 hour ago."),
    end: Optional[datetime] = Query(None, description="End datetime (ISO-8601). Defaults to current time.")
):
    """
    Get tick market data for a specified symbol.
    """
    if not start:
        start = datetime.now() - timedelta(hours=1)
    if not end:
        end = datetime.now()

    if start >= end:
        raise HTTPException(status_code=400, detail="Start datetime must be before end datetime.")

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
        "lastSyncAt": datetime.now().isoformat()
    }


@app.get("/api/v1/market/instruments", response_model=List[InstrumentResponse])
def get_market_instruments():
    """
    Retrieve available B3/Bovespa assets from local MT5 environment.
    """
    b3_symbols = [
        {"symbol": "PETR4", "name": "PETROBRAS PN N2", "exchange": "BOVESPA", "assetClass": "equity"},
        {"symbol": "VALE3", "name": "VALE ON NM", "exchange": "BOVESPA", "assetClass": "equity"},
        {"symbol": "ITUB4", "name": "ITAU UNIBANCO PN N1", "exchange": "BOVESPA", "assetClass": "equity"},
        {"symbol": "WIN$", "name": "IBOVESPA MINI", "exchange": "BMF", "assetClass": "future"},
        {"symbol": "WDO$", "name": "DOLAR MINI", "exchange": "BMF", "assetClass": "future"},
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
            active_symbols.append(item) # Fallback to return anyway
            
    return active_symbols


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
        raise HTTPException(status_code=404, detail=f"Symbol '{symbol}' not found on MetaTrader 5.")
        
    tick = mt5.symbol_info_tick(symbol)
    if not tick:
        # Fallback to copy_rates if market is closed/inactive
        rates = mt5.copy_rates_from_pos(symbol, mt5.TIMEFRAME_M1, 0, 1)
        if rates is not None and len(rates) > 0:
            last_price = float(rates[0]['close'])
            volume = int(rates[0]['tick_volume'])
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
        prev_close = float(rates_d1[0]['close'])
        current_close = float(rates_d1[-1]['close'])
        if prev_close > 0:
            change_pct = ((current_close - prev_close) / prev_close) * 100
    elif rates_d1 is not None and len(rates_d1) == 1:
        open_price = float(rates_d1[0]['open'])
        if open_price > 0 and last_price > 0:
            change_pct = ((last_price - open_price) / open_price) * 100
            
    return {
        "symbol": symbol,
        "last": last_price,
        "changePct": change_pct,
        "volume": volume
    }


@app.get("/api/v1/market/ohlcv/{symbol}", response_model=List[OhlcvBarResponse])
def get_market_ohlcv(symbol: str):
    """
    Retrieve historical OHLCV data (30 sessions) for B3 asset.
    """
    import MetaTrader5 as mt5
    symbol = symbol.upper()
    
    connected = market_data_service.mt5_client.connect()
    if not connected:
        raise HTTPException(status_code=503, detail="MetaTrader 5 terminal is offline.")
        
    if not mt5.symbol_select(symbol, True):
        raise HTTPException(status_code=404, detail=f"Symbol '{symbol}' not found on MetaTrader 5.")
        
    rates = mt5.copy_rates_from_pos(symbol, mt5.TIMEFRAME_D1, 0, 30)
    if rates is None or len(rates) == 0:
        raise HTTPException(status_code=404, detail=f"No OHLCV data found for symbol '{symbol}'.")
        
    ohlcv_bars = []
    for row in rates:
        timestamp_dt = datetime.fromtimestamp(int(row['time']))
        ohlcv_bars.append({
            "timestamp": timestamp_dt.isoformat() + "Z",
            "open": float(row['open']),
            "high": float(row['high']),
            "low": float(row['low']),
            "close": float(row['close']),
            "volume": int(row['real_volume']) if row['real_volume'] > 0 else int(row['tick_volume'])
        })
        
    return ohlcv_bars

