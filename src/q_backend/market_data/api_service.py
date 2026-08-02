"""Market-data API service — logic lifted from the former main.py handlers."""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from fastapi import HTTPException

from q_backend.market_data import local_store
from q_backend.market_data.clients import metatrader as mt5_client_module
from q_backend.market_data.clients.metatrader import (
    TICK_FLAG_BUY,
    TICK_FLAG_SELL,
    _to_naive_local,
)
from q_backend.market_data.models import Tick
from q_backend.market_data.routing import resolve_ohlcv_source
from q_backend.market_data.service import MarketDataService
from q_backend.market_data.timezone import mt5_datetime_to_utc_iso, unix_seconds_to_utc_iso
from q_backend.storage.runtime_config import get_data_source

logger = logging.getLogger(__name__)

DEFAULT_B3_INSTRUMENTS: list[dict[str, str]] = [
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


def infer_asset_class(symbol_name: str, path: str) -> str:
    if "BMF" in path or "@" in symbol_name or "$" in symbol_name:
        return "future"
    if "FX" in path or "Forex" in path:
        return "fx"
    return "equity"


def raw_symbol_to_instrument(raw: Dict[str, Any]) -> dict:
    path = raw.get("path", "") or ""
    path_parts = path.split("\\")
    exchange = path_parts[0] if path_parts else "LOCAL"
    symbol_name = raw.get("name", "")
    return {
        "symbol": symbol_name,
        "name": raw.get("description") or symbol_name,
        "exchange": exchange,
        "assetClass": infer_asset_class(symbol_name, path),
    }


def merge_instruments_by_symbol(*groups: list[dict]) -> list[dict]:
    merged: dict[str, dict] = {}
    for group in groups:
        for item in group:
            merged.setdefault(item["symbol"], item)
    return list(merged.values())


def stored_instruments() -> list[dict]:
    return [
        raw_symbol_to_instrument(entry) for entry in local_store.stored_symbols()
    ]


def search_instrument_sources(service: MarketDataService, query: str) -> list[dict]:
    needle = query.strip()
    if not needle:
        return []

    source = get_data_source()
    mt5_up = service.mt5_available()
    remote_up = service._remote_client.is_available()

    if not mt5_up and source == "mt5":
        raise HTTPException(
            status_code=503, detail="MetaTrader 5 terminal is offline."
        )
    if not remote_up and source == "remote":
        raise HTTPException(
            status_code=503, detail="Remote MetaTrader 5 gateway is offline."
        )

    merged: dict[str, dict] = {}

    def add_hits(raw_symbols: list[dict[str, Any]]) -> None:
        for raw in raw_symbols:
            instrument = raw_symbol_to_instrument(raw)
            merged.setdefault(instrument["symbol"], instrument)

    try:
        add_hits(service._local_client.search_symbols(needle))
    except Exception as exc:  # noqa: BLE001 - best-effort market-data read; logged
        logger.error("Error searching local storage for query '%s': %s", needle, exc)

    if source != "local":
        if mt5_up:
            try:
                add_hits(service.mt5_client.search_symbols(needle))
            except Exception as exc:  # noqa: BLE001 - best-effort market-data read; logged
                logger.error("Error searching MT5 for query '%s': %s", needle, exc)
        elif remote_up:
            try:
                add_hits(service._remote_client.search_symbols(needle))
            except Exception as exc:  # noqa: BLE001 - best-effort market-data read; logged
                logger.error("Error searching remote MT5 for query '%s': %s", needle, exc)

    return list(merged.values())[:50]
def utc_iso_seconds(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    else:
        dt = dt.astimezone(timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def utc_iso_milliseconds(time_msc: int) -> str:
    seconds = time_msc // 1000
    millis = time_msc % 1000
    base = unix_seconds_to_utc_iso(seconds)
    return f"{base[:-1]}.{millis:03d}Z"


def build_market_snapshot(symbol: str) -> Optional[dict]:
    """
    Build a market snapshot dict for a resolvable symbol, or None when the symbol
    cannot be selected in MT5.
    """
    mt5 = mt5_client_module.mt5
    if mt5 is None:
        return None

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


def tick_side(flags: int) -> Optional[str]:
    buy = bool(flags & TICK_FLAG_BUY)
    sell = bool(flags & TICK_FLAG_SELL)
    if buy and not sell:
        return "buy"
    if sell and not buy:
        return "sell"
    return None


def is_trade_tick(tick: Tick) -> bool:
    flags = tick.flags or 0
    has_side = bool(flags & (TICK_FLAG_BUY | TICK_FLAG_SELL))
    return (tick.last or 0.0) > 0 or has_side


def format_tape_ticks(raw_ticks: List[Tick]) -> List[dict]:
    trade_ticks = [tick for tick in raw_ticks if is_trade_tick(tick)]
    selected = trade_ticks if trade_ticks else raw_ticks

    formatted: List[dict] = []
    for tick in selected:
        time_msc = tick.time_msc or 0
        timestamp = (
            utc_iso_milliseconds(time_msc)
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
                "side": tick_side(tick.flags or 0),
            }
        )
    return formatted


def symbol_info_to_instrument_response(symbol: str, info: Dict[str, Any]) -> dict:
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
def normalize_market_timeframe(timeframe: str) -> str:
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


def estimate_start_time(end_time: datetime, timeframe: str, count: int) -> datetime:
    """Estimate a start time going back far enough to contain at least `count` bars.

    Uses a 3x buffer to account for weekends, holidays, and low-activity periods.
    """
    seconds_map = {
        "M1": 60,
        "M2": 120,
        "M3": 180,
        "M4": 240,
        "M5": 300,
        "M6": 360,
        "M10": 600,
        "M12": 720,
        "M15": 900,
        "M20": 1200,
        "M30": 1800,
        "H1": 3600,
        "H2": 7200,
        "H3": 10800,
        "H4": 14400,
        "H6": 21600,
        "H8": 28800,
        "H12": 43200,
        "D1": 86400,
        "W1": 604800,
        "MN1": 2592000,
    }
    seconds_per_bar = seconds_map.get(timeframe.upper(), 86400)
    delta_seconds = count * seconds_per_bar * 3
    from datetime import timedelta
    return end_time - timedelta(seconds=delta_seconds)


def fetch_ohlcv_rows(
    service: MarketDataService,
    symbol: str,
    timeframe: str,
    *,
    count: int,
    start: Optional[datetime],
    end: Optional[datetime],
) -> list:
    mt5_timeframe = normalize_market_timeframe(timeframe)
    ohlcv_source = resolve_ohlcv_source(service, symbol, mt5_timeframe)

    if (start is None) ^ (end is None):
        raise HTTPException(
            status_code=400,
            detail="Both start and end must be provided for date-range queries.",
        )

    if start is not None and end is not None:
        start = _to_naive_local(start)
        end = _to_naive_local(end)
        if start >= end:
            raise HTTPException(
                status_code=400,
                detail="Start datetime must be before end datetime.",
            )
        if ohlcv_source == "local":
            client = service._local_client
        elif ohlcv_source == "remote":
            client = service._remote_client
        else:
            client = service.mt5_client
        try:
            return client.get_ohlcv(symbol, mt5_timeframe, start, end)
        except ValueError as ve:
            raise HTTPException(status_code=400, detail=str(ve)) from ve
        except ConnectionError as ce:
            raise HTTPException(status_code=503, detail=str(ce)) from ce

    if ohlcv_source == "local":
        available = local_store.available_range(symbol, mt5_timeframe)
        if available is None:
            return []
        bars = service._local_client.get_ohlcv(
            symbol, mt5_timeframe, available.start, available.end
        )
        return bars[-count:] if len(bars) > count else bars

    if ohlcv_source == "remote":
        try:
            available = service._remote_client.get_available_ohlcv_range(symbol, mt5_timeframe)
            if available is not None:
                start_est = estimate_start_time(available.end, mt5_timeframe, count)
                if start_est < available.start:
                    start_est = available.start
                try:
                    bars = service._remote_client.get_ohlcv(
                        symbol, mt5_timeframe, start_est, available.end
                    )
                    return bars[-count:] if len(bars) > count else bars
                except ValueError as ve:
                    raise HTTPException(status_code=400, detail=str(ve)) from ve
        except Exception:  # noqa: BLE001, S110 - fallback to local store if remote fails
            pass

        # Fallback to local if remote was unavailable or returned None for available range
        available_loc = local_store.available_range(symbol, mt5_timeframe)
        if available_loc is not None:
            bars = service._local_client.get_ohlcv(
                symbol, mt5_timeframe, available_loc.start, available_loc.end
            )
            return bars[-count:] if len(bars) > count else bars
        return []


    if not service.mt5_available():
        if get_data_source() == "mt5":
            raise HTTPException(
                status_code=503, detail="MetaTrader 5 terminal is offline."
            )
        raise HTTPException(
            status_code=404,
            detail=f"No OHLCV data found for symbol '{symbol}' (local data provider).",
        )

    mt5 = mt5_client_module.mt5
    if mt5 is None:
        raise HTTPException(
            status_code=503, detail="MetaTrader 5 terminal is offline."
        )

    if not mt5.symbol_select(symbol, True):
        raise HTTPException(
            status_code=404, detail=f"Symbol '{symbol}' not found on MetaTrader 5."
        )

    timeframe_map = {
        "M1": mt5_client_module._mt5_timeframe("M1"),
        "M5": mt5_client_module._mt5_timeframe("M5"),
        "M15": mt5_client_module._mt5_timeframe("M15"),
        "M30": mt5_client_module._mt5_timeframe("M30"),
        "H1": mt5_client_module._mt5_timeframe("H1"),
        "H4": mt5_client_module._mt5_timeframe("H4"),
        "D1": mt5_client_module._mt5_timeframe("D1"),
    }

    mt5_tf = timeframe_map.get(mt5_timeframe, mt5_client_module._mt5_timeframe("D1"))
    rates = mt5.copy_rates_from_pos(symbol, mt5_tf, 0, count)
    if rates is None or len(rates) == 0:
        return []
    return list(rates)


def fetch_ohlcv_available_range(service: MarketDataService, symbol: str, timeframe: str):
    mt5_timeframe = normalize_market_timeframe(timeframe)
    ohlcv_source = resolve_ohlcv_source(service, symbol, mt5_timeframe)

    if ohlcv_source == "local":
        return local_store.available_range(symbol, mt5_timeframe)

    if ohlcv_source == "remote":
        try:
            available = service._remote_client.get_available_ohlcv_range(symbol, mt5_timeframe)
            if available is not None:
                return available
        except Exception:  # noqa: BLE001, S110 - fallback to local store if remote fails
            pass
        available_loc = local_store.available_range(symbol, mt5_timeframe)
        if available_loc is not None:
            return available_loc
        return None

    if not service.is_available():
        raise HTTPException(
            status_code=503, detail="Market data provider is unavailable."
        )

    try:
        return service.mt5_client.get_available_ohlcv_range(symbol, mt5_timeframe)
    except ValueError as ve:
        raise HTTPException(status_code=400, detail=str(ve)) from ve
    except ConnectionError as ce:
        raise HTTPException(status_code=503, detail=str(ce)) from ce


def ohlcv_to_bar_response(row) -> dict:
    """Convert OHLCV model or MT5 rate row to frontend bar shape."""
    if hasattr(row, "time"):
        timestamp_dt = row.time
        if not isinstance(timestamp_dt, datetime):
            timestamp_dt = datetime.fromisoformat(
                str(timestamp_dt).replace("Z", "+00:00")
            )
        volume = (
            row.real_volume
            if row.real_volume is not None and row.real_volume > 0
            else row.tick_volume
        )
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

def list_market_instruments(service: MarketDataService) -> list[dict]:
    """Retrieve tradable instruments: default B3 assets plus symbols stored locally."""
    instruments = merge_instruments_by_symbol(
        DEFAULT_B3_INSTRUMENTS,
        stored_instruments(),
    )

    if not service.mt5_available():
        logger.warning(
            "MT5 not connected; returning default and stored instrument definitions."
        )
        return instruments

    mt5 = mt5_client_module.mt5
    if mt5 is None:
        return instruments

    for item in DEFAULT_B3_INSTRUMENTS:
        if not mt5.symbol_select(item["symbol"], True):
            logger.warning(
                "Symbol '%s' could not be selected in MT5.", item["symbol"]
            )

    return instruments

