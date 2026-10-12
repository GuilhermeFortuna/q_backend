"""Market indicator series endpoint tests (Q-112 criteria 4–7).

Bars follow real sessions — 09:00 to 17:50 on weekdays, with overnight and weekend gaps —
and ticks use the tick store's MT5 clock, because both are where the endpoint can go wrong.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import numpy as np
import pandas as pd
import pytest
from fastapi.testclient import TestClient
from quote_ticks import write_quote_ticks

from q_backend.api.main import app
from q_backend.backtesting.moving_averages import compute_ma
from q_backend.backtesting.technical_indicators import (
    compute_atr,
    compute_ewma_vol_per_bar,
    compute_macd,
    compute_realized_vol_per_bar,
    compute_rsi,
    compute_vol_normalize,
)
from q_backend.market_data.api_service import ohlcv_to_bar_response
from q_backend.market_data.indicator_series import _values
from q_backend.market_data.models import OHLCV

SYMBOL = "WDO$"
URL = f"/api/v1/market/indicators/{SYMBOL}/series"
SESSION_DAYS = (2, 3, 4, 5, 8, 9)  # January 2024: Tuesday to Friday, then Monday and Tuesday
BARS_PER_DAY = 54


def _session_bars() -> list[OHLCV]:
    rng = np.random.default_rng(7)
    bars: list[OHLCV] = []
    close = 100.0
    for day in SESSION_DAYS:
        for index in range(BARS_PER_DAY):
            open_ = close + float(rng.normal(0, 0.05))
            close = open_ + float(rng.normal(0, 0.4))
            bars.append(
                OHLCV(
                    time=datetime(2024, 1, day, 9, 0) + timedelta(minutes=10 * index),
                    open=open_,
                    high=max(open_, close) + 0.3,
                    low=min(open_, close) - 0.3,
                    close=close,
                    tick_volume=1000,
                    spread=0,
                    real_volume=1000,
                )
            )
    return bars


@pytest.fixture()
def bars(monkeypatch) -> list[OHLCV]:
    rows = _session_bars()

    def fake_fetch(_mds, symbol, timeframe, *, count, start, end):
        return [bar for bar in rows if start <= bar.time <= end]

    monkeypatch.setattr("q_backend.market_data.indicator_series.market_service.fetch_ohlcv_rows", fake_fetch)
    return rows


@pytest.fixture()
def client() -> TestClient:
    return TestClient(app)


def _post(client: TestClient, rows: list[OHLCV], indicators: list[dict], *, timeframe: str = "M10"):
    body = {
        "timeframe": timeframe,
        "start": rows[0].time.isoformat(),
        "end": rows[-1].time.isoformat(),
        "indicators": indicators,
    }
    return client.post(URL, json=body)


def _series(client: TestClient, rows: list[OHLCV], indicator: dict, index: int = 0) -> np.ndarray:
    response = _post(client, rows, [indicator])
    assert response.status_code == 200, response.text
    values = response.json()["series"][index]["values"]
    return np.array([np.nan if value is None else value for value in values], dtype=float)


def _frame(rows: list[OHLCV]) -> pd.DataFrame:
    return pd.DataFrame({name: [getattr(bar, name) for bar in rows] for name in ("open", "high", "low", "close")})


def test_series_matches_backend_functions(client, bars):
    frame = _frame(bars)
    close = frame["close"]
    response = _post(
        client,
        bars,
        [
            {"id": "sma", "parameters": {"period": 10}},
            {"id": "rsi", "parameters": {"period": 14}},
            {"id": "macd", "parameters": {"fast_period": 12, "slow_period": 26, "signal_period": 9}},
            {"id": "atr", "parameters": {"period": 14}},
        ],
    )
    assert response.status_code == 200, response.text
    payload = response.json()
    by_key = [np.array([np.nan if v is None else v for v in item["values"]]) for item in payload["series"]]
    sma, rsi, macd_line, _signal, _histogram, atr = by_key
    np.testing.assert_array_equal(sma, compute_ma(close, 10, "sma").to_numpy())
    np.testing.assert_array_equal(rsi, compute_rsi(close, 14).to_numpy())
    np.testing.assert_array_equal(macd_line, compute_macd(close, 12, 26, 9)[0].to_numpy())
    np.testing.assert_array_equal(atr, compute_atr(frame["high"], frame["low"], close, 14).to_numpy())


def test_timestamps_are_the_chart_bar_timestamps(client, bars):
    response = _post(client, bars, [{"id": "sma", "parameters": {"period": 10}}])
    expected = [ohlcv_to_bar_response(bar)["timestamp"] for bar in bars]
    assert response.json()["timestamps"] == expected
    assert expected[0] == "2024-01-02T12:00:00Z"  # 09:00 in Brasília


@pytest.mark.parametrize(
    "indicator",
    [
        {"id": "per_bar_ewma_vol", "parameters": {"half_life": 32.0}},
        {"id": "per_bar_realized_vol", "parameters": {"window": 60}},
        {"id": "sma", "parameters": {"period": 100}},
        {"id": "hma", "parameters": {"period": 30}},
        {"id": "per_bar_yang_zhang", "parameters": {"window": 20}},
        {"id": "normalized_momentum", "parameters": {"horizon": 5, "vol_estimator": "ewma", "vol_half_life": 16.0}},
        {
            "id": "normalized_ewma_trend_spread",
            "parameters": {
                "fast_half_life": 2.0,
                "slow_half_life": 4.0,
                "vol_estimator": "ewma",
                "vol_half_life": 30.0,
            },
        },
        {"id": "volatility_log_ratio", "parameters": {"short_window": 10, "long_window": 70}},
    ],
    ids=lambda indicator: indicator["id"],
)
def test_values_do_not_depend_on_where_the_range_starts(client, bars, indicator):
    """Warm-up is loaded in bars across overnight and weekend gaps (criterion 5)."""
    full = _series(client, bars, indicator)
    last_day = bars[-BARS_PER_DAY:]
    short = _series(client, last_day, indicator)
    assert np.isfinite(short).all()
    np.testing.assert_array_equal(short, full[-BARS_PER_DAY:])


@pytest.mark.parametrize(
    "indicator",
    [
        {"id": "ema", "parameters": {"period": 20}},
        {"id": "smma", "parameters": {"period": 10}},
        {"id": "rsi", "parameters": {"period": 14}},
        {"id": "atr", "parameters": {"period": 14}},
        {"id": "macd", "parameters": {"fast_period": 12, "slow_period": 26, "signal_period": 9}},
    ],
    ids=lambda indicator: indicator["id"],
)
def test_recursive_indicators_are_warm_at_the_range_start(client, bars, indicator):
    """A recursive average keeps a trace of its seed; after warm-up that trace is at most 1%."""
    full = _series(client, bars, indicator)
    short = _series(client, bars[-BARS_PER_DAY:], indicator)
    assert np.isfinite(short).all()
    spread = np.nanmax(full) - np.nanmin(full)
    assert np.abs(short - full[-BARS_PER_DAY:]).max() <= 0.01 * spread


def test_range_at_the_start_of_history_returns_leading_nulls(client, bars):
    values = _series(client, bars, {"id": "per_bar_ewma_vol", "parameters": {"half_life": 32.0}})
    expected = compute_ewma_vol_per_bar(_frame(bars)["close"], 32.0).to_numpy()
    assert np.isnan(values[:214]).all()
    np.testing.assert_array_equal(values, expected)


@pytest.mark.parametrize(
    ("indicator", "named"),
    [
        ({"id": "not_real", "parameters": {}}, "not_real"),
        ({"id": "macd", "parameters": {"fast_period": 30, "slow_period": 10}}, "fast_period"),
        ({"id": "sma", "parameters": {"period": 10**12}}, "period"),
        ({"id": "sma", "parameters": {"period": 0}}, "period"),
        ({"id": "sma", "parameters": {"period": 20.5}}, "period"),
        ({"id": "sma", "parameters": {"window": 20}}, "window"),
        ({"id": "per_bar_ewma_vol", "parameters": {"half_life": 1e9}}, "half_life"),
        ({"id": "price_zscore_log", "parameters": {"window": 1}}, "window"),
        ({"id": "per_bar_yang_zhang", "parameters": {"window": 1}}, "window"),
        ({"id": "volatility_log_ratio", "parameters": {"short_window": 40, "long_window": 40}}, "short_window"),
        ({"id": "quote_spread", "parameters": {"max_quote_age_ms": 86_400_000}}, "max_quote_age_ms"),
        ({"id": "quote_spread", "parameters": {"output": "median"}}, "output"),
    ],
)
def test_invalid_requests_return_422_naming_the_parameter(client, bars, indicator, named):
    response = _post(client, bars, [indicator])
    assert response.status_code == 422, response.text
    assert named in response.json()["detail"]


def test_unsupported_timeframe_returns_422(client, bars):
    response = _post(client, bars, [{"id": "sma", "parameters": {}}], timeframe="M30")
    assert response.status_code == 422, response.text


def test_non_finite_values_are_null():
    assert _values(np.array([1.5, np.nan, np.inf, -np.inf])) == [1.5, None, None, None]


def _write_session_quotes(root, rows: list[OHLCV], *, days: tuple[int, ...]) -> dict[datetime, float]:
    """Four quotes per bar on ``days``; returns each bar's widest relative spread."""
    rng = np.random.default_rng(11)
    ticks = []
    widest: dict[datetime, float] = {}
    for bar in rows:
        if bar.time.day not in days:
            continue
        widths = rng.uniform(0.01, 0.4, size=4)
        for offset, width in zip((0, 150, 300, 450), widths, strict=True):
            ticks.append((bar.time + timedelta(seconds=offset), 100.0 - width, 100.0 + width))
        widest[bar.time] = float(2.0 * widths.max() / 100.0)
    write_quote_ticks(root, SYMBOL, ticks)
    return widest


def test_quote_spread_reads_each_bar_from_its_own_ticks(client, bars, tmp_path, monkeypatch):
    """Criterion 6: the Q-111 aggregate, on the bars' own clock, for every bar of the day."""
    monkeypatch.setenv("Q_RESEARCH_TICK_STORE", str(tmp_path))
    widest = _write_session_quotes(tmp_path, bars, days=SESSION_DAYS)
    indicator = {"id": "quote_spread", "parameters": {"output": "max", "max_quote_age_ms": 150_000}}
    values = _series(client, bars, indicator)
    np.testing.assert_allclose(values, [widest[bar.time] * 1e4 for bar in bars], rtol=1e-12)

    covered = {"id": "quote_spread", "parameters": {"min_coverage": 1.0, "max_quote_age_ms": 150_000}}
    assert np.isfinite(_series(client, bars, covered)).all()  # including each day's last bar

    last_day = bars[-BARS_PER_DAY:]
    np.testing.assert_array_equal(_series(client, last_day, indicator), values[-BARS_PER_DAY:])


def test_quote_spread_is_null_on_days_without_ticks(client, bars, tmp_path, monkeypatch):
    monkeypatch.setenv("Q_RESEARCH_TICK_STORE", str(tmp_path))
    _write_session_quotes(tmp_path, bars, days=(3,))
    response = _post(client, bars, [{"id": "quote_spread", "parameters": {}}])
    series = response.json()["series"][0]
    assert series.get("unavailableReason") is None
    has_value = [value is not None for value in series["values"]]
    assert has_value == [bar.time.day == 3 for bar in bars]


def test_quote_spread_unavailable_without_ticks(client, bars, tmp_path, monkeypatch):
    monkeypatch.setenv("Q_RESEARCH_TICK_STORE", str(tmp_path))
    response = _post(client, bars, [{"id": "quote_spread", "parameters": {}}])
    assert response.status_code == 200, response.text
    series = response.json()["series"][0]
    assert SYMBOL in series["unavailableReason"]
    assert all(value is None for value in series["values"])


def test_spread_vs_volatility_is_warm_and_independent_of_the_range_start(client, bars, tmp_path, monkeypatch):
    monkeypatch.setenv("Q_RESEARCH_TICK_STORE", str(tmp_path))
    widest = _write_session_quotes(tmp_path, bars, days=SESSION_DAYS)
    indicator = {
        "id": "spread_vs_volatility",
        "parameters": {"spread_output": "max", "max_quote_age_ms": 150_000, "vol_window": 60, "vol_lag": 1},
    }
    full = _series(client, bars, indicator)
    frame = _frame(bars)
    spread = pd.Series([widest[bar.time] for bar in bars])
    expected = compute_vol_normalize(spread, compute_realized_vol_per_bar(frame["close"], 60), 1, 1).to_numpy()
    np.testing.assert_allclose(full, expected, rtol=1e-12, equal_nan=True)

    short = _series(client, bars[-BARS_PER_DAY:], indicator)
    assert np.isfinite(short).all()
    np.testing.assert_array_equal(short, full[-BARS_PER_DAY:])
