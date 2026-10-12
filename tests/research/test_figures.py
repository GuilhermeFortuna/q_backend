"""Tests for research.publish_figure() and research.use_figure_style()."""

from __future__ import annotations

import base64
import json
import struct
import sys
from unittest.mock import MagicMock, patch

import httpx
import matplotlib.pyplot as plt
import plotly.graph_objects as go
import pytest

from q_backend.research import publish_figure, use_figure_style
from q_backend.research.figure_style import BG_CARBON_950, PLOT_CARBON_900


def test_matplotlib_figure_publishes_png_and_svg_and_remains_open() -> None:
    # 1. Matplotlib PNG
    fig, ax = plt.subplots()
    ax.plot([1, 2, 3], [4, 5, 6])
    ax.set_title("Test Plot")

    size_before = tuple(fig.get_size_inches())
    dpi_before = fig.dpi
    fignum = fig.number

    captured_requests: list[httpx.Request] = []

    def mock_handler(request: httpx.Request) -> httpx.Response:
        captured_requests.append(request)
        return httpx.Response(201, json={"figure_id": "fig-123", "replaced": False})

    client = httpx.Client(transport=httpx.MockTransport(mock_handler))

    fig_id = publish_figure(fig, image_format="png", client=client)
    assert fig_id == "fig-123"
    assert len(captured_requests) == 1

    payload = json.loads(captured_requests[0].content)
    assert payload["title"] == "Test Plot"
    assert payload["content"]["kind"] == "image"
    assert payload["content"]["media_type"] == "image/png"
    w = payload["content"]["width"]
    h = payload["content"]["height"]
    assert w > 0 and h > 0

    # Verify pixel size matches decoded PNG IHDR chunk
    raw_bytes = base64.b64decode(payload["content"]["data"])
    unpacked_w, unpacked_h = struct.unpack(">II", raw_bytes[16:24])
    assert w == unpacked_w
    assert h == unpacked_h

    # Verify figure is unchanged and still open
    assert tuple(fig.get_size_inches()) == size_before
    assert fig.dpi == dpi_before
    assert plt.fignum_exists(fignum)

    # 2. Matplotlib SVG
    captured_requests.clear()
    fig_id_svg = publish_figure(fig, image_format="svg", client=client)
    assert fig_id_svg == "fig-123"
    payload_svg = json.loads(captured_requests[0].content)
    assert payload_svg["content"]["kind"] == "image"
    assert payload_svg["content"]["media_type"] == "image/svg+xml"
    assert payload_svg["content"]["width"] > 0
    assert payload_svg["content"]["height"] > 0

    # Figure still open
    assert plt.fignum_exists(fignum)
    plt.close(fig)


def test_axes_publishes_parent_figure_and_plotly_to_json_unchanged() -> None:
    # 1. Axes publishes parent figure
    fig, ax = plt.subplots()
    ax.plot([0, 1], [0, 1])
    ax.set_title("Axes Title")

    captured_requests: list[httpx.Request] = []

    def mock_handler(request: httpx.Request) -> httpx.Response:
        captured_requests.append(request)
        return httpx.Response(201, json={"figure_id": "fig-ax", "replaced": False})

    client = httpx.Client(transport=httpx.MockTransport(mock_handler))

    fig_id = publish_figure(ax, client=client)
    assert fig_id == "fig-ax"
    payload = json.loads(captured_requests[0].content)
    assert payload["title"] == "Axes Title"
    assert payload["content"]["kind"] == "image"
    plt.close(fig)

    # 2. Plotly figure publishes to_json() content unchanged
    p_fig = go.Figure(
        data=go.Scatter(x=[1, 2, 3], y=[10, 20, 30], mode="lines+markers"),
        layout=go.Layout(title=dict(text="Plotly Title")),
    )
    expected_dict = json.loads(p_fig.to_json())

    captured_requests.clear()
    fig_id_p = publish_figure(p_fig, client=client)
    assert fig_id_p == "fig-ax"
    p_payload = json.loads(captured_requests[0].content)
    assert p_payload["title"] == "Plotly Title"
    assert p_payload["content"]["kind"] == "plotly"
    assert p_payload["content"]["figure"] == expected_dict


def test_title_collection_and_provenance_defaults_and_errors() -> None:
    # Matplotlib suptitle default
    fig = plt.figure()
    fig.suptitle("Suptitle Default")
    ax = fig.add_subplot(111)
    ax.set_title("Single Ax Title")  # suptitle should win

    captured: list[httpx.Request] = []

    def mock_handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(201, json={"figure_id": "fig-title", "replaced": False})

    client = httpx.Client(transport=httpx.MockTransport(mock_handler))
    publish_figure(fig, client=client)
    payload = json.loads(captured[0].content)
    assert payload["title"] == "Suptitle Default"
    plt.close(fig)

    # Missing title raises ValueError
    fig_no_title, _ = plt.subplots()
    with pytest.raises(ValueError, match="Figure title is required"):
        publish_figure(fig_no_title, client=client)
    plt.close(fig_no_title)

    # Blank title string raises ValueError
    fig_blank, _ = plt.subplots()
    with pytest.raises(ValueError, match="Figure title is required"):
        publish_figure(fig_blank, title="   ", client=client)
    plt.close(fig_blank)

    # Unsupported object raises TypeError naming accepted types
    with pytest.raises(TypeError, match="Unsupported figure type"):
        publish_figure({"not": "a figure"}, client=client)

    # Invalid image_format raises ValueError
    fig_fmt, _ = plt.subplots()
    with pytest.raises(ValueError, match="image_format must be 'png' or 'svg'"):
        publish_figure(fig_fmt, title="Valid Title", image_format="jpg", client=client)
    plt.close(fig_fmt)

    # Defaults for collection and provenance
    p_fig = go.Figure(layout=go.Layout(title=dict(text="Provenance Test")))
    captured.clear()
    publish_figure(p_fig, client=client)
    payload2 = json.loads(captured[0].content)
    assert "collection" in payload2
    assert payload2["collection"] != ""
    assert "provenance" in payload2
    assert isinstance(payload2["provenance"], dict)

    # Explicit collection, notes, replace
    captured.clear()
    publish_figure(
        p_fig,
        title="Custom Title",
        collection="custom_col",
        notes="Important chart",
        replace=False,
        client=client,
    )
    payload3 = json.loads(captured[0].content)
    assert payload3["title"] == "Custom Title"
    assert payload3["collection"] == "custom_col"
    assert payload3["notes"] == "Important chart"
    assert payload3["replace"] is False


def test_transport_and_status_error_mapping() -> None:
    fig, _ = plt.subplots()

    # 1. Transport error (unreachable API)
    def connect_error_handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("Connection refused")

    client_conn_err = httpx.Client(transport=httpx.MockTransport(connect_error_handler))
    with pytest.raises(
        ConnectionError, match=r"Cannot connect to Research API at .*Start the stack with '\./dev research'\."
    ):
        publish_figure(fig, title="Title", client=client_conn_err)

    # 2. HTTP 422 Unprocessable Entity
    def err_422_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(422, json={"detail": "title must not be blank"})

    client_422 = httpx.Client(transport=httpx.MockTransport(err_422_handler))
    with pytest.raises(ValueError, match="Server rejected figure publish \\(422\\): title must not be blank"):
        publish_figure(fig, title="Title", client=client_422)

    # 3. HTTP 413 Content Too Large
    def err_413_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(413, json={"detail": "Figure content 35000000 bytes exceeds 33554432 bytes limit"})

    client_413 = httpx.Client(transport=httpx.MockTransport(err_413_handler))
    with pytest.raises(ValueError, match="Figure too large for Research API \\(413\\)"):
        publish_figure(fig, title="Title", client=client_413)

    # 4. HTTP 500 Internal Server Error
    def err_500_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="Internal Server Error")

    client_500 = httpx.Client(transport=httpx.MockTransport(err_500_handler))
    with pytest.raises(RuntimeError, match="Figure publish failed with status 500"):
        publish_figure(fig, title="Title", client=client_500)

    # 5. Bad JSON response
    def err_bad_json_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(201, json={"status": "ok"})  # missing figure_id

    client_bad_json = httpx.Client(transport=httpx.MockTransport(err_bad_json_handler))
    with pytest.raises(RuntimeError, match="Unexpected response format"):
        publish_figure(fig, title="Title", client=client_bad_json)

    plt.close(fig)


def test_use_figure_style_configures_both_and_tolerates_missing_lib() -> None:
    import plotly.io as pio

    # Test dark style application
    use_figure_style("dark")
    assert plt.rcParams["figure.facecolor"] == BG_CARBON_950
    assert plt.rcParams["axes.facecolor"] == PLOT_CARBON_900
    assert "q_dark" in pio.templates
    assert pio.templates.default == "q_dark"

    # Unsupported theme raises ValueError
    with pytest.raises(ValueError, match="Unsupported theme"):
        use_figure_style("light")

    # Works when matplotlib is unavailable
    with patch.dict(sys.modules, {"matplotlib": None, "matplotlib.pyplot": None}):
        use_figure_style()

    # Works when plotly is unavailable
    with patch.dict(sys.modules, {"plotly": None, "plotly.graph_objects": None, "plotly.io": None}):
        use_figure_style()
