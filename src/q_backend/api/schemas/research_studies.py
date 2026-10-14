from __future__ import annotations

import math
from datetime import datetime, timezone
from typing import Any, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from q_backend.api.schemas.research_figures import ResearchFigureProvenance

CHART_TIMEFRAMES: tuple[str, ...] = ("M1", "M5", "M10", "M15", "M30", "H1", "H2", "H4", "D1")
STUDY_PANES: tuple[str, ...] = ("price", "oscillator")
STUDY_LINE_STYLES: tuple[str, ...] = ("solid", "dashed", "dotted")
RESERVED_STUDY_KEY = "time"

StudyPane = Literal["price", "oscillator"]
StudyLineStyle = Literal["solid", "dashed", "dotted"]


def parse_utc_timestamps(values: list[str]) -> list[datetime]:
    """Parse timezone-aware ISO-8601 strings into UTC datetimes; naive input is rejected."""
    parsed: list[datetime] = []
    for raw in values:
        try:
            moment = datetime.fromisoformat(raw)
        except ValueError as exc:
            raise ValueError(f"timestamp {raw!r} is not ISO-8601") from exc
        if moment.tzinfo is None:
            raise ValueError(f"timestamp {raw!r} must include a UTC offset")
        parsed.append(moment.astimezone(timezone.utc))
    return parsed


class ResearchStudySeriesInput(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    key: str = Field(min_length=1)
    label: str = Field(min_length=1)
    pane: StudyPane
    color: Optional[str] = None
    line_style: Optional[StudyLineStyle] = Field(default=None, alias="lineStyle")
    line_width: Optional[float] = Field(default=None, alias="lineWidth", gt=0)
    values: list[Optional[float]]

    @field_validator("values")
    @classmethod
    def _values_are_finite(cls, values: list[Optional[float]]) -> list[Optional[float]]:
        for value in values:
            if value is not None and not math.isfinite(value):
                raise ValueError("values must be finite numbers or null")
        return values


class ResearchStudyCreateRequest(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    name: str = Field(min_length=1, max_length=120)
    symbol: str = Field(min_length=1)
    timeframe: str
    notes: Optional[str] = Field(default=None, max_length=4000)
    replace: bool = True
    provenance: Optional[ResearchFigureProvenance] = None
    timestamps: list[str]
    series: list[ResearchStudySeriesInput] = Field(min_length=1, max_length=32)

    @model_validator(mode="after")
    def _check_study(self) -> "ResearchStudyCreateRequest":
        if not self.name.strip():
            raise ValueError("name must not be blank")
        symbol = self.symbol.strip().upper()
        if not symbol:
            raise ValueError("symbol must not be blank")
        self.symbol = symbol
        timeframe = self.timeframe.strip().upper()
        if timeframe not in CHART_TIMEFRAMES:
            raise ValueError(f"timeframe must be one of {list(CHART_TIMEFRAMES)}, got {self.timeframe!r}")
        self.timeframe = timeframe
        self.name = self.name.strip()

        if not self.timestamps:
            raise ValueError("timestamps must not be empty")
        parsed = parse_utc_timestamps(self.timestamps)
        for previous, current in zip(parsed, parsed[1:]):
            if current <= previous:
                raise ValueError("timestamps must be strictly increasing")

        keys: set[str] = set()
        for item in self.series:
            if item.key == RESERVED_STUDY_KEY:
                raise ValueError(f"series key {RESERVED_STUDY_KEY!r} is reserved")
            if item.key in keys:
                raise ValueError(f"duplicate series key {item.key!r}")
            keys.add(item.key)
            if len(item.values) != len(parsed):
                raise ValueError(f"series {item.key!r} has {len(item.values)} values for {len(parsed)} timestamps")
            if all(value is None for value in item.values):
                raise ValueError(f"series {item.key!r} has no values")
        return self


class ResearchStudyCreateResponse(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    study_id: str = Field(alias="studyId")
    replaced: bool


class ResearchStudySeriesMetadata(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    key: str
    label: str
    pane: StudyPane
    color: Optional[str] = None
    line_style: Optional[StudyLineStyle] = Field(default=None, alias="lineStyle")
    line_width: Optional[float] = Field(default=None, alias="lineWidth")


class ResearchStudyMetadata(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    study_id: str = Field(alias="studyId")
    name: str
    symbol: str
    timeframe: str
    notes: Optional[str] = None
    series: list[ResearchStudySeriesMetadata]
    start: Optional[str] = None
    end: Optional[str] = None
    point_count: int = Field(alias="pointCount")
    revision: int
    created_at: str = Field(alias="createdAt")
    updated_at: str = Field(alias="updatedAt")
    provenance: Optional[ResearchFigureProvenance] = None


class ResearchStudyListResponse(BaseModel):
    items: list[ResearchStudyMetadata]


class ResearchStudyRenameRequest(BaseModel):
    name: str = Field(min_length=1, max_length=120)

    @field_validator("name")
    @classmethod
    def _name_not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("name must not be blank")
        return value.strip()


class ResearchStudySeriesValues(BaseModel):
    key: str
    values: list[Optional[float]]


class ResearchStudySeriesResponse(BaseModel):
    timestamps: list[str]
    series: list[ResearchStudySeriesValues]


def study_series_metadata(items: list[dict[str, Any]]) -> list[ResearchStudySeriesMetadata]:
    return [ResearchStudySeriesMetadata.model_validate(item) for item in items]
