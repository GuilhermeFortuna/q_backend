"""Chart series declarations for research strategies."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Literal

ChartPane = Literal["price", "oscillator"]
ChartLineStyle = Literal["solid", "dashed", "dotted"]

CHART_PANES: tuple[str, ...] = ("price", "oscillator")
CHART_LINE_STYLES: tuple[str, ...] = ("solid", "dashed", "dotted")


@dataclass(frozen=True)
class ChartIndicator:
    """A computed column a research strategy asks the Trade Chart to draw."""

    column: str
    pane: ChartPane = "price"
    label: str = ""
    color: str | None = None
    line_style: ChartLineStyle | None = None
    line_width: float | None = None

    def __post_init__(self) -> None:
        if self.pane not in CHART_PANES:
            raise ValueError(f"ChartIndicator pane must be one of {CHART_PANES!r}, got {self.pane!r}")
        if self.line_style is not None and self.line_style not in CHART_LINE_STYLES:
            raise ValueError(f"ChartIndicator line_style must be one of {CHART_LINE_STYLES!r}, got {self.line_style!r}")
        if self.line_width is not None and (
            isinstance(self.line_width, bool) or not math.isfinite(self.line_width) or self.line_width <= 0
        ):
            raise ValueError(f"ChartIndicator line_width must be a positive finite number, got {self.line_width!r}")
        if not self.label:
            object.__setattr__(self, "label", self.column)
