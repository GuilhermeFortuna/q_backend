import uuid
from datetime import datetime
from enum import Enum
from typing import Any, Optional

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, String, Text, UniqueConstraint
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
    CANCELLED = "cancelled"


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
    is_saved: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
        server_default="false",
    )

    backtest_config: Mapped["BacktestConfig"] = relationship(back_populates="runs")

    __table_args__ = (
        Index("ix_backtest_runs_status", "status"),
        Index("ix_backtest_runs_config_created", "backtest_config_id", "created_at"),
        Index("ix_backtest_runs_is_saved", "is_saved"),
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


class WalkForwardRun(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "walkforward_runs"

    name: Mapped[str] = mapped_column(String(255), nullable=False)
    status: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default=RunStatus.PENDING.value,
    )
    config: Mapped[dict[str, Any]] = mapped_column(PortableJSON, nullable=False, default=dict)
    result_summary: Mapped[Optional[dict[str, Any]]] = mapped_column(
        PortableJSON, nullable=True
    )
    lake_paths: Mapped[Optional[dict[str, Any]]] = mapped_column(PortableJSON, nullable=True)
    error_message: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    started_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)

    windows: Mapped[list["WalkForwardWindow"]] = relationship(
        back_populates="run",
        cascade="all, delete-orphan",
    )

    __table_args__ = (
        Index("ix_walkforward_runs_status", "status"),
        Index("ix_walkforward_runs_created_at", "created_at"),
    )


class WalkForwardWindow(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "walkforward_windows"

    run_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("walkforward_runs.id", ondelete="CASCADE"),
        nullable=False,
    )
    window_number: Mapped[int] = mapped_column(nullable=False)
    train_start: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    train_end: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    test_start: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    test_end: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    best_params: Mapped[dict[str, Any]] = mapped_column(PortableJSON, nullable=False, default=dict)
    is_metrics: Mapped[Optional[dict[str, Any]]] = mapped_column(PortableJSON, nullable=True)
    oos_metrics: Mapped[Optional[dict[str, Any]]] = mapped_column(PortableJSON, nullable=True)

    run: Mapped["WalkForwardRun"] = relationship(back_populates="windows")

    __table_args__ = (
        UniqueConstraint(
            "run_id",
            "window_number",
            name="uq_walkforward_windows_run_window",
        ),
    )


class StrategySearchRun(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "strategy_search_runs"

    name: Mapped[str] = mapped_column(String(255), nullable=False)
    status: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default=RunStatus.PENDING.value,
    )
    config: Mapped[dict[str, Any]] = mapped_column(PortableJSON, nullable=False, default=dict)
    result_summary: Mapped[Optional[dict[str, Any]]] = mapped_column(
        PortableJSON, nullable=True
    )
    lake_paths: Mapped[Optional[dict[str, Any]]] = mapped_column(PortableJSON, nullable=True)
    error_message: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    started_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)

    candidates: Mapped[list["StrategySearchCandidate"]] = relationship(
        back_populates="run",
        cascade="all, delete-orphan",
    )

    __table_args__ = (
        Index("ix_strategy_search_runs_status", "status"),
        Index("ix_strategy_search_runs_created_at", "created_at"),
    )


class StrategySearchCandidate(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "strategy_search_candidates"

    run_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("strategy_search_runs.id", ondelete="CASCADE"),
        nullable=False,
    )
    candidate_id: Mapped[str] = mapped_column(String(128), nullable=False)
    strategy: Mapped[str] = mapped_column(String(128), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    rank: Mapped[Optional[int]] = mapped_column(nullable=True)
    objective_value: Mapped[Optional[float]] = mapped_column(nullable=True)
    robustness_score: Mapped[Optional[float]] = mapped_column(nullable=True)
    efficiency: Mapped[Optional[float]] = mapped_column(nullable=True)
    gate_flags: Mapped[list[str]] = mapped_column(PortableJSON, nullable=False, default=list)
    passed_gates: Mapped[bool] = mapped_column(nullable=False, default=False)
    oos_metrics: Mapped[Optional[dict[str, Any]]] = mapped_column(PortableJSON, nullable=True)
    is_metrics_summary: Mapped[Optional[dict[str, Any]]] = mapped_column(
        PortableJSON, nullable=True
    )
    best_params: Mapped[Optional[dict[str, Any]]] = mapped_column(PortableJSON, nullable=True)
    window_count: Mapped[int] = mapped_column(nullable=False, default=0)
    completed_windows: Mapped[int] = mapped_column(nullable=False, default=0)

    run: Mapped["StrategySearchRun"] = relationship(back_populates="candidates")

    __table_args__ = (
        UniqueConstraint(
            "run_id",
            "candidate_id",
            name="uq_strategy_search_candidates_run_candidate",
        ),
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
