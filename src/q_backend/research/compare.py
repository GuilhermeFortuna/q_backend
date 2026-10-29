"""Trade-by-trade comparison of two research backtests (Q-139)."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

import pandas as pd

from q_backend.research.results import BacktestResult

# Compared in this order; a differing pair reports the first of these fields that differs.
VALUE_FIELDS = ("side", "entry_time", "entry_price", "exit_time", "exit_price", "quantity", "pnl")
# Reasons are labels: a pair that differs only in them is matched and listed as a label difference.
LABEL_FIELDS = ("entry_reason", "exit_reason")
_NUMERIC_FIELDS = frozenset({"entry_price", "exit_price", "quantity", "pnl"})


@dataclass(frozen=True)
class TradeDifference:
    """Two trades that open at the same time but differ; ``field`` is the first field that differs.

    For a value difference the field is one of ``VALUE_FIELDS``; for a label difference, one of
    ``LABEL_FIELDS``.
    """

    field: str
    left: Mapping[str, Any]
    right: Mapping[str, Any]

    @property
    def left_value(self) -> Any:
        return self.left[self.field]

    @property
    def right_value(self) -> Any:
        return self.right[self.field]


@dataclass(frozen=True)
class ResultSummary:
    """The headline figures of one backtest, as its metrics report them."""

    total_pnl: float | None
    profit_factor: float | None
    max_drawdown: float | None


@dataclass(frozen=True)
class ComparisonReport:
    """The trade-by-trade comparison of two backtests (left against right).

    ``matched`` counts the pairs that agree on every value field, including those that differ only
    in a label; ``labels`` lists those label-only pairs. ``identical`` is true only when every trade
    matches on its values: no differing pair and no trade present in one result alone. Labels do
    not affect it.
    """

    matched: int
    differing: tuple[TradeDifference, ...]
    labels: tuple[TradeDifference, ...]
    left_only: tuple[Mapping[str, Any], ...]
    right_only: tuple[Mapping[str, Any], ...]
    left: ResultSummary
    right: ResultSummary

    @property
    def identical(self) -> bool:
        return not self.differing and not self.left_only and not self.right_only

    def __str__(self) -> str:
        lines = [
            f"matched {self.matched}, differing {len(self.differing)}, "
            f"label only {len(self.labels)}, "
            f"left only {len(self.left_only)}, right only {len(self.right_only)}",
            f"total PnL: {_fmt(self.left.total_pnl)} | {_fmt(self.right.total_pnl)}",
            f"profit factor: {_fmt(self.left.profit_factor)} | {_fmt(self.right.profit_factor)}",
            f"max drawdown: {_fmt(self.left.max_drawdown)} | {_fmt(self.right.max_drawdown)}",
        ]
        for diff in self.differing:
            lines.append(
                f"differs at {_stamp(diff.left)} on {diff.field}: {diff.left_value!r} (left) != {diff.right_value!r} (right)"
            )
        for diff in self.labels:
            lines.append(
                f"label differs at {_stamp(diff.left)} on {diff.field}: {diff.left_value!r} (left) != {diff.right_value!r} (right)"
            )
        for trade in self.left_only:
            lines.append(f"left only: {_stamp(trade)} {trade.get('side')} @ {trade.get('entry_price')}")
        for trade in self.right_only:
            lines.append(f"right only: {_stamp(trade)} {trade.get('side')} @ {trade.get('entry_price')}")
        return "\n".join(lines)


def compare_results(a: BacktestResult, b: BacktestResult, *, tolerance: float = 0.0) -> ComparisonReport:
    """Compare two backtests trade by trade.

    Trades are paired by entry time: within one entry time the n-th trade of ``a`` pairs with the
    n-th of ``b``. A pair differs when any of entry and exit time, price, side, quantity or PnL
    differ; prices, quantity and PnL may differ by up to ``tolerance``. A pair that agrees on those
    but differs in its entry or exit reason is matched and listed in ``labels``. Trades that open
    at a time the other result does not have are reported as one-sided.

    Parameters
    ----------
    a, b : BacktestResult
        The results to compare; ``a`` is the left side of the report.
    tolerance : float, default 0.0
        Largest absolute difference accepted for a numeric field.

    Returns
    -------
    ComparisonReport
    """
    if not isinstance(a, BacktestResult) or not isinstance(b, BacktestResult):
        raise TypeError("compare_results compares two BacktestResult objects")
    if isinstance(tolerance, bool) or not isinstance(tolerance, (int, float)) or tolerance < 0:
        raise ValueError(f"tolerance must be a non-negative number, got {tolerance!r}")

    left_rows = _rows(a)
    right_rows = _rows(b)
    right_by_time: dict[pd.Timestamp, list[int]] = defaultdict(list)
    for index, row in enumerate(right_rows):
        right_by_time[row["entry_time"]].append(index)

    used: set[int] = set()
    matched = 0
    differing: list[TradeDifference] = []
    labels: list[TradeDifference] = []
    left_only: list[Mapping[str, Any]] = []
    for row in left_rows:
        candidates = right_by_time.get(row["entry_time"])
        if not candidates:
            left_only.append(row)
            continue
        index = candidates.pop(0)
        used.add(index)
        other = right_rows[index]
        field = _first_difference(row, other, VALUE_FIELDS, tolerance)
        if field is not None:
            differing.append(TradeDifference(field=field, left=row, right=other))
            continue
        matched += 1
        label = _first_difference(row, other, LABEL_FIELDS, tolerance)
        if label is not None:
            labels.append(TradeDifference(field=label, left=row, right=other))
    right_only = [row for index, row in enumerate(right_rows) if index not in used]
    return ComparisonReport(
        matched=matched,
        differing=tuple(differing),
        labels=tuple(labels),
        left_only=tuple(left_only),
        right_only=tuple(right_only),
        left=_summary(a),
        right=_summary(b),
    )


def _rows(result: BacktestResult) -> list[dict[str, Any]]:
    trades = result.trades
    if trades.empty:
        return []
    ordered = trades.sort_values(["entry_time", "side", "entry_price"], kind="stable")
    rows = []
    for record in ordered.to_dict(orient="records"):
        row = {name: _clean(record.get(name)) for name in VALUE_FIELDS + LABEL_FIELDS}
        rows.append(row)
    return rows


def _clean(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, float) and value != value:
        return None
    if value is pd.NaT:
        return None
    return value


def _first_difference(
    left: Mapping[str, Any], right: Mapping[str, Any], fields: tuple[str, ...], tolerance: float
) -> str | None:
    for field in fields:
        a_value, b_value = left.get(field), right.get(field)
        if a_value is None or b_value is None:
            if a_value is not b_value:
                return field
            continue
        if field in _NUMERIC_FIELDS:
            if abs(float(a_value) - float(b_value)) > tolerance:
                return field
        elif a_value != b_value:
            return field
    return None


def _summary(result: BacktestResult) -> ResultSummary:
    metrics = result.metrics
    return ResultSummary(
        total_pnl=_number(metrics.get("total_pnl")),
        profit_factor=_number(metrics.get("profit_factor")),
        max_drawdown=_number(metrics.get("max_drawdown_value")),
    )


def _number(value: Any) -> float | None:
    return None if value is None else float(value)


def _fmt(value: float | None) -> str:
    return "n/a" if value is None else f"{value:,.2f}"


def _stamp(trade: Mapping[str, Any]) -> str:
    entry = trade.get("entry_time")
    return "n/a" if entry is None else str(entry)
