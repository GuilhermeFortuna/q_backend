from __future__ import annotations

from typing import Annotated, Any, Literal, Optional, Union

from pydantic import BaseModel, Field, model_validator


class ResearchFigureProvenance(BaseModel):
    script: Optional[str] = None
    git_revision: Optional[str] = None
    git_dirty: Optional[bool] = None


class ResearchFigureImageContent(BaseModel):
    kind: Literal["image"]
    media_type: Literal["image/png", "image/svg+xml"]
    data: str
    width: int = Field(gt=0)
    height: int = Field(gt=0)


class ResearchFigurePlotlyContent(BaseModel):
    kind: Literal["plotly"]
    figure: dict[str, Any]


ResearchFigureContent = Annotated[
    Union[ResearchFigureImageContent, ResearchFigurePlotlyContent],
    Field(discriminator="kind"),
]


class ResearchFigureCreateRequest(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    collection: str = Field(min_length=1, max_length=120)
    notes: Optional[str] = Field(default=None, max_length=4000)
    replace: bool = True
    provenance: Optional[ResearchFigureProvenance] = None
    content: ResearchFigureContent


class ResearchFigureCreateResponse(BaseModel):
    figure_id: str
    replaced: bool


class ResearchFigureMetadata(BaseModel):
    figure_id: str
    title: str
    collection: str
    notes: Optional[str] = None
    kind: str
    media_type: str
    width: Optional[int] = None
    height: Optional[int] = None
    byte_size: int
    revision: int
    created_at: str
    updated_at: str
    provenance: Optional[ResearchFigureProvenance] = None


class ResearchCollectionSummary(BaseModel):
    name: str
    count: int
    updated_at: Optional[str] = None


class ResearchFigureListResponse(BaseModel):
    items: list[ResearchFigureMetadata]
    total: int
    limit: int
    offset: int
    collections: list[ResearchCollectionSummary]


class BulkDeleteResearchFiguresRequest(BaseModel):
    figure_ids: Optional[list[str]] = None
    collection: Optional[str] = None

    @model_validator(mode="after")
    def validate_one_of(self) -> BulkDeleteResearchFiguresRequest:
        has_ids = self.figure_ids is not None
        has_col = self.collection is not None
        if (has_ids and has_col) or (not has_ids and not has_col):
            raise ValueError("Exactly one of 'figure_ids' or 'collection' must be provided.")
        if has_col and not (self.collection or "").strip():
            raise ValueError("'collection' must not be blank.")
        return self
