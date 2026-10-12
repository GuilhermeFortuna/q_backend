"""Tests for the one-call interactive chart helpers in q_backend.research."""

from __future__ import annotations

import json
from unittest.mock import patch

import httpx
import numpy as np
import pandas as pd
import plotly.graph_objects as go
import pytest

from q_backend import research
from q_backend.research import Indicator, bar, candlestick, drawdown, histogram, line, scatter


@pytest.fixture
def captured():
    """Replace publish_figure with a recorder that returns a fixed id."""
    with patch("q_backend.research.plots.publish_figure", return_value="fig-1") as publish:
        yield publish


def _figure(captured) -> go.Figure:
    return captured.call_args.args[0]


def _candles(periods: int = 6) -> pd.DataFrame:
    index = pd.date_range("2026-01-01 09:00", periods=periods, freq="10min")
    close = pd.Series(np.linspace(100.0, 105.0, periods), index=index)
    return pd.DataFrame(
        {"Open": close - 0.5, "High": close + 1.0, "Low": close - 1.0, "Close": close, "Volume": 10},
        index=index,
    )


def test_helpers_are_exported_from_research_package() -> None:
    for name in ("line", "drawdown", "bar", "histogram", "scatter", "candlestick", "Indicator"):
        assert getattr(research, name) is not None
        assert name in research.__all__


def test_line_series_plots_one_interactive_line_and_publishes(captured) -> None:
    index = pd.date_range("2026-01-01", periods=4, freq="D")
    equity = pd.Series([100.0, 101.0, 99.0, 103.0], index=index, name="equity")

    fig_id = line(equity, title="Equity", collection="study", notes="H1", ylabel="BRL")

    assert fig_id == "fig-1"
    fig = _figure(captured)
    assert isinstance(fig, go.Figure)
    assert len(fig.data) == 1
    assert list(fig.data[0].y) == [100.0, 101.0, 99.0, 103.0]
    assert fig.layout.yaxis.title.text == "BRL"
    assert fig.layout.showlegend is False
    kwargs = captured.call_args.kwargs
    assert kwargs["title"] == "Equity"
    assert kwargs["collection"] == "study"
    assert kwargs["notes"] == "H1"
    assert kwargs["replace"] is True


def test_line_dataframe_draws_one_trace_per_column_with_legend(captured) -> None:
    frame = pd.DataFrame({"strategy": [1.0, 2.0], "benchmark": [1.0, 1.5]})

    line(frame, title="Compare")

    fig = _figure(captured)
    assert [trace.name for trace in fig.data] == ["strategy", "benchmark"]
    assert fig.layout.showlegend is True


def test_drawdown_is_distance_from_running_peak(captured) -> None:
    equity = pd.Series([100.0, 120.0, 90.0, 130.0])

    drawdown(equity, title="Drawdown")

    fig = _figure(captured)
    np.testing.assert_allclose(fig.data[0].y, [0.0, 0.0, -0.25, 0.0])
    assert fig.data[0].fill == "tozeroy"


def test_bar_single_series_colours_gains_and_losses(captured) -> None:
    values = pd.Series([2.0, -1.0, 0.5], index=["jan", "feb", "mar"])

    bar(values, title="Monthly P&L")

    colours = list(_figure(captured).data[0].marker.color)
    assert colours[0] == colours[2] != colours[1]


def test_bar_dataframe_draws_grouped_bars_with_legend(captured) -> None:
    frame = pd.DataFrame({"a": [1.0, 2.0], "b": [3.0, 4.0]}, index=["x", "y"])

    bar(frame, title="Grouped")

    fig = _figure(captured)
    assert [trace.name for trace in fig.data] == ["a", "b"]
    assert fig.layout.barmode == "group"
    assert fig.layout.showlegend is True


def test_histogram_leaves_out_missing_values(captured) -> None:
    values = pd.Series([1.0, 2.0, np.nan, 2.5, 3.0])

    histogram(values, title="Returns", bins=4)

    trace = _figure(captured).data[0]
    assert len(trace.x) == 4
    assert trace.nbinsx == 4


def test_scatter_plots_points_and_checks_lengths(captured) -> None:
    scatter([1, 2, 3], [4, 5, 6], title="Feature vs return", xlabel="RSI", ylabel="fwd")

    fig = _figure(captured)
    assert fig.data[0].mode == "markers"
    assert list(fig.data[0].x) == [1.0, 2.0, 3.0]
    assert fig.layout.xaxis.title.text == "RSI"

    with pytest.raises(ValueError, match="same length"):
        scatter([1, 2], [1, 2, 3], title="Bad")


def test_candlestick_draws_candles_with_overlay_and_panel(captured) -> None:
    candles = _candles()
    ema = candles["Close"].ewm(span=3).mean()
    rsi = pd.Series(np.linspace(30, 70, len(candles)), index=candles.index)

    candlestick(
        candles,
        title="WDO M10",
        overlays={"EMA 3": ema},
        panels={"RSI": Indicator(rsi, kind="scatter")},
        collection="wdo",
    )

    fig = _figure(captured)
    assert [trace.type for trace in fig.data] == ["candlestick", "scatter", "scatter"]
    candle = fig.data[0]
    assert list(candle.open) == pytest.approx(list(candles["Open"]))
    assert list(candle.close) == pytest.approx(list(candles["Close"]))
    # The overlay sits on the price pane; the scatter panel sits in its own row below.
    assert fig.data[1].yaxis == "y"
    assert fig.data[2].yaxis == "y2"
    assert fig.data[2].mode == "markers"
    assert fig.layout.yaxis2.title.text == "RSI"
    assert captured.call_args.kwargs["collection"] == "wdo"


def test_candlestick_without_indicators_has_one_pane(captured) -> None:
    candlestick(_candles(), title="Price only")

    fig = _figure(captured)
    assert [trace.type for trace in fig.data] == ["candlestick"]
    assert "yaxis2" not in fig.to_plotly_json()["layout"]


def test_candlestick_accepts_lower_case_columns(captured) -> None:
    candles = _candles().rename(columns=str.lower)

    candlestick(candles, title="Lower case")

    assert _figure(captured).data[0].close is not None


def test_candlestick_rejects_missing_columns_and_empty_data(captured) -> None:
    with pytest.raises(ValueError, match="missing column"):
        candlestick(_candles().drop(columns=["High"]), title="Bad")
    with pytest.raises(ValueError, match="no data"):
        candlestick(_candles().iloc[:0], title="Empty")
    captured.assert_not_called()


def test_indicator_must_match_candle_length(captured) -> None:
    with pytest.raises(ValueError, match="same length"):
        candlestick(_candles(), title="Bad", overlays={"EMA": np.arange(3.0)})
    captured.assert_not_called()


def test_numpy_indicator_uses_candle_index_as_x(captured) -> None:
    candles = _candles()

    candlestick(candles, title="Numpy", overlays={"Signal": Indicator(np.arange(6.0), kind="scatter")})

    assert list(_figure(captured).data[1].x) == list(candles.index)


@pytest.mark.parametrize(
    "call",
    [
        lambda: line([], title="Empty"),
        lambda: histogram([np.nan], title="Empty"),
        lambda: scatter([], [], title="Empty"),
    ],
)
def test_empty_data_is_rejected(call, captured) -> None:
    with pytest.raises(ValueError, match="no data"):
        call()
    captured.assert_not_called()


def test_single_series_helpers_reject_multi_column_frames(captured) -> None:
    frame = pd.DataFrame({"a": [1.0], "b": [2.0]})

    with pytest.raises(ValueError, match="one series"):
        drawdown(frame, title="Drawdown")
    with pytest.raises(ValueError, match="one series"):
        histogram(frame, title="Histogram")
    captured.assert_not_called()


def test_line_publishes_a_plotly_figure_to_the_api() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(201, json={"figure_id": "fig-9", "replaced": False})

    client = httpx.Client(transport=httpx.MockTransport(handler))

    fig_id = line(pd.Series([1.0, 2.0, 3.0], name="equity"), title="Equity", collection="study", client=client)

    assert fig_id == "fig-9"
    payload = json.loads(requests[0].content)
    assert payload["title"] == "Equity"
    assert payload["collection"] == "study"
    assert payload["content"]["kind"] == "plotly"
    assert payload["content"]["figure"]["data"][0]["type"] == "scatter"
