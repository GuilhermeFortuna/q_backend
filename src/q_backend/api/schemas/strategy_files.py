"""Response models for strategy files and frozen strategy versions."""

from datetime import datetime
from typing import List, Optional

from pydantic import BaseModel

from q_backend.backtesting.strategy_registry import StrategyParamSpec


class StrategyFile(BaseModel):
    id: str
    label: str
    description: str
    class_name: Optional[str] = None
    bundle_hash: str
    valid: bool
    error: Optional[str] = None
    parameters: List[StrategyParamSpec]
    uses_tick_hooks: bool = False
    requires_tick_data: bool = False


class StrategyFileListResponse(BaseModel):
    strategy_files: List[StrategyFile]


class StrategySourceFile(BaseModel):
    path: str
    content: str


class StrategyVersionResponse(BaseModel):
    version_id: str
    strategy_id: str
    class_name: str
    bundle_hash: str
    created_at: datetime
    parameters: List[StrategyParamSpec]
    source_files: List[StrategySourceFile]


__all__ = [
    "StrategyFileListResponse",
    "StrategyFile",
    "StrategySourceFile",
    "StrategyVersionResponse",
]
