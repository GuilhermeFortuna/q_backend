from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal

from q_backend.ml_filters.features import FEATURE_ALLOWLIST

Algorithm = Literal["lightgbm", "random_forest", "logistic_regression"]

DEFAULT_FEATURES = tuple(name for name in FEATURE_ALLOWLIST if name != "real_volume")
DEFAULT_SEED = 42


@dataclass(frozen=True)
class EntryFeatureConfig:
    train_end: datetime
    validation_end: datetime
    feature_names: tuple[str, ...] = DEFAULT_FEATURES
    algorithms: tuple[Algorithm, ...] = ("lightgbm", "random_forest", "logistic_regression")
    hyperparameters: dict[str, dict[str, Any]] | None = None
    seed: int = DEFAULT_SEED

    def __post_init__(self) -> None:
        if self.train_end.tzinfo is None or self.validation_end.tzinfo is None:
            raise ValueError("train_end and validation_end must be timezone-aware UTC timestamps")
        if self.train_end >= self.validation_end:
            raise ValueError("train_end must be earlier than validation_end")
        if len(self.feature_names) != len(set(self.feature_names)):
            raise ValueError("feature_names must not contain duplicates")
        if "side" not in self.feature_names or len(self.feature_names) < 2:
            raise ValueError("side and at least one other feature are required")
        unknown_features = set(self.feature_names) - set(FEATURE_ALLOWLIST)
        if unknown_features:
            raise ValueError(f"Unsupported features: {', '.join(sorted(unknown_features))}")
        if not self.algorithms or len(self.algorithms) != len(set(self.algorithms)):
            raise ValueError("algorithms must be a non-empty list without duplicates")
        if set(self.algorithms) - {"lightgbm", "random_forest", "logistic_regression"}:
            raise ValueError("Unsupported ML filter algorithm")
        if isinstance(self.seed, bool) or not 0 <= self.seed <= 0xFFFFFFFF:
            raise ValueError("seed must be a nonnegative 32-bit integer")


@dataclass(frozen=True)
class EntrySample:
    partition: Literal["train", "validation", "lockbox"]
    signal_position: int
    signal_time: datetime
    entry_time: datetime
    exit_time: datetime
    side: int
    label: int
    net_pnl: float


@dataclass(frozen=True)
class MLFilterDataset:
    dataset_id: str
    source_run_id: str
    bars: Any
    samples: tuple[EntrySample, ...]
    selected_features: tuple[str, ...]
    train_end: datetime
    validation_end: datetime
    rejections: dict[str, dict[str, int]]
    source_config: dict[str, Any]
    source_config_revision: str
    compatibility_fingerprint: str
    bars_checksum: str
    trades_checksum: str
