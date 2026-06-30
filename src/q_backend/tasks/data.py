"""Worker-side market data loading with a parquet cache.

Leaf actors run in many worker processes that each hold their own MT5 connection.
Several leaves of the same job (windows, candidates, trial batches) need the same
OHLCV range, so we cache the fetched frame as parquet in the data lake keyed by
(symbol, timeframe, start, end). The first leaf to need a range fetches it from MT5
and writes the cache; the rest read parquet, keeping terminal load low.
"""

import hashlib
import logging
from datetime import datetime
from pathlib import Path

import pandas as pd

from q_backend.market_data.exogenous_context import prepare_evaluation_frame
from q_backend.optimization.backtest_runner import DefaultBacktestRunner
from q_backend.optimization.strategy_search import StrategySearchConfig
from q_backend.storage.settings import get_settings
from q_backend.tasks.worker_context import get_worker_market_data_service

logger = logging.getLogger(__name__)


def _cache_dir() -> Path:
    return Path(get_settings().data_lake_root) / "_cache" / "ohlcv"


def _cache_path(symbol: str, timeframe: str, start: datetime, end: datetime) -> Path:
    key = f"{symbol}|{timeframe}|{start.isoformat()}|{end.isoformat()}"
    digest = hashlib.sha1(key.encode("utf-8")).hexdigest()[:16]
    safe_symbol = symbol.replace("/", "_").replace("\\", "_")
    return _cache_dir() / f"{safe_symbol}_{timeframe}_{digest}.parquet"


def load_ohlcv_frame(
    symbol: str, timeframe: str, start: datetime, end: datetime
) -> pd.DataFrame:
    """Return the naive-local OHLCV frame for a range, using the parquet cache."""
    path = _cache_path(symbol, timeframe, start, end)
    if path.is_file():
        try:
            cached = pd.read_parquet(path)
            cached = cached.set_index("time")
            cached.index = pd.to_datetime(cached.index, format="ISO8601")
            return cached
        except Exception:
            logger.warning("Corrupt OHLCV cache at %s; refetching", path, exc_info=True)

    service = get_worker_market_data_service()
    df = DefaultBacktestRunner.load_sliced_frame(
        service, symbol=symbol, timeframe=timeframe, start=start, end=end
    )
    _write_ohlcv_cache(df, path)
    return df


def prime_ohlcv_cache(
    market_data_service,
    *,
    symbol: str,
    timeframe: str,
    start: datetime,
    end: datetime,
) -> pd.DataFrame:
    """Fetch OHLCV on the caller thread and write the worker parquet cache.

    Used by the API ``start_job`` path so MetaTrader5 is touched from the request
    thread and leaf workers read parquet instead of calling ``get_ohlcv`` again.
    """
    path = _cache_path(symbol, timeframe, start, end)
    if path.is_file():
        try:
            cached = pd.read_parquet(path)
            cached = cached.set_index("time")
            cached.index = pd.to_datetime(cached.index, format="ISO8601")
            return cached
        except Exception:
            logger.warning("Corrupt OHLCV cache at %s; refetching", path, exc_info=True)

    df = DefaultBacktestRunner.load_sliced_frame(
        market_data_service,
        symbol=symbol,
        timeframe=timeframe,
        start=start,
        end=end,
    )
    _write_ohlcv_cache(df, path)
    return df


def _write_ohlcv_cache(df: pd.DataFrame, path: Path) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        df.rename_axis("time").reset_index().to_parquet(path, index=False)
    except Exception:
        logger.warning("Failed to write OHLCV cache at %s", path, exc_info=True)


def load_evaluation_frame(request: StrategySearchConfig) -> pd.DataFrame:
    """Load primary OHLCV once and attach configured exogenous context columns."""
    backtest = request.backtest
    frame, _ = prepare_evaluation_frame(
        primary_symbol=backtest.symbol,
        primary_timeframe=backtest.timeframe,
        start=backtest.start,
        end=backtest.end,
        exogenous_series=request.exogenous_series,
        loader=load_ohlcv_frame,
    )
    return frame


def sliced_runner(
    symbol: str, timeframe: str, start: datetime, end: datetime
) -> DefaultBacktestRunner:
    """Build a backtest runner that slices a cached OHLCV frame per run."""
    return DefaultBacktestRunner.from_frame_sliced(
        load_ohlcv_frame(symbol, timeframe, start, end)
    )
