import logging
from datetime import datetime, timedelta
from contextlib import asynccontextmanager
from typing import List, Optional
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware

from q_backend.market_data.service import MarketDataService
from q_backend.market_data.models import OHLCV, Tick

# Setup logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

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
