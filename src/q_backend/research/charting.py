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
