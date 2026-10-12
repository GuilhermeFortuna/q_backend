"""One-call interactive charts for research scripts: draw and publish.

Each function takes plain pandas or numpy data, draws it with Plotly in the Q dark
style and publishes the result with ``publish_figure``. The figures stay interactive
in the Figures tab (zoom, pan, hover). The caller never writes plotting code, and
each function returns the stored figure id.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from itertools import cycle
from typing import Any, Literal, Optional, Union

import numpy as np
import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots

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
POSITIVE_COLOR = "#34d399"
GAIN_BAR_COLOR = SERIES_PALETTE[0]
DRAWDOWN_FILL = "rgba(251, 113, 133, 0.35)"
MARKER_COLOR = SERIES_PALETTE[0]


@dataclass(frozen=True)
class Indicator:
    """An indicator drawn as a line (default) or as markers.

    ``data`` is a pandas Series, or a numpy array aligned with the candles' index.
    Use ``kind="scatter"`` for markers, such as entries or signals.
    """

    data: Any
    kind: Literal["line", "scatter"] = "line"
    color: Optional[str] = None


IndicatorSpec = Union[Indicator, Any]


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


def _as_series(data: Any, index: Optional[pd.Index] = None) -> pd.Series:
    """Return the data as one Series; a DataFrame must have exactly one column."""
    if isinstance(data, pd.Series):
        return data
    if isinstance(data, pd.DataFrame):
        if data.shape[1] != 1:
            raise ValueError("This plot takes one series; pass a Series or a one-column DataFrame.")
        return data.iloc[:, 0]
    values = np.asarray(data, dtype=float)
    if values.ndim != 1:
        raise ValueError("Indicator values must be one-dimensional.")
    if index is not None and len(index) != len(values):
        raise ValueError("Indicator values must have the same length as the candles.")
    return pd.Series(values, index=index)


def _layout(fig: go.Figure, *, title: str, height: int = 480, show_legend: bool = False) -> None:
    """Apply the Q dark style to a figure; each chart sets its own axes labels."""
    fig.update_layout(
        title=dict(text=title, font=dict(color=TEXT_SILVER_100, size=16)),
        paper_bgcolor=BG_CARBON_950,
        plot_bgcolor=PLOT_CARBON_900,
        font=dict(color=SUBTEXT_SILVER_200),
        height=height,
        showlegend=show_legend,
        legend=dict(bgcolor=PLOT_CARBON_900, bordercolor=EDGE_CARBON_700, font=dict(color=SUBTEXT_SILVER_200)),
        margin=dict(l=60, r=24, t=60, b=50),
        hovermode="x unified",
    )
    axis = dict(
        gridcolor=GRID_CARBON_800,
        zerolinecolor=EDGE_CARBON_700,
        linecolor=EDGE_CARBON_700,
        tickfont=dict(color=MUTED_SILVER_400),
        title=dict(font=dict(color=SUBTEXT_SILVER_200)),
    )
    fig.update_xaxes(**axis, rangeslider_visible=False)
    fig.update_yaxes(**axis)


def _axis_titles(fig: go.Figure, *, xlabel: Optional[str], ylabel: Optional[str]) -> None:
    if xlabel is not None:
        fig.update_xaxes(title_text=xlabel)
    if ylabel is not None:
        fig.update_yaxes(title_text=ylabel)


def _finish(
    fig: go.Figure,
    *,
    title: str,
    collection: Optional[str],
    notes: Optional[str],
    replace: bool,
    api_url: Optional[str],
    client: Any,
) -> str:
    return publish_figure(
        fig,
        title=title,
        collection=collection,
        notes=notes,
        replace=replace,
        api_url=api_url,
        client=client,
    )


def _indicator_trace(
    name: str,
    spec: IndicatorSpec,
    color: str,
    index: Optional[pd.Index],
) -> go.Scatter:
    if isinstance(spec, Indicator):
        series = _as_series(spec.data, index=index)
        kind, color = spec.kind, spec.color or color
    else:
        series = _as_series(spec, index=index)
        kind = "line"
    if kind == "scatter":
        return go.Scatter(
            x=series.index,
            y=series.to_numpy(),
            mode="markers",
            name=name,
            marker=dict(color=color, size=7, opacity=0.85),
        )
    return go.Scatter(
        x=series.index,
        y=series.to_numpy(),
        mode="lines",
        name=name,
        line=dict(color=color, width=1.5),
        connectgaps=False,
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
    fig = go.Figure()
    for color, column in zip(cycle(SERIES_PALETTE), frame.columns):
        fig.add_trace(
            go.Scatter(
                x=frame.index,
                y=frame[column].to_numpy(),
                mode="lines",
                name=str(column),
                line=dict(color=color, width=1.5),
                connectgaps=False,
            )
        )
    _layout(fig, title=title, show_legend=frame.shape[1] > 1)
    _axis_titles(fig, xlabel=xlabel, ylabel=ylabel)
    return _finish(
        fig, title=title, collection=collection, notes=notes, replace=replace, api_url=api_url, client=client
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
    fig = go.Figure(
        go.Scatter(
            x=drawdown_series.index,
            y=drawdown_series.to_numpy(),
            mode="lines",
            name="Drawdown",
            line=dict(color=NEGATIVE_COLOR, width=1.0),
            fill="tozeroy",
            fillcolor=DRAWDOWN_FILL,
        )
    )
    _layout(fig, title=title)
    fig.update_yaxes(tickformat=".0%", title_text="Drawdown")
    return _finish(
        fig, title=title, collection=collection, notes=notes, replace=replace, api_url=api_url, client=client
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

    A single series colours gains gold and losses rose. A DataFrame draws one bar per
    column for each label, grouped, with a legend.
    """
    frame = _as_frame(values)
    fig = go.Figure()
    count = frame.shape[1]
    for color, column in zip(cycle(SERIES_PALETTE), frame.columns):
        heights = frame[column].to_numpy(dtype=float)
        if count == 1:
            colors = [GAIN_BAR_COLOR if height >= 0 else NEGATIVE_COLOR for height in heights]
        else:
            colors = color
        fig.add_trace(go.Bar(x=frame.index, y=heights, name=str(column), marker=dict(color=colors)))
    fig.update_layout(barmode="group")
    _layout(fig, title=title, show_legend=count > 1)
    _axis_titles(fig, xlabel=xlabel, ylabel=ylabel)
    return _finish(
        fig, title=title, collection=collection, notes=notes, replace=replace, api_url=api_url, client=client
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
    fig = go.Figure(go.Histogram(x=samples, nbinsx=bins, marker=dict(color=GAIN_BAR_COLOR), opacity=0.85))
    _layout(fig, title=title)
    _axis_titles(fig, xlabel=xlabel, ylabel="Count")
    return _finish(
        fig, title=title, collection=collection, notes=notes, replace=replace, api_url=api_url, client=client
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
    fig = go.Figure(
        go.Scatter(
            x=x_values,
            y=y_values,
            mode="markers",
            marker=dict(color=MARKER_COLOR, size=6, opacity=0.7),
        )
    )
    _layout(fig, title=title)
    _axis_titles(fig, xlabel=xlabel, ylabel=ylabel)
    return _finish(
        fig, title=title, collection=collection, notes=notes, replace=replace, api_url=api_url, client=client
    )


OHLC_COLUMNS = ("open", "high", "low", "close")


def _ohlc_frame(ohlc: pd.DataFrame) -> pd.DataFrame:
    """Return the candles with lower-case open/high/low/close columns."""
    if not isinstance(ohlc, pd.DataFrame):
        raise TypeError("candlestick() takes a DataFrame with open, high, low and close columns.")
    columns = {str(column).lower(): column for column in ohlc.columns}
    missing = [name for name in OHLC_COLUMNS if name not in columns]
    if missing:
        raise ValueError(f"Candle data is missing column(s): {', '.join(missing)}.")
    if ohlc.empty:
        raise ValueError("There is no data to plot.")
    return pd.DataFrame({name: ohlc[columns[name]].to_numpy(dtype=float) for name in OHLC_COLUMNS}, index=ohlc.index)


def candlestick(
    ohlc: pd.DataFrame,
    *,
    title: str,
    overlays: Optional[Mapping[str, IndicatorSpec]] = None,
    panels: Optional[Mapping[str, IndicatorSpec]] = None,
    collection: Optional[str] = None,
    notes: Optional[str] = None,
    replace: bool = True,
    api_url: Optional[str] = None,
    client: Any = None,
) -> str:
    """Publish candlesticks with indicators drawn over the price or in panels below.

    ``ohlc`` is a DataFrame with open, high, low and close columns (any case) and a
    time index. ``overlays`` maps a name to an indicator drawn on the price pane, such
    as a moving average. ``panels`` maps a name to an indicator drawn in its own pane
    below the price, sharing the time axis, such as RSI.

    Each indicator is a Series aligned with the candles, or an ``Indicator`` to choose
    a line (default) or markers.
    """
    candles = _ohlc_frame(ohlc)
    overlays = dict(overlays or {})
    panels = dict(panels or {})
    panel_count = len(panels)
    heights = [0.6] + [0.4 / panel_count] * panel_count if panel_count else [1.0]
    fig = make_subplots(rows=1 + panel_count, cols=1, shared_xaxes=True, vertical_spacing=0.03, row_heights=heights)

    fig.add_trace(
        go.Candlestick(
            x=candles.index,
            open=candles["open"],
            high=candles["high"],
            low=candles["low"],
            close=candles["close"],
            name="Price",
            increasing=dict(line=dict(color=POSITIVE_COLOR), fillcolor=POSITIVE_COLOR),
            decreasing=dict(line=dict(color=NEGATIVE_COLOR), fillcolor=NEGATIVE_COLOR),
        ),
        row=1,
        col=1,
    )

    colors = cycle(SERIES_PALETTE)
    for name, spec in overlays.items():
        fig.add_trace(_indicator_trace(name, spec, next(colors), candles.index), row=1, col=1)
    for row, (name, spec) in enumerate(panels.items(), start=2):
        fig.add_trace(_indicator_trace(name, spec, next(colors), candles.index), row=row, col=1)
        fig.update_yaxes(title_text=name, row=row, col=1)

    _layout(fig, title=title, height=560 + 160 * panel_count, show_legend=bool(overlays or panels))
    fig.update_yaxes(title_text="Price", row=1, col=1)
    return _finish(
        fig, title=title, collection=collection, notes=notes, replace=replace, api_url=api_url, client=client
    )
