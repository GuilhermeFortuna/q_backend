"""One-call charts for research scripts: draw common plots and publish them.

Each function takes plain pandas or numpy data, draws it with Matplotlib in the Q
dark style and publishes the result with ``publish_figure``. The caller never
writes plotting code, and each function returns the stored figure id.
"""

from __future__ import annotations

from itertools import cycle
from typing import Any, Optional

import numpy as np
import pandas as pd
from matplotlib.figure import Figure
from matplotlib.ticker import PercentFormatter

from q_backend.research.figure_style import (
    BG_CARBON_950,
    EDGE_CARBON_700,
    GRID_CARBON_800,
    MUTED_SILVER_400,
    PLOT_CARBON_900,
    SERIES_PALETTE,
    SUBTEXT_SILVER_200,
    TEXT_SILVER_100,
)
from q_backend.research.figures import publish_figure

NEGATIVE_COLOR = "#fb7185"
POSITIVE_COLOR = SERIES_PALETTE[0]


def _as_frame(data: Any) -> pd.DataFrame:
    """Return the data as a non-empty DataFrame with one column per series."""
    if isinstance(data, pd.DataFrame):
        frame = data
    elif isinstance(data, pd.Series):
        name = data.name if data.name is not None else "value"
        frame = data.to_frame(name=name)
    else:
        frame = pd.Series(np.asarray(data, dtype=float), name="value").to_frame()
    if frame.empty:
        raise ValueError("There is no data to plot.")
    return frame


def _as_series(data: Any) -> pd.Series:
    """Return the data as one Series; a DataFrame must have exactly one column."""
    frame = _as_frame(data)
    if frame.shape[1] != 1:
        raise ValueError("This plot takes one series; pass a Series or a one-column DataFrame.")
    return frame.iloc[:, 0]


def _styled_axes() -> tuple[Figure, Any]:
    fig = Figure(figsize=(10, 5), facecolor=BG_CARBON_950)
    ax = fig.add_subplot()
    ax.set_facecolor(PLOT_CARBON_900)
    for spine in ax.spines.values():
        spine.set_color(EDGE_CARBON_700)
    ax.tick_params(colors=MUTED_SILVER_400)
    ax.grid(True, color=GRID_CARBON_800, alpha=0.5)
    ax.set_axisbelow(True)
    return fig, ax


def _finish(
    fig: Figure,
    ax: Any,
    *,
    title: str,
    xlabel: Optional[str],
    ylabel: Optional[str],
    legend: bool,
    collection: Optional[str],
    notes: Optional[str],
    replace: bool,
    api_url: Optional[str],
    client: Any,
) -> str:
    ax.set_title(title, color=TEXT_SILVER_100)
    if xlabel is not None:
        ax.set_xlabel(xlabel, color=SUBTEXT_SILVER_200)
    if ylabel is not None:
        ax.set_ylabel(ylabel, color=SUBTEXT_SILVER_200)
    if legend:
        ax.legend(facecolor=PLOT_CARBON_900, edgecolor=EDGE_CARBON_700, labelcolor=SUBTEXT_SILVER_200)
    return publish_figure(
        fig,
        title=title,
        collection=collection,
        notes=notes,
        replace=replace,
        api_url=api_url,
        client=client,
    )


def line(
    data: Any,
    *,
    title: str,
    xlabel: Optional[str] = None,
    ylabel: Optional[str] = None,
    collection: Optional[str] = None,
    notes: Optional[str] = None,
    replace: bool = True,
    api_url: Optional[str] = None,
    client: Any = None,
) -> str:
    """Publish a line chart. A DataFrame draws one line per column, with a legend.

    The index is the x axis, so a DatetimeIndex gives a time axis.
    """
    frame = _as_frame(data)
    fig, ax = _styled_axes()
    for color, column in zip(cycle(SERIES_PALETTE), frame.columns):
        ax.plot(frame.index, frame[column], color=color, linewidth=1.5, label=str(column))
    return _finish(
        fig,
        ax,
        title=title,
        xlabel=xlabel,
        ylabel=ylabel,
        legend=frame.shape[1] > 1,
        collection=collection,
        notes=notes,
        replace=replace,
        api_url=api_url,
        client=client,
    )


def drawdown(
    equity: Any,
    *,
    title: str,
    collection: Optional[str] = None,
    notes: Optional[str] = None,
    replace: bool = True,
    api_url: Optional[str] = None,
    client: Any = None,
) -> str:
    """Publish the drawdown from the running peak of an equity curve, as a filled chart."""
    series = _as_series(equity).astype(float)
    drawdown_series = series / series.cummax() - 1.0
    fig, ax = _styled_axes()
    ax.fill_between(drawdown_series.index, drawdown_series.to_numpy(), 0.0, color=NEGATIVE_COLOR, alpha=0.6)
    ax.plot(drawdown_series.index, drawdown_series.to_numpy(), color=NEGATIVE_COLOR, linewidth=1.0)
    ax.yaxis.set_major_formatter(PercentFormatter(1.0))
    return _finish(
        fig,
        ax,
        title=title,
        xlabel=None,
        ylabel="Drawdown",
        legend=False,
        collection=collection,
        notes=notes,
        replace=replace,
        api_url=api_url,
        client=client,
    )


def bar(
    values: Any,
    *,
    title: str,
    xlabel: Optional[str] = None,
    ylabel: Optional[str] = None,
    collection: Optional[str] = None,
    notes: Optional[str] = None,
    replace: bool = True,
    api_url: Optional[str] = None,
    client: Any = None,
) -> str:
    """Publish a bar chart, one bar per index label.

    A single series colours positive bars gold and negative bars rose. A DataFrame
    draws one bar per column for each label, with a legend.
    """
    frame = _as_frame(values)
    fig, ax = _styled_axes()
    positions = np.arange(len(frame.index))
    count = frame.shape[1]
    width = 0.8 / count
    for offset, (color, column) in enumerate(zip(cycle(SERIES_PALETTE), frame.columns)):
        heights = frame[column].to_numpy(dtype=float)
        if count == 1:
            colors = [POSITIVE_COLOR if height >= 0 else NEGATIVE_COLOR for height in heights]
        else:
            colors = color
        x = positions + (offset - (count - 1) / 2) * width
        ax.bar(x, heights, width=width, color=colors, label=str(column))
    ax.set_xticks(positions, [str(label) for label in frame.index], rotation=45, ha="right")
    ax.axhline(0.0, color=EDGE_CARBON_700, linewidth=1.0)
    return _finish(
        fig,
        ax,
        title=title,
        xlabel=xlabel,
        ylabel=ylabel,
        legend=count > 1,
        collection=collection,
        notes=notes,
        replace=replace,
        api_url=api_url,
        client=client,
    )


def histogram(
    values: Any,
    *,
    title: str,
    bins: int = 50,
    xlabel: Optional[str] = None,
    collection: Optional[str] = None,
    notes: Optional[str] = None,
    replace: bool = True,
    api_url: Optional[str] = None,
    client: Any = None,
) -> str:
    """Publish a histogram of one series. Missing values are left out."""
    samples = _as_series(values).astype(float).dropna().to_numpy()
    if samples.size == 0:
        raise ValueError("There is no data to plot.")
    fig, ax = _styled_axes()
    ax.hist(samples, bins=bins, color=POSITIVE_COLOR, alpha=0.85, edgecolor=BG_CARBON_950)
    ax.set_ylabel("Count", color=SUBTEXT_SILVER_200)
    return _finish(
        fig,
        ax,
        title=title,
        xlabel=xlabel,
        ylabel=None,
        legend=False,
        collection=collection,
        notes=notes,
        replace=replace,
        api_url=api_url,
        client=client,
    )


def scatter(
    x: Any,
    y: Any,
    *,
    title: str,
    xlabel: Optional[str] = None,
    ylabel: Optional[str] = None,
    collection: Optional[str] = None,
    notes: Optional[str] = None,
    replace: bool = True,
    api_url: Optional[str] = None,
    client: Any = None,
) -> str:
    """Publish a scatter plot of two equally long sequences."""
    x_values = np.asarray(x, dtype=float)
    y_values = np.asarray(y, dtype=float)
    if x_values.shape != y_values.shape or x_values.ndim != 1:
        raise ValueError("x and y must be one-dimensional sequences of the same length.")
    if x_values.size == 0:
        raise ValueError("There is no data to plot.")
    fig, ax = _styled_axes()
    ax.scatter(x_values, y_values, s=14, color=POSITIVE_COLOR, alpha=0.7, edgecolors="none")
    return _finish(
        fig,
        ax,
        title=title,
        xlabel=xlabel,
        ylabel=ylabel,
        legend=False,
        collection=collection,
        notes=notes,
        replace=replace,
        api_url=api_url,
        client=client,
    )
