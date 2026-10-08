"""Immutable snapshots of filled research positions."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

import pandas as pd


@dataclass(frozen=True, slots=True)
class ResearchPosition:
    """An actual open position at the current closed-bar decision boundary.

    Pending entry and exit requests are not fills. ``entry_time`` uses the
    research frame timezone; price and quantity include actual engine sizing.
    """

    symbol: str
    side: Literal["long", "short"]
    entry_time: pd.Timestamp
    entry_price: float
    quantity: float
