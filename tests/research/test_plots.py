"""Tests for the one-call plot helpers in q_backend.research."""

from __future__ import annotations

import base64
import json
from unittest.mock import patch

import httpx
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
import pytest

from q_backend import research
from q_backend.research import bar, drawdown, histogram, line, scatter


@pytest.fixture
def captured():
    """Replace publish_figure with a recorder that returns a fixed id."""
    with patch("q_backend.research.plots.publish_figure", return_value="fig-1") as publish:
        yield publish


def test_helpers_are_exported_from_research_package() -> None:
    for name in ("line", "drawdown", "bar", "histogram", "scatter"):
        assert getattr(research, name) is not None
        assert name in research.__all__


def test_line_series_plots_one_line_and_publishes(captured) -> None:
    index = pd.date_range("2026-01-01", periods=4, freq="D")
    equity = pd.Series([100.0, 101.0, 99.0, 103.0], index=index, name="equity")

    fig_id = line(equity, title="Equity", collection="study", notes="H1", ylabel="BRL")

    assert fig_id == "fig-1"
    fig = captured.call_args.args[0]
    ax = fig.axes[0]
    assert len(ax.lines) == 1
    np.testing.assert_allclose(ax.lines[0].get_ydata(), [100.0, 101.0, 99.0, 103.0])
    assert ax.get_title() == "Equity"
    assert ax.get_ylabel() == "BRL"
    assert ax.get_legend() is None
    kwargs = captured.call_args.kwargs
    assert kwargs["title"] == "Equity"
    assert kwargs["collection"] == "study"
    assert kwargs["notes"] == "H1"
    assert kwargs["replace"] is True


def test_line_dataframe_draws_one_line_per_column_with_legend(captured) -> None:
    frame = pd.DataFrame({"strategy": [1.0, 2.0], "benchmark": [1.0, 1.5]})

    line(frame, title="Compare")

    ax = captured.call_args.args[0].axes[0]
    assert [entry.get_label() for entry in ax.lines] == ["strategy", "benchmark"]
    assert ax.get_legend() is not None


def test_drawdown_is_distance_from_running_peak(captured) -> None:
    equity = pd.Series([100.0, 120.0, 90.0, 130.0])

    drawdown(equity, title="Drawdown")

    ax = captured.call_args.args[0].axes[0]
    np.testing.assert_allclose(ax.lines[0].get_ydata(), [0.0, 0.0, -0.25, 0.0])


def test_bar_single_series_colours_by_sign(captured) -> None:
    values = pd.Series([2.0, -1.0, 0.5], index=["jan", "feb", "mar"])

    bar(values, title="Monthly P&L")

    ax = captured.call_args.args[0].axes[0]
    colours = [patch_.get_facecolor()[:3] for patch_ in ax.patches]
    assert colours[0] != colours[1]
    assert colours[0] == colours[2]
    assert [tick.get_text() for tick in ax.get_xticklabels()] == ["jan", "feb", "mar"]


def test_bar_dataframe_draws_grouped_bars_with_legend(captured) -> None:
    frame = pd.DataFrame({"a": [1.0, 2.0], "b": [3.0, 4.0]}, index=["x", "y"])

    bar(frame, title="Grouped")

    ax = captured.call_args.args[0].axes[0]
    assert len(ax.patches) == 4
    assert ax.get_legend() is not None


def test_histogram_leaves_out_missing_values(captured) -> None:
    values = pd.Series([1.0, 2.0, np.nan, 2.5, 3.0])

    histogram(values, title="Returns", bins=4)

    ax = captured.call_args.args[0].axes[0]
    counted = sum(patch_.get_height() for patch_ in ax.patches)
    assert counted == 4


def test_scatter_plots_points_and_checks_lengths(captured) -> None:
    scatter([1, 2, 3], [4, 5, 6], title="Feature vs return", xlabel="RSI", ylabel="fwd")

    ax = captured.call_args.args[0].axes[0]
    offsets = ax.collections[0].get_offsets()
    np.testing.assert_allclose(offsets, [[1, 4], [2, 5], [3, 6]])
    assert ax.get_xlabel() == "RSI"

    with pytest.raises(ValueError, match="same length"):
        scatter([1, 2], [1, 2, 3], title="Bad")


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


def test_helpers_leave_no_pyplot_figures_open(captured) -> None:
    before = plt.get_fignums()

    line([1.0, 2.0], title="No pyplot")
    scatter([1.0], [2.0], title="No pyplot")

    assert plt.get_fignums() == before


def test_line_publishes_a_png_to_the_api() -> None:
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
    assert payload["content"]["kind"] == "image"
    assert payload["content"]["media_type"] == "image/png"
    assert base64.b64decode(payload["content"]["data"]).startswith(b"\x89PNG\r\n\x1a\n")
