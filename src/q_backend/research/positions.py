"""Immutable snapshots of filled research positions."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

import pandas as pd


@dataclass(frozen=True, slots=True)
class ResearchPosition:
    """
    An immutable snapshot of a filled open position at the decision boundary.

    Parameters
    ----------
    symbol : str
        Instrument ticker of the position.
    side : {'long', 'short'}
        Direction of the open trade.
    entry_time : pandas.Timestamp
        Actual entry fill timestamp in the research frame's timezone.
    entry_price : float
        Actual execution fill price.
    quantity : float
        Filled contract or share quantity.
    """

    symbol: str
    side: Literal["long", "short"]
    entry_time: pd.Timestamp
    entry_price: float
    quantity: float
