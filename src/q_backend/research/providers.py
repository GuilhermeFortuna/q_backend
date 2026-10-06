"""Scoped market-data providers for the research library."""

from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Literal

import pandas as pd
from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from q_backend.market_data.catalog.partitions import Subject
from q_backend.market_data.catalog.repository import current_dataset, list_current_datasets
from q_backend.market_data.catalog.service import LakeCatalog
from q_backend.market_data.clients.remote import RemoteMt5Client
from q_backend.market_data.lake_query import LakePathError, read_bars_table
from q_backend.market_data.timezone import BRASILIA_TZ
from q_backend.research.frame import (
    bars_table_to_frame,
    bounds_to_brasilia_naive,
    ohlcv_models_to_frame,
)
from q_backend.storage.settings import get_settings


def resolve_market_data_root(explicit: str | Path | None) -> Path:
    if explicit is not None:
        root = Path(explicit)
        if not root.is_absolute():
            root = root.resolve()
        return root
    env_root = os.getenv("Q_MARKET_DATA_ROOT")
    if env_root:
        root = Path(env_root)
        if not root.is_absolute():
            root = Path.cwd() / root
        return root.resolve()
    settings = get_settings()
    root = Path(settings.market_data_root)
    if not root.is_absolute():
        root = Path.cwd() / root
    return root.resolve()


def resolve_database_url(explicit: str | None) -> str:
    if explicit is not None:
        return explicit
    return get_settings().database_url


def resolve_gateway_url(explicit: str | None) -> str | None:
    if explicit is not None:
        return explicit or None
    from q_backend.storage.runtime_config import get_remote_gateway_url

    return get_remote_gateway_url()


def resolve_gateway_token(explicit: str | None) -> str | None:
    if explicit is not None:
        return explicit or None
    from q_backend.storage.runtime_config import get_remote_gateway_token

    return get_remote_gateway_token()


class ResearchResources:
    """Owns optional SQLAlchemy engine and catalog instances for one Research object."""

    def __init__(
        self,
        *,
        database_url: str,
        market_data_root: Path,
        gateway_url: str | None,
        gateway_token: str | None,
        session_factory: sessionmaker[Session] | None = None,
        catalog: LakeCatalog | None = None,
        remote_client: RemoteMt5Client | None = None,
    ) -> None:
        self.market_data_root = market_data_root
        self.gateway_url = gateway_url
        self.gateway_token = gateway_token
        self._owns_engine = session_factory is None and catalog is None
        self._engine: Engine | None = None
        if session_factory is not None:
            self._session_factory = session_factory
        else:
            self._engine = create_engine(database_url, pool_pre_ping=True)
            self._session_factory = sessionmaker(
                bind=self._engine,
                autoflush=False,
                autocommit=False,
                expire_on_commit=False,
            )
        self._catalog = catalog
        self._remote = remote_client

    def catalog(self) -> LakeCatalog:
        if self._catalog is None:
            self._catalog = LakeCatalog(session_factory=self._session_factory, root=self.market_data_root)
        return self._catalog

    def remote_client(self) -> RemoteMt5Client:
        if self._remote is None:
            self._remote = RemoteMt5Client(base_url=self.gateway_url, token=self.gateway_token)
        return self._remote

    def close(self) -> None:
        if self._engine is not None and self._owns_engine:
            self._engine.dispose()
            self._engine = None


def _to_brasilia_naive(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(BRASILIA_TZ).replace(tzinfo=None)


def _dataset_envelope_covers(
    dataset,
    start_naive: datetime,
    end_naive: datetime,
) -> bool:
    ds_start = _to_brasilia_naive(dataset.time_start)
    ds_end = _to_brasilia_naive(dataset.time_end)
    return ds_start <= start_naive and ds_end >= end_naive


def select_auto_source(
    resources: ResearchResources,
    symbol: str,
    timeframe: str,
    start_naive: datetime,
    end_naive: datetime,
) -> Literal["local", "remote"]:
    catalog = resources.catalog()
    subject = Subject(kind="bars", symbol=symbol, timeframe=timeframe)
    with catalog.session_factory() as session:
        dataset = current_dataset(session, subject)
        if dataset is not None and _dataset_envelope_covers(dataset, start_naive, end_naive):
            return "local"
    if resources.gateway_url is None:
        raise ValueError(
            f"Local catalog does not cover {symbol} {timeframe} for the requested range "
            "and no remote gateway is configured (gateway_url / Q_MT5_GATEWAY_URL)."
        )
    return "remote"


def read_local_bars(
    resources: ResearchResources,
    symbol: str,
    timeframe: str,
    start_naive: datetime,
    end_naive: datetime,
) -> tuple[pd.DataFrame, str | None]:
    catalog = resources.catalog()
    subject = Subject(kind="bars", symbol=symbol, timeframe=timeframe)
    with catalog.session_factory() as session:
        dataset = current_dataset(session, subject)
        if dataset is None:
            return bars_table_to_frame(read_bars_table([], start_naive, end_naive)), None
        dataset_id = str(dataset.dataset_id)

    paths = catalog.files_for_range(subject, start_naive, end_naive)
    if not paths:
        raise ValueError(
            f"Catalog lists {symbol} {timeframe} but no parquet files overlap "
            f"{start_naive.isoformat()}–{end_naive.isoformat()}."
        )
    missing = [path for path in paths if not path.is_file()]
    if missing:
        raise ValueError(f"Catalog references missing parquet file(s): {missing[0]}")
    try:
        table = read_bars_table(paths, start_naive, end_naive)
    except LakePathError as exc:
        raise ValueError(str(exc)) from exc
    return bars_table_to_frame(table), dataset_id


def read_remote_bars(
    resources: ResearchResources,
    symbol: str,
    timeframe: str,
    start_naive: datetime,
    end_naive: datetime,
) -> pd.DataFrame:
    client = resources.remote_client()
    if not client.is_supported():
        raise ValueError("Remote gateway URL is not configured.")
    bars = client.get_ohlcv(symbol, timeframe, start_naive, end_naive)
    return ohlcv_models_to_frame(bars)


def load_inventory(resources: ResearchResources) -> list[dict[str, object]]:
    catalog = resources.catalog()
    with catalog.session_factory() as session:
        datasets = list_current_datasets(session, kind="bars")
        rows: list[dict[str, object]] = []
        for dataset in datasets:
            total_bytes = sum(f.size_bytes for f in dataset.files)
            rows.append(
                {
                    "symbol": dataset.symbol,
                    "timeframe": dataset.timeframe.upper(),
                    "start": dataset.time_start,
                    "end": dataset.time_end,
                    "rows": int(dataset.row_count),
                    "bytes": int(total_bytes),
                    "dataset_id": str(dataset.dataset_id),
                }
            )
        return rows
