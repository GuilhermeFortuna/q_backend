"""Lake catalog service coordinating publication, adoption, querying, and sweeping."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Mapping, Sequence

import numpy as np
import pandas as pd
from sqlalchemy.orm import Session

from q_backend.market_data.catalog.partitions import (
    Subject,
    WrittenFile,
    describe_existing_partition,
)
from q_backend.market_data.catalog.repository import (
    current_dataset,
    get_dataset,
    list_current_datasets,
    live_paths,
    lock_subject,
    publish_version,
    sweepable,
    to_manifest,
    tombstone_current,
)
from q_backend.market_data.timezone import BRASILIA_TZ
from q_backend.storage.db.catalog_models import Dataset
from q_backend.storage.db.engine import create_session_factory
from q_backend.storage.settings import get_settings

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class AdoptionReport:
    adopted: list[Subject]
    skipped_existing: list[Subject]
    files: int
    bytes: int


@dataclass(frozen=True)
class SweepReport:
    datasets_deleted: int
    files_deleted: int
    bytes_freed: int
    unreferenced_files: list[str]


class LakeCatalog:
    def __init__(
        self,
        session_factory: Callable[[], Session],
        root: Path,
        grace: timedelta = timedelta(days=7),
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ) -> None:
        self.session_factory = session_factory
        self.root = root
        self.grace = grace
        self.clock = clock

    def adopt(self) -> AdoptionReport:
        """Catalog existing bars and ticks parquet partitions without modifying files."""
        candidates: list[tuple[Subject, list[Path]]] = []

        ohlcv_dir = self.root / "ohlcv"
        if ohlcv_dir.is_dir():
            for symbol_dir in sorted(ohlcv_dir.iterdir()):
                if not symbol_dir.is_dir():
                    continue
                for tf_dir in sorted(symbol_dir.iterdir()):
                    if not tf_dir.is_dir():
                        continue
                    files = sorted(tf_dir.glob("*.parquet"))
                    if files:
                        subject = Subject(kind="bars", symbol=symbol_dir.name, timeframe=tf_dir.name)
                        candidates.append((subject, files))

        ticks_dir = self.root / "ticks"
        if ticks_dir.is_dir():
            for symbol_dir in sorted(ticks_dir.iterdir()):
                if not symbol_dir.is_dir():
                    continue
                files = sorted(symbol_dir.glob("*.parquet"))
                if files:
                    subject = Subject(kind="ticks", symbol=symbol_dir.name, timeframe="")
                    candidates.append((subject, files))

        adopted: list[Subject] = []
        skipped_existing: list[Subject] = []
        total_files = 0
        total_bytes = 0

        for subject, files in candidates:
            with self.session_factory() as session:
                lock_subject(session, subject)
                current = current_dataset(session, subject)
                if current is not None:
                    skipped_existing.append(subject)
                    continue

                written_files = [
                    describe_existing_partition(
                        self.root,
                        p.relative_to(self.root).as_posix(),
                        kind=subject.kind,
                    )
                    for p in files
                ]
                written_files.sort(key=lambda f: f.partition_start)

                now = self.clock()
                if now.tzinfo is None:
                    now = now.replace(tzinfo=timezone.utc)

                publish_version(session, subject, written_files, now=now, grace=self.grace)
                session.commit()

                adopted.append(subject)
                total_files += len(written_files)
                total_bytes += sum(f.size_bytes for f in written_files)

        return AdoptionReport(
            adopted=adopted,
            skipped_existing=skipped_existing,
            files=total_files,
            bytes=total_bytes,
        )


_catalog_instance: LakeCatalog | None = None


def get_lake_catalog() -> LakeCatalog:
    global _catalog_instance
    if _catalog_instance is None:
        settings = get_settings()
        from q_backend.market_data.local_store import market_data_root

        root = market_data_root()
        grace = timedelta(seconds=getattr(settings, "catalog_tombstone_grace_s", 604_800))
        session_factory = create_session_factory()
        _catalog_instance = LakeCatalog(
            session_factory=session_factory,
            root=root,
            grace=grace,
        )
    return _catalog_instance
