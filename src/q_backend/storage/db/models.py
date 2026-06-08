import uuid
from datetime import datetime
from enum import Enum
from typing import Any, Optional

from sqlalchemy import DateTime, ForeignKey, Index, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from q_backend.storage.db.base import (
    Base,
    PortableJSON,
    TimestampMixin,
    UUIDPrimaryKeyMixin,
)


class RunStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"


class TrialStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    PRUNED = "pruned"


class DatasetType(str, Enum):
    OHLCV = "ohlcv"
    TICKS = "ticks"


class Strategy(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "strategies"

    name: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    versions: Mapped[list["StrategyVersion"]] = relationship(
        back_populates="strategy",
        cascade="all, delete-orphan",
    )

    __table_args__ = (Index("ix_strategies_name", "name"),)


class StrategyVersion(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "strategy_versions"

    strategy_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("strategies.id", ondelete="CASCADE"),
        nullable=False,
    )
    version: Mapped[int] = mapped_column(nullable=False)
    config: Mapped[dict[str, Any]] = mapped_column(PortableJSON, nullable=False, default=dict)
    status: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default=RunStatus.PENDING.value,
    )

    strategy: Mapped["Strategy"] = relationship(back_populates="versions")
    backtest_configs: Mapped[list["BacktestConfig"]] = relationship(
        back_populates="strategy_version",
    )

    __table_args__ = (
        UniqueConstraint("strategy_id", "version", name="uq_strategy_versions_strategy_version"),
    )


class BacktestConfig(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "backtest_configs"

    strategy_version_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        ForeignKey("strategy_versions.id", ondelete="SET NULL"),
        nullable=True,
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    config: Mapped[dict[str, Any]] = mapped_column(PortableJSON, nullable=False, default=dict)

    strategy_version: Mapped[Optional["StrategyVersion"]] = relationship(
        back_populates="backtest_configs",
    )
    runs: Mapped[list["BacktestRun"]] = relationship(
        back_populates="backtest_config",
        cascade="all, delete-orphan",
    )

    __table_args__ = (
        Index("ix_backtest_configs_strategy_version_id", "strategy_version_id"),
    )


class BacktestRun(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "backtest_runs"

    backtest_config_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("backtest_configs.id", ondelete="CASCADE"),
        nullable=False,
    )
    status: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default=RunStatus.PENDING.value,
    )
    config: Mapped[dict[str, Any]] = mapped_column(PortableJSON, nullable=False, default=dict)
    result_summary: Mapped[Optional[dict[str, Any]]] = mapped_column(PortableJSON, nullable=True)
    lake_paths: Mapped[Optional[dict[str, Any]]] = mapped_column(PortableJSON, nullable=True)
    error_message: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    started_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)

    backtest_config: Mapped["BacktestConfig"] = relationship(back_populates="runs")

    __table_args__ = (
        Index("ix_backtest_runs_status", "status"),
        Index("ix_backtest_runs_config_created", "backtest_config_id", "created_at"),
    )


class OptimizationStudy(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "optimization_studies"

    name: Mapped[str] = mapped_column(String(255), nullable=False)
    status: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default=RunStatus.PENDING.value,
    )
    config: Mapped[dict[str, Any]] = mapped_column(PortableJSON, nullable=False, default=dict)

    trials: Mapped[list["OptimizationTrial"]] = relationship(
        back_populates="study",
        cascade="all, delete-orphan",
    )

    __table_args__ = (
        Index("ix_optimization_studies_name", "name"),
        Index("ix_optimization_studies_status", "status"),
    )


class OptimizationTrial(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "optimization_trials"

    study_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("optimization_studies.id", ondelete="CASCADE"),
        nullable=False,
    )
    trial_number: Mapped[int] = mapped_column(nullable=False)
    status: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default=TrialStatus.PENDING.value,
    )
    params: Mapped[dict[str, Any]] = mapped_column(PortableJSON, nullable=False, default=dict)
    metrics: Mapped[Optional[dict[str, Any]]] = mapped_column(PortableJSON, nullable=True)

    study: Mapped["OptimizationStudy"] = relationship(back_populates="trials")

    __table_args__ = (
        UniqueConstraint("study_id", "trial_number", name="uq_optimization_trials_study_trial"),
    )


class DataIngestionRun(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "data_ingestion_runs"

    source: Mapped[str] = mapped_column(String(64), nullable=False)
    symbol: Mapped[str] = mapped_column(String(64), nullable=False)
    dataset_type: Mapped[str] = mapped_column(String(32), nullable=False)
    timeframe: Mapped[Optional[str]] = mapped_column(String(16), nullable=True)
    status: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default=RunStatus.PENDING.value,
    )
    lake_path: Mapped[Optional[str]] = mapped_column(String(1024), nullable=True)
    stats: Mapped[Optional[dict[str, Any]]] = mapped_column(PortableJSON, nullable=True)
    started_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        Index("ix_data_ingestion_runs_symbol_dataset", "symbol", "dataset_type"),
        Index("ix_data_ingestion_runs_status", "status"),
    )
