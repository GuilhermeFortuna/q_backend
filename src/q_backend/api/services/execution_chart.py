"""Read-only deployment chart service (bars + strategy indicator series).

Serves ``GET /api/v1/execution/deployments/{id}/chart``. The payload is computed
through the *same* indicator path the forward worker evaluates
(:func:`q_backend.execution.indicator_frame.augment_indicator_frame`), so the
chart is a faithful mirror of what the strategy sees on each bar close — never a
frontend re-implementation.

Guardrails honoured here:
- read-only (no worker state, no evaluator instance, no lifecycle mutation);
- indicator math goes exclusively through the shared helper;
- nothing is persisted (compute-and-return only);
- market reads funnel through ``MarketDataService`` (auto/mt5/local);
- bounded work: ``bars`` is capped by the router and the window bound is reused.
"""

from __future__ import annotations

import uuid
from collections import OrderedDict
from datetime import datetime, timezone
from typing import Any, Optional

import pandas as pd
from fastapi import HTTPException

from q_backend.api.schemas.execution import (
    DeploymentChartBar,
    DeploymentChartIndicator,
    DeploymentChartResponse,
)
from q_backend.backtesting.chart_data import serialize_chart_data
from q_backend.execution.bars import bar_close_time, drop_forming_bar
from q_backend.execution.indicator_frame import augment_indicator_frame
from q_backend.execution.strategy_build import (
    UnsupportedForwardStrategyError,
    build_strategy_from_compiled,
)
from q_backend.execution.warmup import (
    WindowBoundUndeterminedError,
    compute_window_bound_bars,
)
from q_backend.market_data.api_service import fetch_ohlcv_rows, ohlcv_to_bar_response
from q_backend.market_data.exogenous_context import bar_duration
from q_backend.market_data.service import MarketDataService
from q_backend.storage.db.execution_repositories import get_execution_deployment

# Extra bars fetched beyond (display + window bound) so the forming bar can be
# dropped without shrinking the warm-up window.
_FETCH_CUSHION_BARS = 5

# In-process payload cache keyed by (deployment_id, bars). Each entry stores the
# last completed bar's open time so a repeated poll with an unchanged last bar is
# served without recomputing indicators; a new bar invalidates it.
_CHART_CACHE: "OrderedDict[tuple[str, int], tuple[pd.Timestamp, DeploymentChartResponse]]" = (
    OrderedDict()
)
_CHART_CACHE_MAX = 64


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _rows_to_frame(rows: list[Any]) -> pd.DataFrame:
    """Normalise OHLCV models / MT5 rate rows into a time-indexed OHLCV frame."""
    columns = ["open", "high", "low", "close", "volume", "tick_volume"]
    if not rows:
        return pd.DataFrame(columns=columns)

    records: list[dict[str, Any]] = []
    index: list[pd.Timestamp] = []
    for row in rows:
        bar = ohlcv_to_bar_response(row)
        index.append(pd.Timestamp(bar["timestamp"]))
        records.append(
            {
                "open": bar["open"],
                "high": bar["high"],
                "low": bar["low"],
                "close": bar["close"],
                "volume": bar["volume"],
                "tick_volume": bar["volume"],
            }
        )
    frame = pd.DataFrame(records, index=pd.DatetimeIndex(index)).sort_index()
    return frame[~frame.index.duplicated(keep="last")]


def _load_completed_frame(
    mds: MarketDataService,
    symbol: str,
    timeframe: str,
    total: int,
    *,
    now: datetime,
) -> pd.DataFrame:
    """Fetch the most recent ``total`` completed bars (forming bar excluded)."""
    try:
        rows = fetch_ohlcv_rows(
            mds,
            symbol,
            timeframe,
            count=total + _FETCH_CUSHION_BARS,
            start=None,
            end=None,
        )
    except HTTPException:
        raise
    except (ConnectionError, ValueError) as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc

    frame = _rows_to_frame(rows)
    if frame.empty:
        return frame

    frame = drop_forming_bar(frame, timeframe, now=now)
    if len(frame) > total:
        frame = frame.iloc[-total:]
    return frame


def _cache_get(
    key: tuple[str, int], last_open: pd.Timestamp
) -> Optional[DeploymentChartResponse]:
    entry = _CHART_CACHE.get(key)
    if entry is None:
        return None
    cached_open, payload = entry
    if cached_open != last_open:
        return None
    _CHART_CACHE.move_to_end(key)
    return payload


def _cache_put(
    key: tuple[str, int], last_open: pd.Timestamp, payload: DeploymentChartResponse
) -> None:
    _CHART_CACHE[key] = (last_open, payload)
    _CHART_CACHE.move_to_end(key)
    while len(_CHART_CACHE) > _CHART_CACHE_MAX:
        _CHART_CACHE.popitem(last=False)


def get_deployment_chart(
    session,
    mds: MarketDataService,
    deployment_id: uuid.UUID,
    *,
    bars: int,
    now: Optional[datetime] = None,
) -> DeploymentChartResponse:
    """Compute (or serve cached) chart payload for a deployment.

    Raises ``HTTPException`` 404 when the deployment is unknown, 422 when the
    strategy's indicator window cannot be bounded, and 503 when market data is
    unavailable — never a 500 or an empty 200.
    """
    deployment = get_execution_deployment(session, deployment_id)
    if deployment is None:
        raise HTTPException(status_code=404, detail="deployment not found")

    now = now or _utcnow()
    symbol = deployment.symbol
    timeframe = deployment.timeframe.upper()
    compiled = deployment.compiled_config

    try:
        window_bound = compute_window_bound_bars(compiled)
    except WindowBoundUndeterminedError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    total = bars + window_bound
    frame = _load_completed_frame(mds, symbol, timeframe, total, now=now)
    if frame.empty:
        raise HTTPException(
            status_code=503,
            detail=(
                f"market data unavailable for '{symbol}' {timeframe}; "
                "no completed bars could be loaded"
            ),
        )

    last_open = frame.index[-1]
    cache_key = (str(deployment_id), bars)
    cached = _cache_get(cache_key, last_open)
    if cached is not None:
        return cached

    try:
        strategy = build_strategy_from_compiled(compiled, symbol=symbol)
    except UnsupportedForwardStrategyError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    augmented = augment_indicator_frame(strategy, frame)
    display = augmented.iloc[-bars:] if len(augmented) > bars else augmented
    serialized = serialize_chart_data(display, strategy)

    last_bar_open = display.index[-1].to_pydatetime()
    last_bar_close = bar_close_time(last_bar_open, timeframe)
    next_bar_close = (pd.Timestamp(last_bar_close) + bar_duration(timeframe)).to_pydatetime()

    payload = DeploymentChartResponse(
        symbol=symbol,
        timeframe=timeframe,
        window_bound_bars=window_bound,
        last_bar_close_time=last_bar_close,
        next_bar_close_time=next_bar_close,
        bars=[DeploymentChartBar(**bar) for bar in serialized["bars"]],
        indicators=[
            DeploymentChartIndicator(**indicator)
            for indicator in serialized["indicators"]
        ],
    )
    _cache_put(cache_key, last_open, payload)
    return payload
