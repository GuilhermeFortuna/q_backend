"""Chart series declarations for research strategies."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Literal

ChartPane = Literal["price", "oscillator"]
ChartLineStyle = Literal["solid", "dashed", "dotted"]
ChartKind = Literal["line", "step", "markers", "histogram"]
ChartMarkerShape = Literal["circle", "square", "diamond", "triangle-up", "triangle-down", "cross"]
ChartMarkerPosition = Literal["value", "above-bar", "below-bar"]

CHART_PANES: tuple[str, ...] = ("price", "oscillator")
CHART_LINE_STYLES: tuple[str, ...] = ("solid", "dashed", "dotted")


@dataclass(frozen=True)
class ChartIndicator:
    """
    Visual series configuration to render on the Trade Chart in the Q desktop terminal.

    Parameters
    ----------
    column : str
        Column name in the frame computed by ``compute_indicators``.
    pane : {'price', 'oscillator'}, default 'price'
        Chart pane to draw the series in.
    label : str, optional
        Display legend label. Defaults to the column name.
    color : str, optional
        CSS color hex or name (e.g. ``'#4da3ff'``).
    line_style : {'solid', 'dashed', 'dotted'}, optional
        Stroke style for line rendering.
    line_width : float, optional
        Stroke thickness in pixels (positive finite float).
    """

    column: str
    pane: ChartPane = "price"
    label: str = ""
    color: str | None = None
    line_style: ChartLineStyle | None = None
    line_width: float | None = None
    kind: ChartKind = "line"
    shape: ChartMarkerShape | None = None
    size: float | None = None
    position: ChartMarkerPosition | None = None
    negative_color: str | None = None
    baseline: float | None = None
    fill_to: str | None = None
    fill_opacity: float | None = None

    def __post_init__(self) -> None:
        if self.kind == "markers":
            if self.shape is None:
                object.__setattr__(self, "shape", "circle")
            if self.size is None:
                object.__setattr__(self, "size", 8)
            if self.position is None:
                object.__setattr__(self, "position", "value")
        elif self.kind == "histogram" and self.baseline is None:
            object.__setattr__(self, "baseline", 0)
        if self.kind in {"line", "step"} and self.fill_to is not None and self.fill_opacity is None:
            object.__setattr__(self, "fill_opacity", 0.15)
        if self.pane not in CHART_PANES:
            raise ValueError(f"ChartIndicator pane must be one of {CHART_PANES!r}, got {self.pane!r}")
        if self.line_style is not None and self.line_style not in CHART_LINE_STYLES:
            raise ValueError(f"ChartIndicator line_style must be one of {CHART_LINE_STYLES!r}, got {self.line_style!r}")
        if self.line_width is not None and (
            isinstance(self.line_width, bool) or not math.isfinite(self.line_width) or self.line_width <= 0
        ):
            raise ValueError(f"ChartIndicator line_width must be a positive finite number, got {self.line_width!r}")
        allowed = {
            "line": {"color", "line_style", "line_width", "fill_to", "fill_opacity"},
            "step": {"color", "line_style", "line_width", "fill_to", "fill_opacity"},
            "markers": {"color", "shape", "size", "position"},
            "histogram": {"color", "negative_color", "baseline"},
        }
        if not isinstance(self.kind, str) or self.kind not in allowed:
            raise ValueError(f"ChartIndicator kind must be one of {tuple(allowed)!r}, got {self.kind!r}")
        if self.shape is not None and self.shape not in (
            "circle",
            "square",
            "diamond",
            "triangle-up",
            "triangle-down",
            "cross",
        ):
            raise ValueError(f"ChartIndicator shape is invalid: {self.shape!r}")
        if self.position is not None and self.position not in ("value", "above-bar", "below-bar"):
            raise ValueError(f"ChartIndicator position is invalid: {self.position!r}")
        supplied = {
            field
            for field in (
                "color",
                "line_style",
                "line_width",
                "shape",
                "size",
                "position",
                "negative_color",
                "baseline",
                "fill_to",
                "fill_opacity",
            )
            if getattr(self, field) is not None
        }
        unsupported = supplied - allowed[self.kind]
        if unsupported:
            raise ValueError(f"ChartIndicator fields {sorted(unsupported)} do not apply to kind {self.kind!r}")
        if self.kind == "histogram" and self.pane != "oscillator":
            raise ValueError("ChartIndicator histogram must use the oscillator pane")
        if self.position in {"above-bar", "below-bar"} and self.pane != "price":
            raise ValueError("ChartIndicator above-bar and below-bar markers must use the price pane")
        if self.size is not None and (
            isinstance(self.size, bool) or not math.isfinite(self.size) or self.size <= 0 or self.size > 32
        ):
            raise ValueError(f"ChartIndicator size must be greater than 0 and at most 32, got {self.size!r}")
        if self.baseline is not None and (isinstance(self.baseline, bool) or not math.isfinite(self.baseline)):
            raise ValueError(f"ChartIndicator baseline must be finite, got {self.baseline!r}")
        if self.fill_opacity is not None and (
            isinstance(self.fill_opacity, bool)
            or not math.isfinite(self.fill_opacity)
            or self.fill_opacity <= 0
            or self.fill_opacity > 1
        ):
            raise ValueError(
                f"ChartIndicator fill_opacity must be greater than 0 and at most 1, got {self.fill_opacity!r}"
            )
        if not self.label:
            object.__setattr__(self, "label", self.column)
