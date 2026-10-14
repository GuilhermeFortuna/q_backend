"""Causal swing detection: confirmed tops and bottoms without look-ahead.

``LiveSwingDetector`` consumes one completed bar at a time and emits a pivot only once
price has retreated from the running extreme by ``reversal_atr`` times the Wilder ATR.
Pivots are therefore known no earlier than the bar that confirms them.
"""

from __future__ import annotations

from dataclasses import dataclass
from math import inf, nan
from typing import Literal

import numpy as np

__all__ = ("SwingPivot", "LiveSwingDetector")


@dataclass(frozen=True)
class SwingPivot:
    """A confirmed top or bottom.

    ``pivot_index`` is the bar holding the extreme. ``confirmed_index`` is the bar whose
    close proved the reversal; it is always at or after ``pivot_index`` and is the earliest
    bar on which the pivot could have been known in live trading.
    """

    kind: Literal["top", "bottom"]
    pivot_index: object
    price: float
    confirmed_index: object


class LiveSwingDetector:
    """Causal ATR-threshold zigzag for live top and bottom detection.

    Bars are fed one at a time through ``update``. Within each leg the detector tracks the
    running extreme; a top (bottom) is confirmed once price falls (rises) from that extreme by
    at least ``reversal_atr * ATR``. Confirmed pivots are never revised and alternate
    top/bottom by construction.

    No look-ahead: ATR is updated recursively (Wilder) from bars seen so far, and the
    reversal test on a bar uses only extremes set by earlier bars. A bar that confirms a
    reversal does not extend the leg it terminates, because its intrabar order is unknown.

    Warm-up: no pivots are emitted until ATR is available (``atr_period`` bars). The first
    leg direction is set by the first move of ``reversal_atr * ATR`` from the running
    extremes since the first bar.

    Parameters
    ----------
    atr_period : int, default 14
        Wilder ATR period. Must be >= 1.
    reversal_atr : float, default 2.0
        Reversal size in ATR multiples. Must be > 0.
    """

    def __init__(self, *, atr_period: int = 14, reversal_atr: float = 2.0):
        if atr_period < 1:
            raise ValueError("atr_period must be >= 1")
        if reversal_atr <= 0:
            raise ValueError("reversal_atr must be > 0")
        self.atr_period = atr_period
        self.reversal_atr = reversal_atr
        self._prev_close = nan
        self._atr = nan
        self._tr_seed: list[float] = []
        self._run_high, self._run_high_index = -inf, None
        self._run_low, self._run_low_index = inf, None
        self._direction = 0
        self._leg_price = nan
        self._leg_index = None

    @property
    def direction(self) -> int:
        """1 while in a rising leg, -1 while falling, 0 before the first pivot is possible."""
        return self._direction

    @property
    def atr(self) -> float:
        return self._atr

    def update(self, index: object, high: float, low: float, close: float) -> SwingPivot | None:
        """Consume one completed bar and return the pivot it confirms, if any."""
        self._update_atr(high, low, close)
        if np.isnan(self._atr):
            self._track_run_extremes(index, high, low)
            return None

        threshold = self.reversal_atr * self._atr
        pivot = None

        if self._direction == 0:
            if high - self._run_low >= threshold:
                pivot = SwingPivot("bottom", self._run_low_index, self._run_low, index)
                self._set_leg(1, high, index)
            elif self._run_high - low >= threshold:
                pivot = SwingPivot("top", self._run_high_index, self._run_high, index)
                self._set_leg(-1, low, index)
            else:
                self._track_run_extremes(index, high, low)
        elif self._direction == 1:
            if low <= self._leg_price - threshold:
                pivot = SwingPivot("top", self._leg_index, self._leg_price, index)
                self._set_leg(-1, low, index)
            elif high > self._leg_price:
                self._set_leg(1, high, index)
        else:
            if high >= self._leg_price + threshold:
                pivot = SwingPivot("bottom", self._leg_index, self._leg_price, index)
                self._set_leg(1, high, index)
            elif low < self._leg_price:
                self._set_leg(-1, low, index)

        return pivot

    def _update_atr(self, high: float, low: float, close: float) -> None:
        if np.isnan(self._prev_close):
            true_range = high - low
        else:
            true_range = max(high - low, abs(high - self._prev_close), abs(low - self._prev_close))
        self._prev_close = close

        if np.isnan(self._atr):
            self._tr_seed.append(true_range)
            if len(self._tr_seed) == self.atr_period:
                self._atr = float(np.mean(self._tr_seed))
        else:
            self._atr = (self._atr * (self.atr_period - 1) + true_range) / self.atr_period

    def _track_run_extremes(self, index: object, high: float, low: float) -> None:
        if high > self._run_high:
            self._run_high, self._run_high_index = high, index
        if low < self._run_low:
            self._run_low, self._run_low_index = low, index

    def _set_leg(self, direction: int, price: float, index: object) -> None:
        self._direction = direction
        self._leg_price = price
        self._leg_index = index
