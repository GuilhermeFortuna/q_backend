"""Market indicator series endpoint tests (Q-112 criteria 4–7)."""

from __future__ import annotations

from datetime import datetime

import numpy as np
import pandas as pd
import pytest
from fastapi.testclient import TestClient

from q_backend.api.main import app
from q_backend.backtesting.moving_averages import compute_ma
from q_backend.backtesting.technical_indicators import compute_atr, compute_macd, compute_rsi
from q_backend.market_data.models import OHLCV


def _synthetic_bars(n: int = 120, seed: int = 7) -> list[OHLCV]:
    rng = np.random.default_rng(seed)
    closes = 100 + np.cumsum(rng.normal(0, 0.5, n))
    bars: list[OHLCV] = []
    start = datetime(2024, 1, 2, 9, 0)
    for idx in range(n):
        close = float(closes[idx])
        high = close + 0.6
        low = close - 0.6
        open_ = close + float(rng.normal(0, 0.1))
        ts = start + pd.Timedelta(minutes=5 * idx)
        bars.append(
            OHLCV(
                time=ts,
                open=open_,
                high=high,
                low=low,
                close=close,
                tick_volume=1000,
                spread=0,
                real_volume=1000,
            )
        )
    return bars


@pytest.fixture()
def client(monkeypatch):
    bars = _synthetic_bars()
    start = bars[0].time
    end = bars[-1].time

    def fake_fetch(_mds, symbol, timeframe, *, count, start, end):
        return [bar for bar in bars if start <= bar.time <= end]

    monkeypatch.setattr("q_backend.market_data.indicator_series.market_service.fetch_ohlcv_rows", fake_fetch)
    yield TestClient(app), bars, start, end


def test_series_matches_backend_functions(client):
    test_client, bars, start, end = client
    frame = pd.DataFrame(
        {
            "open": [b.open for b in bars],
            "high": [b.high for b in bars],
            "low": [b.low for b in bars],
            "close": [b.close for b in bars],
            "volume": [b.tick_volume for b in bars],
        }
    )
    close = frame["close"]
    expected_sma = compute_ma(close, 10, "sma").tolist()
    expected_rsi = compute_rsi(close, 14).tolist()
    expected_macd = compute_macd(close, 12, 26, 9)[0].tolist()
    expected_atr = compute_atr(frame["high"], frame["low"], close, 14).tolist()

    body = {
        "timeframe": "M5",
        "start": start.isoformat(),
        "end": end.isoformat(),
        "indicators": [
            {"id": "sma", "parameters": {"period": 10}},
            {"id": "rsi", "parameters": {"period": 14}},
            {"id": "macd", "parameters": {"fast_period": 12, "slow_period": 26, "signal_period": 9}},
            {"id": "atr", "parameters": {"period": 14}},
        ],
    }
    response = test_client.post("/api/v1/market/indicators/WIN$/series", json=body)
    assert response.status_code == 200, response.text
    payload = response.json()
    by_key: dict[str, list] = {}
    for item in payload["series"]:
        by_key.setdefault(item["key"], []).append(item["values"])
    sma_vals = by_key["line"][0]
    rsi_vals = by_key["line"][1]
    macd_vals = by_key["line"][2]
    atr_vals = by_key["line"][3]

    def _as_float(values):
        return np.array([np.nan if value is None else value for value in values], dtype=float)

    np.testing.assert_allclose(_as_float(sma_vals), expected_sma, equal_nan=True)
    np.testing.assert_allclose(_as_float(rsi_vals), expected_rsi, equal_nan=True, rtol=0, atol=0)
    np.testing.assert_allclose(_as_float(macd_vals), expected_macd, equal_nan=True)
    np.testing.assert_allclose(_as_float(atr_vals), expected_atr, equal_nan=True)
    assert len(payload["timestamps"]) == len(sma_vals)


def test_ewma_vol_prefix_independent(client):
    test_client, bars, start, end = client
    short_body = {
        "timeframe": "M5",
        "start": bars[50].time.isoformat(),
        "end": end.isoformat(),
        "indicators": [{"id": "per_bar_ewma_vol", "parameters": {"half_life": 32.0}}],
    }
    long_body = {
        "timeframe": "M5",
        "start": bars[0].time.isoformat(),
        "end": end.isoformat(),
        "indicators": [{"id": "per_bar_ewma_vol", "parameters": {"half_life": 32.0}}],
    }
    short = test_client.post("/api/v1/market/indicators/WIN$/series", json=short_body).json()
    long = test_client.post("/api/v1/market/indicators/WIN$/series", json=long_body).json()
    short_vals = short["series"][0]["values"]
    long_vals = long["series"][0]["values"][-len(short_vals) :]
    assert short_vals == long_vals


def test_unknown_indicator_422(client):
    test_client, _bars, start, end = client
    body = {
        "timeframe": "M5",
        "start": start.isoformat(),
        "end": end.isoformat(),
        "indicators": [{"id": "not_real", "parameters": {}}],
    }
    response = test_client.post("/api/v1/market/indicators/WIN$/series", json=body)
    assert response.status_code == 422


def test_inverted_macd_fast_slow_422(client):
    test_client, _bars, start, end = client
    body = {
        "timeframe": "M5",
        "start": start.isoformat(),
        "end": end.isoformat(),
        "indicators": [{"id": "macd", "parameters": {"fast_period": 30, "slow_period": 10, "signal_period": 9}}],
    }
    response = test_client.post("/api/v1/market/indicators/WIN$/series", json=body)
    assert response.status_code == 422
    assert "fast_period" in response.json()["detail"]


def test_quote_spread_unavailable_without_ticks(client, tmp_path, monkeypatch):
    test_client, _bars, start, end = client
    monkeypatch.setenv("Q_RESEARCH_TICK_STORE", str(tmp_path))
    body = {
        "timeframe": "M5",
        "start": start.isoformat(),
        "end": end.isoformat(),
        "indicators": [{"id": "quote_spread", "parameters": {}}],
    }
    response = test_client.post("/api/v1/market/indicators/WIN$/series", json=body)
    assert response.status_code == 200, response.text
    series = response.json()["series"][0]
    assert series["unavailableReason"]
    assert all(value is None for value in series["values"])


def test_quote_spread_with_ticks(client, tmp_path, monkeypatch):
    from datetime import date, datetime, timezone

    from q_backend.research.tick_store import _slug_symbol, _write_day_atomic

    test_client, bars, start, end = client
    monkeypatch.setenv("Q_RESEARCH_TICK_STORE", str(tmp_path))

    day = date(start.year, start.month, start.day)
    times = []
    for bar in bars[:5]:
        moment = datetime(
            bar.time.year, bar.time.month, bar.time.day, bar.time.hour, bar.time.minute, tzinfo=timezone.utc
        )
        times.append(int(moment.timestamp() * 1000))
    count = len(times)
    arrays = {
        "time_msc": np.asarray(times, dtype=np.int64),
        "bid": np.full(count, 99.0),
        "ask": np.full(count, 101.0),
        "last": np.full(count, 100.0),
        "volume": np.ones(count, dtype=np.float64),
        "flags": np.full(count, 6, dtype=np.int32),
    }
    directory = tmp_path / _slug_symbol("WIN$")
    directory.mkdir(parents=True, exist_ok=True)
    _write_day_atomic(directory / f"{day.isoformat()}.parquet", arrays)

    body = {
        "timeframe": "M5",
        "start": bars[0].time.isoformat(),
        "end": bars[4].time.isoformat(),
        "indicators": [{"id": "quote_spread", "parameters": {"min_coverage": 0.0}}],
    }
    response = test_client.post("/api/v1/market/indicators/WIN$/series", json=body)
    assert response.status_code == 200, response.text
    series = response.json()["series"][0]
    assert series.get("unavailableReason") is None
    assert series["values"][0] == pytest.approx(200.0)
