from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class EntryInstance(BaseModel):
    strategy: str
    params: dict[str, Any] = {}


class EntryManagerConfig(BaseModel):
    kind: str = "or"
    params: dict[str, Any] = Field(default_factory=dict)
