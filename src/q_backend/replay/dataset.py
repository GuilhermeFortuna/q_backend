"""Immutable dataset manifests for replay runs.

A manifest names every frozen input file with its SHA-256 and size, the symbols it covers and
the UTC interval it is known to cover. Verification runs before a run starts and rejects any
checksum mutation, path escape, missing symbol, or requested interval (including warmup) that
the manifest does not cover. Verification never syncs, repairs or rewrites the files.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Literal

import numpy as np
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

_CHUNK = 1 << 20


class DatasetError(ValueError):
    """Raised when a manifest or its files cannot back the requested replay run."""


class DatasetFile(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    path: str = Field(min_length=1)
    symbol: str = Field(min_length=1)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    size_bytes: int = Field(ge=0)


class SymbolSpec(BaseModel):
    """Instrument parameters the simulated broker and gateway metadata are built from."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    description: str = ""
    point: float = Field(gt=0, allow_inf_nan=False)
    digits: int = Field(ge=0, le=12)
    volume_min: float = Field(gt=0, allow_inf_nan=False)
    volume_step: float = Field(gt=0, allow_inf_nan=False)
    volume_max: float = Field(gt=0, allow_inf_nan=False)
    trade_contract_size: float = Field(gt=0, allow_inf_nan=False)
    margin_per_contract: str = Field(pattern=r"^[0-9]+(\.[0-9]+)?$")


class DatasetManifest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1]
    time_unit: Literal["msc"]
    exchange_timezone: str = Field(min_length=1)
    coverage_start_utc: AwareDatetime
    coverage_end_utc: AwareDatetime
    files: list[DatasetFile] = Field(min_length=1)
    symbol_specs: dict[str, SymbolSpec] = Field(min_length=1)

    @model_validator(mode="after")
    def _consistent(self) -> DatasetManifest:
        if self.coverage_end_utc <= self.coverage_start_utc:
            raise ValueError("coverage_end_utc must be after coverage_start_utc")
        paths = [item.path for item in self.files]
        if len(set(paths)) != len(paths):
            raise ValueError("manifest lists the same file more than once")
        unspecified = self.symbols - set(self.symbol_specs)
        if unspecified:
            raise ValueError(f"symbols without a spec: {', '.join(sorted(unspecified))}")
        return self

    @property
    def symbols(self) -> frozenset[str]:
        return frozenset(item.symbol for item in self.files)

    def file_for(self, symbol: str) -> DatasetFile:
        matches = [item for item in self.files if item.symbol == symbol]
        if len(matches) != 1:
            raise DatasetError(f"symbol {symbol} must map to exactly one tick file")
        return matches[0]


def load_manifest(path: Path) -> DatasetManifest:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise DatasetError(f"cannot read dataset manifest {path}: {exc}") from exc
    try:
        return DatasetManifest.model_validate(raw)
    except ValueError as exc:
        raise DatasetError(f"invalid dataset manifest {path}: {exc}") from exc


def _sha256_file(path: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as handle:
        while chunk := handle.read(_CHUNK):
            digest.update(chunk)
            size += len(chunk)
    return digest.hexdigest(), size


def verify_manifest(
    manifest: DatasetManifest,
    root: Path,
    *,
    symbols: list[str],
    start_utc: AwareDatetime,
    end_utc: AwareDatetime,
    warmup_start_utc: AwareDatetime | None = None,
) -> list[str]:
    """Check files, symbols and coverage; return ``sha256:`` checksums of every file.

    Raises :class:`DatasetError` on the first problem. ``root`` is the directory the manifest
    paths are relative to; a path that resolves outside it is refused.
    """
    base = root.resolve()
    checksums: list[str] = []
    for item in manifest.files:
        candidate = (base / item.path).resolve()
        if base not in candidate.parents:
            raise DatasetError(f"dataset file escapes its root: {item.path}")
        if not candidate.is_file():
            raise DatasetError(f"dataset file is missing: {item.path}")
        digest, size = _sha256_file(candidate)
        if digest != item.sha256 or size != item.size_bytes:
            raise DatasetError(f"dataset file changed since the manifest was prepared: {item.path}")
        checksums.append(f"sha256:{digest}")

    missing = sorted(set(symbols) - manifest.symbols)
    if missing:
        raise DatasetError(f"requested symbols are not in the dataset: {', '.join(missing)}")

    lower = warmup_start_utc if warmup_start_utc is not None else start_utc
    if lower < manifest.coverage_start_utc or end_utc > manifest.coverage_end_utc:
        what = "warmup and interval" if warmup_start_utc is not None else "interval"
        raise DatasetError(
            f"requested {what} is outside the dataset coverage "
            f"[{manifest.coverage_start_utc.isoformat()}, {manifest.coverage_end_utc.isoformat()})"
        )
    return checksums


TICK_KEYS = ("time_msc", "bid", "ask", "last", "volume", "flags")


def load_tick_file(path: Path, symbol: str) -> dict[str, np.ndarray]:
    """Read one frozen tick NPZ (UTC milliseconds) and check its ordering and shape.

    Records are kept in source order; equal timestamps are never merged or dropped. A file
    whose timestamps decrease is refused: preparation must sort it explicitly and record the
    new checksum.
    """
    try:
        with np.load(path, allow_pickle=False) as npz:
            columns = {name: np.asarray(npz[name]) for name in TICK_KEYS}
    except (OSError, KeyError, ValueError) as exc:
        raise DatasetError(f"cannot read tick file for {symbol}: {exc}") from exc
    times = columns["time_msc"]
    count = len(times)
    if times.ndim != 1 or any(array.ndim != 1 or len(array) != count for array in columns.values()):
        raise DatasetError(f"tick file for {symbol} has ragged columns")
    if count and np.any(times[1:] < times[:-1]):
        raise DatasetError(f"tick file for {symbol} is out of order; prepare a sorted copy")
    return {
        "time_msc": times.astype(np.int64, copy=False),
        "bid": columns["bid"].astype(np.float64, copy=False),
        "ask": columns["ask"].astype(np.float64, copy=False),
        "last": columns["last"].astype(np.float64, copy=False),
        "volume": columns["volume"].astype(np.float64, copy=False),
        "flags": columns["flags"].astype(np.int32, copy=False),
    }
