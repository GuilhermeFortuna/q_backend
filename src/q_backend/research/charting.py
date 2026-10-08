"""Chart series declarations for research strategies."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

ChartPane = Literal["price", "oscillator"]

CHART_PANES: tuple[str, ...] = ("price", "oscillator")


@dataclass(frozen=True)
class ChartIndicator:
    """A computed column a research strategy asks the Trade Chart to draw."""

    column: str
    pane: ChartPane = "price"
    label: str = ""
    color: str | None = None

    def __post_init__(self) -> None:
        if self.pane not in CHART_PANES:
            raise ValueError(f"ChartIndicator pane must be one of {CHART_PANES!r}, got {self.pane!r}")
        if not self.label:
            object.__setattr__(self, "label", self.column)
