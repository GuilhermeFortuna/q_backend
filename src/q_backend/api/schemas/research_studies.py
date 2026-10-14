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
StudyKind = Literal["line", "step", "markers", "histogram"]
StudyMarkerShape = Literal["circle", "square", "diamond", "triangle-up", "triangle-down", "cross"]
StudyMarkerPosition = Literal["value", "above-bar", "below-bar"]

_KIND_FIELDS = {
    "line": {"color", "lineStyle", "lineWidth", "fillTo", "fillOpacity"},
    "step": {"color", "lineStyle", "lineWidth", "fillTo", "fillOpacity"},
    "markers": {"color", "shape", "size", "position"},
    "histogram": {"color", "negativeColor", "baseline"},
}


def _validate_series_style(values: Any) -> Any:
    if not isinstance(values, dict):
        return values
    kind = values.get("kind", "line")
    if not isinstance(kind, str) or kind not in _KIND_FIELDS:
        return values  # Literal validation reports the invalid kind.
    supplied = set(values) - {"key", "label", "pane", "values", "kind"}
    unsupported = supplied - _KIND_FIELDS[kind]
    if unsupported:
        raise ValueError(f"fields {sorted(unsupported)} do not apply to kind {kind!r}")
    pane = values.get("pane")
    if kind == "histogram" and pane != "oscillator":
        raise ValueError("histogram series must use the oscillator pane")
    if kind == "markers" and values.get("position", "value") in ("above-bar", "below-bar") and pane != "price":
        raise ValueError("above-bar and below-bar markers must use the price pane")
    return values


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
    model_config = ConfigDict(populate_by_name=True, extra="forbid")

    key: str = Field(min_length=1)
    label: str = Field(min_length=1)
    pane: StudyPane
    color: Optional[str] = None
    line_style: Optional[StudyLineStyle] = Field(default=None, alias="lineStyle")
    line_width: Optional[float] = Field(default=None, alias="lineWidth", gt=0)
    kind: StudyKind = "line"
    shape: Optional[StudyMarkerShape] = None
    size: Optional[float] = Field(default=None, gt=0, le=32)
    position: Optional[StudyMarkerPosition] = None
    negative_color: Optional[str] = Field(default=None, alias="negativeColor")
    baseline: Optional[float] = None
    fill_to: Optional[str] = Field(default=None, alias="fillTo")
    fill_opacity: Optional[float] = Field(default=None, alias="fillOpacity", gt=0, le=1)
    values: list[Optional[float]]

    @model_validator(mode="before")
    @classmethod
    def _style_fields_apply_to_kind(cls, values: Any) -> Any:
        return _validate_series_style(values)

    @field_validator("size", "baseline", "fill_opacity", mode="before")
    @classmethod
    def _new_numbers_are_finite(cls, value: Any) -> Any:
        if value is None:
            return value
        try:
            finite = not isinstance(value, bool) and math.isfinite(float(value))
        except (TypeError, ValueError):
            finite = False
        if not finite:
            raise ValueError("study style values must be finite numbers")
        return value

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
        series_by_key = {item.key: item for item in self.series}
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
            if item.fill_to is not None:
                target = series_by_key.get(item.fill_to)
                if item.fill_to == item.key:
                    raise ValueError(f"series {item.key!r} cannot fill to itself")
                if target is None:
                    raise ValueError(f"fillTo target {item.fill_to!r} does not exist")
                if target.pane != item.pane:
                    raise ValueError(f"fillTo target {item.fill_to!r} must use the same pane")
                if target.kind not in {"line", "step"}:
                    raise ValueError(f"fillTo target {item.fill_to!r} must be line or step")
        return self


class ResearchStudyCreateResponse(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    study_id: str = Field(alias="studyId")
    replaced: bool


class ResearchStudySeriesMetadata(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="forbid")

    key: str
    label: str
    pane: StudyPane
    color: Optional[str] = None
    line_style: Optional[StudyLineStyle] = Field(default=None, alias="lineStyle")
    line_width: Optional[float] = Field(default=None, alias="lineWidth")
    kind: StudyKind = "line"
    shape: Optional[StudyMarkerShape] = None
    size: Optional[float] = Field(default=None, gt=0, le=32)
    position: Optional[StudyMarkerPosition] = None
    negative_color: Optional[str] = Field(default=None, alias="negativeColor")
    baseline: Optional[float] = None
    fill_to: Optional[str] = Field(default=None, alias="fillTo")
    fill_opacity: Optional[float] = Field(default=None, alias="fillOpacity", gt=0, le=1)

    @model_validator(mode="before")
    @classmethod
    def _style_fields_apply_to_kind(cls, values: Any) -> Any:
        return _validate_series_style(values)

    @field_validator("size", "baseline", "fill_opacity", mode="before")
    @classmethod
    def _new_numbers_are_finite(cls, value: Any) -> Any:
        if value is None:
            return value
        try:
            finite = not isinstance(value, bool) and math.isfinite(float(value))
        except (TypeError, ValueError):
            finite = False
        if not finite:
            raise ValueError("study style values must be finite numbers")
        return value


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
