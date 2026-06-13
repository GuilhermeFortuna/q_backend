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

from q_backend.optimization.backtest_runner import DefaultBacktestRunner
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
            cached.index = pd.to_datetime(cached.index)
            return cached
        except Exception:
            logger.warning("Corrupt OHLCV cache at %s; refetching", path, exc_info=True)

    service = get_worker_market_data_service()
    df = DefaultBacktestRunner.load_sliced_frame(
        service, symbol=symbol, timeframe=timeframe, start=start, end=end
    )
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        df.rename_axis("time").reset_index().to_parquet(path, index=False)
    except Exception:
        logger.warning("Failed to write OHLCV cache at %s", path, exc_info=True)
    return df


def sliced_runner(
    symbol: str, timeframe: str, start: datetime, end: datetime
) -> DefaultBacktestRunner:
    """Build a backtest runner that slices a cached OHLCV frame per run."""
    return DefaultBacktestRunner.from_frame_sliced(
        load_ohlcv_frame(symbol, timeframe, start, end)
    )
