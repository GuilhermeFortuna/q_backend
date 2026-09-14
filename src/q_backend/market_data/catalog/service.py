"""Lake catalog service coordinating publication, adoption, querying, and sweeping."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Mapping, Sequence

import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
import sqlalchemy as sa
from sqlalchemy.orm import Session

from q_backend.market_data.catalog.partitions import (
    Subject,
    WrittenFile,
    describe_existing_partition,
    write_partition_immutable,
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
from q_backend.market_data.clients.metatrader import _time_msc_to_naive_local
from q_backend.market_data.tick_cache import COLUMNAR_TICK_KEYS, _TICK_DTYPE_MAP
from q_backend.market_data.timezone import BRASILIA_TZ
from q_backend.storage.db.catalog_models import Dataset, DatasetFile
from q_backend.storage.db.engine import create_session_factory
from q_backend.storage.settings import get_settings

logger = logging.getLogger(__name__)

_OHLCV_COLUMNS = (
    "time",
    "open",
    "high",
    "low",
    "close",
    "tick_volume",
    "spread",
    "real_volume",
)


def _merge_year_frame(existing: pd.DataFrame | None, incoming: pd.DataFrame) -> pd.DataFrame:
    if existing is None or existing.empty:
        combined = incoming.copy()
    elif incoming.empty:
        combined = existing.copy()
    else:
        combined = pd.concat([existing, incoming], ignore_index=True)
    if combined.empty:
        return combined
    combined["time"] = pd.to_datetime(combined["time"])
    combined = combined.sort_values("time")
    combined = combined.drop_duplicates(subset=["time"], keep="last")
    return combined.reset_index(drop=True)


def _merge_tick_frame(existing: pd.DataFrame | None, incoming: pd.DataFrame) -> pd.DataFrame:
    if existing is None or existing.empty:
        combined = incoming.copy()
    elif incoming.empty:
        combined = existing.copy()
    else:
        combined = pd.concat([existing, incoming], ignore_index=True)
    if combined.empty:
        return combined
    combined = combined.sort_values("time_msc")
    combined = combined.drop_duplicates(subset=["time_msc"], keep="last")
    return combined.reset_index(drop=True)


def _month_key_from_msc(msc: int) -> str:
    dt = _time_msc_to_naive_local(msc)
    return f"{dt.year}-{dt.month:02d}"


def _arrays_to_dataframe(arrays: dict[str, np.ndarray]) -> pd.DataFrame:
    if len(arrays.get("time_msc", [])) == 0:
        return pd.DataFrame(columns=list(COLUMNAR_TICK_KEYS))
    return pd.DataFrame({col: arrays[col] for col in COLUMNAR_TICK_KEYS})


def _read_month_ticks(path: Path) -> pd.DataFrame:
    table = pq.read_table(path)
    df = table.to_pandas()
    for col in COLUMNAR_TICK_KEYS:
        df[col] = df[col].astype(_TICK_DTYPE_MAP[col], copy=False)
    return df


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

    def publish_bars(self, symbol: str, timeframe: str, frame: pd.DataFrame) -> Dataset:
        """Publish a new bars dataset version, reusing unchanged partitions and merging changed ones."""
        subject = Subject(kind="bars", symbol=symbol, timeframe=timeframe.upper())
        with self.session_factory() as session:
            lock_subject(session, subject)
            current = current_dataset(session, subject)

            current_partitions: dict[str, DatasetFile] = {}
            if current is not None:
                for f in current.files:
                    pkey = Path(f.path).name.split(".")[0]
                    current_partitions[pkey] = f

            if frame.empty:
                if current is not None:
                    return current
                raise ValueError(f"Cannot publish empty bars dataset for {subject}")

            df = frame.copy()
            df["time"] = pd.to_datetime(df["time"])
            if getattr(df["time"].dt, "tz", None) is not None:
                df["time"] = df["time"].dt.tz_convert(BRASILIA_TZ).dt.tz_localize(None)

            incoming_by_year: dict[str, pd.DataFrame] = {}
            for year, ydf in df.groupby(df["time"].dt.year):
                incoming_by_year[str(int(year))] = ydf

            all_keys = set(current_partitions.keys()) | set(incoming_by_year.keys())
            written_files: list[WrittenFile] = []

            for pkey in sorted(all_keys):
                if pkey in incoming_by_year:
                    if pkey in current_partitions:
                        existing_file = current_partitions[pkey]
                        existing_df = pq.read_table(self.root / existing_file.path).to_pandas()
                    else:
                        existing_df = None
                    merged = _merge_year_frame(existing_df, incoming_by_year[pkey])
                    table = pa.Table.from_pandas(merged[list(_OHLCV_COLUMNS)], preserve_index=False)
                    written = write_partition_immutable(self.root, subject, pkey, table)
                    written_files.append(written)
                else:
                    existing_file = current_partitions[pkey]
                    written = describe_existing_partition(self.root, existing_file.path, kind="bars")
                    written_files.append(written)

            written_files.sort(key=lambda f: f.partition_start)
            now = self.clock()
            if now.tzinfo is None:
                now = now.replace(tzinfo=timezone.utc)

            dataset = publish_version(session, subject, written_files, now=now, grace=self.grace)
            session.commit()
            session.refresh(dataset)
            return dataset

    def publish_ticks(self, symbol: str, arrays: Mapping[str, np.ndarray]) -> Dataset:
        """Publish a new ticks dataset version, reusing unchanged partitions and merging changed ones."""
        subject = Subject(kind="ticks", symbol=symbol, timeframe="")
        with self.session_factory() as session:
            lock_subject(session, subject)
            current = current_dataset(session, subject)

            current_partitions: dict[str, DatasetFile] = {}
            if current is not None:
                for f in current.files:
                    pkey = Path(f.path).name.split(".")[0]
                    current_partitions[pkey] = f

            if len(arrays.get("time_msc", [])) == 0:
                if current is not None:
                    return current
                raise ValueError(f"Cannot publish empty ticks dataset for {symbol}")

            df = _arrays_to_dataframe(dict(arrays))
            df["_month"] = df["time_msc"].map(lambda msc: _month_key_from_msc(int(msc)))

            incoming_by_month: dict[str, pd.DataFrame] = {}
            for month_key, mdf in df.groupby("_month", sort=True):
                incoming_by_month[str(month_key)] = mdf

            all_keys = set(current_partitions.keys()) | set(incoming_by_month.keys())
            written_files: list[WrittenFile] = []

            for pkey in sorted(all_keys):
                if pkey in incoming_by_month:
                    if pkey in current_partitions:
                        existing_file = current_partitions[pkey]
                        existing_df = _read_month_ticks(self.root / existing_file.path)
                    else:
                        existing_df = None
                    merged = _merge_tick_frame(existing_df, incoming_by_month[pkey].drop(columns=["_month"]))
                    table = pa.Table.from_pandas(merged[list(COLUMNAR_TICK_KEYS)], preserve_index=False)
                    written = write_partition_immutable(self.root, subject, pkey, table)
                    written_files.append(written)
                else:
                    existing_file = current_partitions[pkey]
                    written = describe_existing_partition(self.root, existing_file.path, kind="ticks")
                    written_files.append(written)

            written_files.sort(key=lambda f: f.partition_start)
            now = self.clock()
            if now.tzinfo is None:
                now = now.replace(tzinfo=timezone.utc)

            dataset = publish_version(session, subject, written_files, now=now, grace=self.grace)
            session.commit()
            session.refresh(dataset)
            return dataset

    def files_for_range(self, subject: Subject, start: datetime, end: datetime) -> list[Path]:
        """Return local lake file Paths matching the requested range for subject."""
        with self.session_factory() as session:
            dataset = current_dataset(session, subject)
            if dataset is None:
                return []

            start_utc = (
                start.replace(tzinfo=BRASILIA_TZ).astimezone(timezone.utc)
                if start.tzinfo is None
                else start.astimezone(timezone.utc)
            )
            end_utc = (
                end.replace(tzinfo=BRASILIA_TZ).astimezone(timezone.utc)
                if end.tzinfo is None
                else end.astimezone(timezone.utc)
            )

            matched: list[Path] = []
            for f in dataset.files:
                p_start = (
                    f.partition_start
                    if f.partition_start.tzinfo is not None
                    else f.partition_start.replace(tzinfo=timezone.utc)
                )
                p_end = (
                    f.partition_end
                    if f.partition_end.tzinfo is not None
                    else f.partition_end.replace(tzinfo=timezone.utc)
                )
                if p_start <= end_utc and p_end >= start_utc:
                    matched.append(self.root / f.path)
            return matched

    def delete_subject(self, subject: Subject) -> Dataset | None:
        """Tombstone the subject's current published dataset; removes no files."""
        with self.session_factory() as session:
            lock_subject(session, subject)
            now = self.clock()
            tombstoned = tombstone_current(session, subject, now=now, grace=self.grace)
            session.commit()
            return tombstoned

    def sweep(self, *, dry_run: bool = False) -> SweepReport:
        """Purge files of tombstoned datasets whose grace period has expired, unless shared by live datasets."""
        now = self.clock()
        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)

        with self.session_factory() as session:
            expired_datasets = sweepable(session, now=now)
            protected_paths = live_paths(session, now=now)

            files_to_delete: set[str] = set()
            bytes_freed = 0

            for dataset in expired_datasets:
                for f in dataset.files:
                    if f.path not in protected_paths:
                        files_to_delete.add(f.path)
                        bytes_freed += f.size_bytes
                if not dry_run:
                    dataset.state = "deleted"

            files_deleted_count = 0
            if not dry_run:
                for rel_path in files_to_delete:
                    full_p = self.root / rel_path
                    if full_p.is_file():
                        full_p.unlink()
                        files_deleted_count += 1
                session.commit()
            else:
                files_deleted_count = len(files_to_delete)

            datasets_deleted_count = len(expired_datasets)

            all_db_paths = set(session.scalars(sa.select(DatasetFile.path)).all())
            unreferenced_files: list[str] = []

            for p in self.root.rglob("*.parquet"):
                rel_posix = p.relative_to(self.root).as_posix()
                parts = p.name.split(".")
                if (
                    len(parts) == 3
                    and parts[-1] == "parquet"
                    and len(parts[1]) == 16
                    and all(c in "0123456789abcdefABCDEF" for c in parts[1])
                ):
                    if rel_posix not in all_db_paths:
                        unreferenced_files.append(rel_posix)

        return SweepReport(
            datasets_deleted=datasets_deleted_count,
            files_deleted=files_deleted_count,
            bytes_freed=bytes_freed,
            unreferenced_files=sorted(unreferenced_files),
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
