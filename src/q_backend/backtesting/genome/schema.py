"""Pydantic models for the genome DSL document (design §2.2)."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


class NodeRef(BaseModel):
    ref: str


class NodeParamRef(BaseModel):
    param: str
    negate: bool = False


class GenomeNode(BaseModel):
    id: str
    kind: str
    params: dict[str, Any] = Field(default_factory=dict)
    inputs: list[str] = Field(default_factory=list)


class Genome(BaseModel):
    version: Literal[1]
    genome_id: str
    nodes: list[GenomeNode]
    entry_long: NodeRef
    entry_short: NodeRef
    exit_long: NodeRef
    exit_short: NodeRef
    metadata: dict[str, Any] = Field(default_factory=dict)
