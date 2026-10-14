"""Tests for research.publish_study(): index, timeframe, series selection and transport."""

from __future__ import annotations

import json

import httpx
import numpy as np
import pandas as pd
import pytest

from q_backend.research import ChartIndicator, publish_study


def _m10_index_with_overnight_gap() -> pd.DatetimeIndex:
    day_one = pd.date_range("2026-01-05 09:00", "2026-01-05 17:50", freq="10min")
    day_two = pd.date_range("2026-01-06 09:00", "2026-01-06 17:50", freq="10min")
    return day_one.append(day_two)


def _client(status: int = 201, body: dict | None = None) -> tuple[httpx.Client, list[httpx.Request]]:
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(status, json=body if body is not None else {"studyId": "study-1", "replaced": False})

    return httpx.Client(transport=httpx.MockTransport(handler)), captured


def _payload(captured: list[httpx.Request]) -> dict:
    return json.loads(captured[0].content)


def test_aware_index_publishes_utc_and_naive_index_reads_brasilia_time() -> None:
    index = pd.DatetimeIndex(["2026-01-05 10:00", "2026-01-05 10:10"]).tz_localize("America/Sao_Paulo")
    client, captured = _client()
    publish_study(pd.Series([1.0, 2.0], index=index, name="band"), symbol="wdo$n", name="Band", client=client)
    payload = _payload(captured)
    assert payload["symbol"] == "WDO$N"
    assert payload["timestamps"] == ["2026-01-05T13:00:00+00:00", "2026-01-05T13:10:00+00:00"]

    naive = pd.DatetimeIndex(["2026-01-05 10:00", "2026-01-05 10:10"])
    client, captured = _client()
    publish_study(pd.Series([1.0, 2.0], index=naive, name="band"), symbol="WDO$N", name="Band", client=client)
    assert _payload(captured)["timestamps"] == ["2026-01-05T13:00:00+00:00", "2026-01-05T13:10:00+00:00"]


def test_nan_and_infinity_become_null_and_booleans_become_numbers() -> None:
    index = pd.date_range("2026-01-05 10:00", periods=4, freq="10min")
    frame = pd.DataFrame(
        {
            "upper": [1.5, np.nan, np.inf, 4.0],
            "regime": [True, False, True, False],
        },
        index=index,
    )
    client, captured = _client()
    publish_study(frame, symbol="WDO$N", name="Mixed", client=client)
    series = {item["key"]: item for item in _payload(captured)["series"]}
    assert series["upper"]["values"] == [1.5, None, None, 4.0]
    assert series["regime"]["values"] == [1.0, 0.0, 1.0, 0.0]
    assert series["upper"]["pane"] == "price"
    assert series["upper"]["label"] == "upper"


def test_unnamed_series_is_published_under_value_key() -> None:
    index = pd.date_range("2026-01-05 10:00", periods=2, freq="10min")
    client, captured = _client()
    publish_study(pd.Series([1.0, 2.0], index=index), symbol="WDO$N", name="Anon", client=client)
    assert [item["key"] for item in _payload(captured)["series"]] == ["value"]


def test_non_numeric_column_is_rejected() -> None:
    index = pd.date_range("2026-01-05 10:00", periods=2, freq="10min")
    frame = pd.DataFrame({"label": ["up", "down"]}, index=index)
    client, _ = _client()
    with pytest.raises(ValueError, match="must be numeric or boolean"):
        publish_study(frame, symbol="WDO$N", name="Text", client=client)


def test_timeframe_is_inferred_from_bars_with_overnight_gaps() -> None:
    index = _m10_index_with_overnight_gap()
    client, captured = _client()
    publish_study(pd.Series(range(len(index)), index=index, dtype=float), symbol="WDO$N", name="M10", client=client)
    assert _payload(captured)["timeframe"] == "M10"


def test_irregular_index_and_seconds_raise_documented_errors() -> None:
    client, _ = _client()
    irregular = pd.DatetimeIndex(["2026-01-05 10:00", "2026-01-05 10:07", "2026-01-05 10:17"])
    with pytest.raises(ValueError, match="not a chart timeframe; pass timeframe="):
        publish_study(pd.Series([1.0, 2.0, 3.0], index=irregular), symbol="WDO$N", name="Irregular", client=client)

    with_seconds = pd.DatetimeIndex(["2026-01-05 10:00:30", "2026-01-05 10:10:30"])
    with pytest.raises(ValueError, match="whole minutes"):
        publish_study(pd.Series([1.0, 2.0], index=with_seconds), symbol="WDO$N", name="Seconds", client=client)


def test_explicit_timeframe_must_divide_every_gap() -> None:
    index = pd.DatetimeIndex(["2026-01-05 10:00", "2026-01-05 10:10", "2026-01-05 10:30"])
    client, _ = _client()
    with pytest.raises(ValueError, match=r"timeframe=") as excinfo:
        publish_study(pd.Series([1.0, 2.0, 3.0], index=index), symbol="WDO$N", name="H1", timeframe="H1", client=client)
    assert "0 days 00:10:00" in str(excinfo.value)


def test_series_publishes_only_named_columns_with_styling() -> None:
    index = pd.date_range("2026-01-05 10:00", periods=3, freq="10min")
    frame = pd.DataFrame(
        {"kc_upper": [3.0, 3.1, 3.2], "kc_lower": [1.0, 1.1, 1.2], "kc_width": [2.0, 2.0, 2.0], "extra": [0, 0, 0]},
        index=index,
    )
    client, captured = _client()
    publish_study(
        frame,
        symbol="WDO$N",
        name="Keltner",
        series=[
            ChartIndicator("kc_upper", color="#6eb5ff"),
            ChartIndicator("kc_lower", color="#6eb5ff", line_style="dashed", line_width=2.0),
            ChartIndicator("kc_width", pane="oscillator", label="Width"),
        ],
        client=client,
    )
    items = _payload(captured)["series"]
    assert [item["key"] for item in items] == ["kc_upper", "kc_lower", "kc_width"]
    assert items[0]["color"] == "#6eb5ff" and "lineStyle" not in items[0]
    assert items[1]["lineStyle"] == "dashed" and items[1]["lineWidth"] == 2.0
    assert items[2]["pane"] == "oscillator" and items[2]["label"] == "Width"


@pytest.mark.parametrize(
    "selection",
    [
        [ChartIndicator("missing")],
        [ChartIndicator("kc_upper"), ChartIndicator("kc_upper")],
        [],
    ],
)
def test_missing_duplicate_or_empty_series_selection_raises(selection: list[ChartIndicator]) -> None:
    index = pd.date_range("2026-01-05 10:00", periods=2, freq="10min")
    frame = pd.DataFrame({"kc_upper": [1.0, 2.0]}, index=index)
    client, captured = _client()
    with pytest.raises(ValueError):
        publish_study(frame, symbol="WDO$N", name="Bad", series=selection, client=client)
    assert captured == []


def test_replacement_is_sent_with_replace_flag_and_returns_id() -> None:
    index = pd.date_range("2026-01-05 10:00", periods=2, freq="10min")
    client, captured = _client()
    study_id = publish_study(pd.Series([1.0, 2.0], index=index, name="v"), symbol="WDO$N", name="Band", client=client)
    assert study_id == "study-1"
    assert _payload(captured)["replace"] is True
    assert captured[0].url.path == "/api/v1/research/studies"


def test_replace_false_conflict_raises_value_error() -> None:
    index = pd.date_range("2026-01-05 10:00", periods=2, freq="10min")
    client, captured = _client(status=409, body={"detail": "study already exists"})
    with pytest.raises(ValueError, match=r"409"):
        publish_study(
            pd.Series([1.0, 2.0], index=index, name="v"), symbol="WDO$N", name="Band", replace=False, client=client
        )
    assert _payload(captured)["replace"] is False


def test_transport_and_status_error_mapping() -> None:
    index = pd.date_range("2026-01-05 10:00", periods=2, freq="10min")
    data = pd.Series([1.0, 2.0], index=index, name="v")

    def connect_error(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("Connection refused")

    with pytest.raises(ConnectionError, match=r"Start the stack with '\./dev research'\."):
        publish_study(
            data, symbol="WDO$N", name="Band", client=httpx.Client(transport=httpx.MockTransport(connect_error))
        )

    client, _ = _client(status=422, body={"detail": "name must not be blank"})
    with pytest.raises(ValueError, match="Server rejected study publish \\(422\\): name must not be blank"):
        publish_study(data, symbol="WDO$N", name="Band", client=client)

    client, _ = _client(status=413, body={"detail": "too many values"})
    with pytest.raises(ValueError, match="Study too large for Research API \\(413\\)"):
        publish_study(data, symbol="WDO$N", name="Band", client=client)

    client, _ = _client(status=500, body={})
    with pytest.raises(RuntimeError, match="Study publish failed with status 500"):
        publish_study(data, symbol="WDO$N", name="Band", client=client)

    client, _ = _client(body={"status": "ok"})
    with pytest.raises(RuntimeError, match="Unexpected response format"):
        publish_study(data, symbol="WDO$N", name="Band", client=client)
