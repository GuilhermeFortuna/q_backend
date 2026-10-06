"""Research facade for loading catalog or gateway OHLCV as pandas DataFrames."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Literal

import pandas as pd

from q_backend.research.errors import NoMarketDataError
from q_backend.research.frame import (
    attach_metadata,
    bounds_to_brasilia_naive,
    drop_forming_bar,
    filter_index_range,
    inventory_rows_to_frame,
    normalize_timeframe,
    parse_bounds,
    validate_bars_frame,
)
from q_backend.research.providers import (
    ResearchResources,
    load_inventory,
    read_local_bars,
    read_remote_bars,
    resolve_database_url,
    resolve_gateway_token,
    resolve_gateway_url,
    resolve_market_data_root,
    select_auto_source,
)
from q_backend.market_data.timezone import BRASILIA_TZ

SourceLiteral = Literal["local", "remote", "auto"]


class Research:
    """Load historical OHLCV bars for research scripts without starting Q services."""

    def __init__(
        self,
        *,
        source: SourceLiteral = "local",
        database_url: str | None = None,
        market_data_root: str | Path | None = None,
        gateway_url: str | None = None,
        gateway_token: str | None = None,
        clock: Callable[[], datetime] | None = None,
        _resources: ResearchResources | None = None,
    ) -> None:
        self._source: SourceLiteral = source
        self._closed = False
        self._clock = clock or (lambda: datetime.now(BRASILIA_TZ))
        if _resources is not None:
            self._resources = _resources
            self._owns_resources = False
        else:
            self._resources = ResearchResources(
                database_url=resolve_database_url(database_url),
                market_data_root=resolve_market_data_root(market_data_root),
                gateway_url=resolve_gateway_url(gateway_url),
                gateway_token=resolve_gateway_token(gateway_token),
            )
            self._owns_resources = True

    def _ensure_open(self) -> None:
        if self._closed:
            raise RuntimeError("Research instance is closed")

    def bars(
        self,
        symbol: str,
        *,
        timeframe: str,
        start: str | datetime,
        end: str | datetime,
    ) -> pd.DataFrame:
        self._ensure_open()
        sym = symbol.strip()
        if not sym:
            raise ValueError("symbol must be non-empty")
        tf = normalize_timeframe(timeframe)
        start_ts, end_ts = parse_bounds(start, end)
        start_naive, end_naive = bounds_to_brasilia_naive(start_ts, end_ts)

        selected_source = self._source
        if selected_source == "auto":
            selected_source = select_auto_source(self._resources, sym, tf, start_naive, end_naive)

        dataset_id: str | None = None
        if selected_source == "local":
            frame, dataset_id = read_local_bars(self._resources, sym, tf, start_naive, end_naive)
        else:
            frame = read_remote_bars(self._resources, sym, tf, start_naive, end_naive)

        frame = filter_index_range(frame, start_ts, end_ts)
        frame = drop_forming_bar(frame, tf, now=self._clock())
        if not frame.empty:
            validate_bars_frame(frame)
        if frame.empty:
            raise NoMarketDataError(
                symbol=sym,
                timeframe=tf,
                source=selected_source,
                start=start_ts.isoformat(),
                end=end_ts.isoformat(),
            )
        return attach_metadata(
            frame,
            symbol=sym,
            timeframe=tf,
            source=selected_source,
            requested_start=start_ts,
            requested_end=end_ts,
            dataset_id=dataset_id,
        )

    def inventory(self) -> pd.DataFrame:
        self._ensure_open()
        rows = load_inventory(self._resources)
        normalized: list[dict[str, object]] = []
        for row in rows:
            start = row["start"]
            end = row["end"]
            if isinstance(start, datetime):
                if start.tzinfo is None:
                    start = start.replace(tzinfo=timezone.utc)
                start = start.astimezone(BRASILIA_TZ).isoformat()
            if isinstance(end, datetime):
                if end.tzinfo is None:
                    end = end.replace(tzinfo=timezone.utc)
                end = end.astimezone(BRASILIA_TZ).isoformat()
            normalized.append({**row, "start": start, "end": end})
        return inventory_rows_to_frame(normalized)

    def close(self) -> None:
        if self._closed:
            return
        if self._owns_resources:
            self._resources.close()
        self._closed = True

    def __enter__(self) -> Research:
        return self

    def __exit__(self, *args: object) -> None:
        self.close()
