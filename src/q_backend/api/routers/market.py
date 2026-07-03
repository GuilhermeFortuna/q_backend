import logging
from datetime import datetime, timedelta
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query

from q_backend.api.dependencies import _require_mt5_live, get_market_data_service
from q_backend.api.schemas.market import (
    InstrumentInfoResponse,
    InstrumentResponse,
    MarketSnapshotResponse,
    MarketSnapshotsResponse,
    MarketTicksResponse,
    OhlcvAvailableRangeResponse,
    OhlcvBarResponse,
)
from q_backend.market_data import api_service as market_service
from q_backend.market_data.clients.metatrader import _to_naive_local
from q_backend.market_data.models import OHLCV, Tick
from q_backend.market_data.service import MarketDataService
from q_backend.market_data.timezone import mt5_datetime_to_utc_iso
from q_backend.storage.runtime_config import get_data_source

logger = logging.getLogger(__name__)

router = APIRouter(tags=["market"])


@router.get("/api/v1/market-data/symbol/{symbol}")
def get_symbol_info(
    symbol: str,
    mds: MarketDataService = Depends(get_market_data_service),
):
    """
    Get detailed information about a specific financial symbol.
    """
    try:
        info = mds.get_symbol_info(symbol)
        if not info:
            raise HTTPException(
                status_code=404,
                detail=f"Symbol '{symbol}' not found or could not be selected.",
            )
        return info
    except ConnectionError as ce:
        raise HTTPException(status_code=503, detail=str(ce))
    except Exception as e:  # noqa: BLE001 - surfaced to client as HTTPException; logged
        logger.error(f"Error fetching info for {symbol}: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/api/v1/market-data/ohlcv", response_model=List[OHLCV])
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
    mds: MarketDataService = Depends(get_market_data_service),
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
        ohlcv_data = mds.get_ohlcv(symbol, timeframe, start, end)
        return ohlcv_data
    except ValueError as ve:
        raise HTTPException(status_code=400, detail=str(ve))
    except ConnectionError as ce:
        raise HTTPException(status_code=503, detail=str(ce))
    except Exception as e:  # noqa: BLE001 - surfaced to client as HTTPException; logged
        logger.error(f"Error fetching OHLCV for {symbol}: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/api/v1/market-data/ticks", response_model=List[Tick])
def get_ticks(
    symbol: str = Query(..., description="Financial instrument (e.g. EURUSD, AUDUSD)"),
    start: Optional[datetime] = Query(
        None, description="Start datetime (ISO-8601). Defaults to 1 hour ago."
    ),
    end: Optional[datetime] = Query(
        None, description="End datetime (ISO-8601). Defaults to current time."
    ),
    mds: MarketDataService = Depends(get_market_data_service),
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
        ticks_data = mds.get_ticks(symbol, start, end)
        return ticks_data
    except ConnectionError as ce:
        raise HTTPException(status_code=503, detail=str(ce))
    except Exception as e:  # noqa: BLE001 - surfaced to client as HTTPException; logged
        logger.error(f"Error fetching ticks for {symbol}: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/api/v1/market/instruments", response_model=List[InstrumentResponse])
def get_market_instruments(
    mds: MarketDataService = Depends(get_market_data_service),
):
    """
    Retrieve tradable instruments: default B3 assets plus symbols stored locally.
    """
    return market_service.list_market_instruments(mds)


@router.get("/api/v1/market/symbols/search", response_model=List[InstrumentResponse])
def search_symbols(
    q: str = Query(..., description="Query to search stored and MT5 symbols"),
    mds: MarketDataService = Depends(get_market_data_service),
):
    """
    Search stored local market data and, when available, the MT5 terminal.
    """
    if not q.strip():
        return []

    try:
        return market_service.search_instrument_sources(mds, q)
    except HTTPException:
        raise
    except Exception as exc:
        logger.error("Error searching symbols for query '%s': %s", q, exc)
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.get("/api/v1/market/snapshot/{symbol}", response_model=MarketSnapshotResponse)
def get_market_snapshot(
    symbol: str,
    mds: MarketDataService = Depends(get_market_data_service),
):
    """
    Retrieve real-time price snapshot for a B3 asset using MT5.
    """
    symbol = symbol.upper()

    _require_mt5_live(mds)
    if not mds.mt5_available():
        raise HTTPException(
            status_code=404,
            detail=f"Live snapshot unavailable for '{symbol}' (local data provider).",
        )

    snapshot = market_service.build_market_snapshot(symbol)
    if snapshot is None:
        raise HTTPException(
            status_code=404, detail=f"Symbol '{symbol}' not found on MetaTrader 5."
        )
    return snapshot


@router.get("/api/v1/market/snapshots", response_model=MarketSnapshotsResponse)
def get_market_snapshots(
    symbols: str = Query(..., description="Comma-separated symbols (max 50)"),
    mds: MarketDataService = Depends(get_market_data_service),
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

    _require_mt5_live(mds)
    if not mds.mt5_available():
        return {"snapshots": []}

    snapshots = []
    for symbol in symbol_list:
        snapshot = market_service.build_market_snapshot(symbol)
        if snapshot is not None:
            snapshots.append(snapshot)

    return {"snapshots": snapshots}


@router.get("/api/v1/market/ticks/{symbol}", response_model=MarketTicksResponse)
def get_market_ticks(
    symbol: str,
    limit: int = Query(200, ge=1, le=1000, description="Number of recent ticks"),
    mds: MarketDataService = Depends(get_market_data_service),
):
    """
    Retrieve recent time-and-sales ticks for a symbol (newest last).
    """
    symbol = symbol.upper()

    if not mds.mt5_available():
        if get_data_source() == "mt5":
            raise HTTPException(
                status_code=503, detail="MetaTrader 5 terminal is offline."
            )
        return {"ticks": []}

    try:
        raw_ticks = mds.get_recent_ticks(symbol, limit)
    except ConnectionError as ce:
        raise HTTPException(status_code=503, detail=str(ce)) from ce

    if not raw_ticks:
        info = mds.get_symbol_info(symbol)
        if not info:
            raise HTTPException(
                status_code=404, detail=f"Symbol '{symbol}' not found on MetaTrader 5."
            )
        return {"ticks": []}

    return {"ticks": market_service.format_tape_ticks(raw_ticks[-limit:])}


@router.get(
    "/api/v1/market/instrument-info/{symbol}",
    response_model=InstrumentInfoResponse,
)
def get_market_instrument_info(
    symbol: str,
    mds: MarketDataService = Depends(get_market_data_service),
):
    """
    Retrieve curated contract specification fields for a symbol.
    """
    symbol = symbol.upper()

    if not mds.mt5_available():
        if get_data_source() == "mt5":
            raise HTTPException(
                status_code=503, detail="MetaTrader 5 terminal is offline."
            )
        raise HTTPException(
            status_code=404,
            detail=f"Symbol '{symbol}' not found (local data provider).",
        )

    try:
        info = mds.get_symbol_info(symbol)
    except ConnectionError as ce:
        raise HTTPException(status_code=503, detail=str(ce))

    if not info:
        raise HTTPException(
            status_code=404, detail=f"Symbol '{symbol}' not found on MetaTrader 5."
        )

    return market_service.symbol_info_to_instrument_response(symbol, info)


@router.get("/api/v1/market/ohlcv/{symbol}", response_model=List[OhlcvBarResponse])
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
    mds: MarketDataService = Depends(get_market_data_service),
):
    """
    Retrieve historical OHLCV data for a symbol from local storage or MT5.
    Returns the most recent `count` bars by default, or a date range when start/end are provided.
    """
    symbol = symbol.upper()

    try:
        ohlcv_data = market_service.fetch_ohlcv_rows(
            mds,
            symbol,
            timeframe,
            count=count,
            start=start,
            end=end,
        )
    except HTTPException:
        raise
    except Exception as exc:
        logger.error("Error fetching OHLCV for %s: %s", symbol, exc)
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    if not ohlcv_data:
        raise HTTPException(
            status_code=404,
            detail=f"No OHLCV data found for symbol '{symbol}'.",
        )

    return [market_service.ohlcv_to_bar_response(row) for row in ohlcv_data]


@router.get(
    "/api/v1/market/ohlcv/{symbol}/available-range",
    response_model=OhlcvAvailableRangeResponse,
)
def get_market_ohlcv_available_range(
    symbol: str,
    timeframe: str = Query(
        "D1", description="Candle timeframe (e.g. M1, M5, M15, H1, D1)"
    ),
    mds: MarketDataService = Depends(get_market_data_service),
):
    """
    Returns the earliest and latest OHLCV bar timestamps available for a symbol.
    """
    symbol = symbol.upper()

    try:
        available_range = market_service.fetch_ohlcv_available_range(
            mds, symbol, timeframe
        )
    except HTTPException:
        raise
    except Exception as exc:
        logger.error("Error probing OHLCV history for %s: %s", symbol, exc)
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    if available_range is None:
        mt5_timeframe = market_service.normalize_market_timeframe(timeframe)
        raise HTTPException(
            status_code=404,
            detail=(
                f"No OHLCV history found for symbol '{symbol}' "
                f"on timeframe '{mt5_timeframe}'."
            ),
        )

    return {
        "symbol": available_range.symbol,
        "timeframe": available_range.timeframe,
        "start": mt5_datetime_to_utc_iso(available_range.start),
        "end": mt5_datetime_to_utc_iso(available_range.end),
        "bar_count": available_range.bar_count,
    }
