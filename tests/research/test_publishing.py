"""Tests for BacktestResult.publish() and research publishing module."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import httpx
import pandas as pd
import pytest

from q_backend.api.schemas.backtest import BacktestImportRequest
from q_backend.market_data.timezone import BRASILIA_TZ
from q_backend.research import (
    ChartIndicator,
    ResearchStrategy,
    TradeOrder,
    backtest,
)


@pytest.fixture
def ohlcv_bars() -> pd.DataFrame:
    idx = pd.date_range("2026-09-01 09:00", periods=10, freq="5min", tz=BRASILIA_TZ, name="time")
    closes = [100.0, 101.0, 102.0, 101.5, 103.0, 102.0, 104.0, 103.5, 105.0, 106.0]
    opens = [c - 0.5 for c in closes]
    highs = [c + 1.0 for c in closes]
    lows = [c - 1.0 for c in closes]
    volumes = [1000 + i * 100 for i in range(10)]
    df = pd.DataFrame(
        {"open": opens, "high": highs, "low": lows, "close": closes, "tick_volume": volumes},
        index=idx,
    )
    df.attrs["q_research"] = {"timeframe": "M5", "symbol": "WIN$"}
    return df


class MockPublishedStrategy(ResearchStrategy):
    """Simple strategy with indicators and trades."""

    def __init__(self, period: int = 3) -> None:
        self.period = period

    def chart_indicators(self) -> tuple[ChartIndicator, ...]:
        return (
            ChartIndicator("sma", pane="price", label="SMA"),
            ChartIndicator("osc", pane="oscillator", label="Oscillator"),
        )

    def compute_indicators(self, frame: pd.DataFrame) -> pd.DataFrame:
        frame = frame.copy()
        frame["sma"] = frame["close"].rolling(self.period).mean()
        frame["osc"] = frame["close"] - frame["sma"]
        return frame

    def entry_strategy(self, frame: pd.DataFrame) -> TradeOrder | None:
        if len(frame) == 5:
            return TradeOrder.buy()
        return None

    def exit_strategy(self, frame: pd.DataFrame) -> TradeOrder | None:
        if len(frame) == 7:
            return TradeOrder.close()
        return None


def test_publish_missing_publish_method(ohlcv_bars: pd.DataFrame) -> None:
    result = backtest(ohlcv_bars, strategy=MockPublishedStrategy(), symbol="WIN$")
    assert hasattr(result, "publish"), "BacktestResult must have a publish() method"


def test_publish_no_bars_raises_value_error() -> None:
    idx = pd.DatetimeIndex([], name="time", tz=BRASILIA_TZ)
    empty_df = pd.DataFrame(columns=["open", "high", "low", "close", "volume"], index=idx)
    empty_df.attrs["q_research"] = {"timeframe": "M5"}
    result = backtest(empty_df, strategy="MACrossover", symbol="WIN$")
    with pytest.raises(ValueError, match="no bars"):
        result.publish()


def test_publish_missing_timeframe_raises_value_error(ohlcv_bars: pd.DataFrame) -> None:
    frame = ohlcv_bars.copy()
    frame.attrs = {}
    result = backtest(frame, strategy=MockPublishedStrategy(), symbol="WIN$")
    with pytest.raises(ValueError, match="timeframe="):
        result.publish()


def test_publish_posts_valid_request_shape(ohlcv_bars: pd.DataFrame) -> None:
    result = backtest(ohlcv_bars, strategy=MockPublishedStrategy(), symbol="WIN$")

    captured_requests: list[httpx.Request] = []

    def mock_handler(request: httpx.Request) -> httpx.Response:
        captured_requests.append(request)
        return httpx.Response(200, json={"run_id": "test-run-uuid-123"})

    mock_client = httpx.Client(transport=httpx.MockTransport(mock_handler))
    with patch("q_backend.research.publishing.httpx.Client", return_value=mock_client):
        run_id = result.publish()

    assert run_id == "test-run-uuid-123"
    assert len(captured_requests) == 1
    req = captured_requests[0]
    assert req.method == "POST"
    assert str(req.url) == "http://127.0.0.1:8000/api/v1/backtests/import"

    body = json.loads(req.content.decode())
    # 1. Validates against vendored Q-097 model
    validated = BacktestImportRequest.model_validate(body)
    assert validated.config.strategy == "MockPublishedStrategy"
    assert validated.config.symbol == "WIN$"
    assert validated.config.timeframe == "M5"

    # 2. Bars and indicator series of equal length
    assert len(body["result"]["bars"]) == len(ohlcv_bars)
    for indicator in body["result"]["indicators"]:
        assert len(indicator["values"]) == len(ohlcv_bars)

    # 3. Null for warm-up values
    sma_series = next(i for i in body["result"]["indicators"] if i["key"] == "sma")
    # rolling(3) has 2 NaNs at start
    assert sma_series["values"][0] is None
    assert sma_series["values"][1] is None
    assert sma_series["values"][2] is not None

    # 4. Closed trades only
    closed_trades = result.trades[result.trades["status"] == "closed"]
    assert len(body["result"]["trades"]) == len(closed_trades)
    for trade in body["result"]["trades"]:
        assert trade["status"] == "CLOSED"

    # 5. Metrics equal to result.metrics
    assert body["result"]["metrics"] == result.metrics


def test_publish_overrides_name_timeframe_and_api_url(ohlcv_bars: pd.DataFrame) -> None:
    frame = ohlcv_bars.copy()
    frame.attrs = {}  # no timeframe in attrs
    result = backtest(frame, strategy=MockPublishedStrategy(), symbol="WIN$")

    captured_requests: list[httpx.Request] = []

    def mock_handler(request: httpx.Request) -> httpx.Response:
        captured_requests.append(request)
        return httpx.Response(200, json={"run_id": "custom-run-456"})

    mock_client = httpx.Client(transport=httpx.MockTransport(mock_handler))
    with patch("q_backend.research.publishing.httpx.Client", return_value=mock_client):
        run_id = result.publish(
            name="MyOverriddenStrategy",
            timeframe="H1",
            api_url="http://custom-api:9000",
        )

    assert run_id == "custom-run-456"
    assert len(captured_requests) == 1
    req = captured_requests[0]
    assert str(req.url) == "http://custom-api:9000/api/v1/backtests/import"
    body = json.loads(req.content.decode())
    assert body["config"]["strategy"] == "MyOverriddenStrategy"
    assert body["config"]["timeframe"] == "H1"


def test_publish_provenance_with_git_repo(tmp_path: Path, ohlcv_bars: pd.DataFrame, monkeypatch) -> None:
    # Setup temporary git repo
    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()
    subprocess.run(["git", "init"], cwd=repo_dir, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=repo_dir, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo_dir, check=True)

    script_path = repo_dir / "scripts" / "my_script.py"
    script_path.parent.mkdir()
    script_path.write_text("class RepoStrategy:\n    pass\n")

    subprocess.run(["git", "add", "."], cwd=repo_dir, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "initial"], cwd=repo_dir, check=True, capture_output=True)
    head_rev = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo_dir, check=True, capture_output=True, text=True
    ).stdout.strip()

    monkeypatch.setattr(sys, "argv", [str(script_path)])
    monkeypatch.setattr(sys.modules["__main__"], "__file__", str(script_path), raising=False)

    strat = MockPublishedStrategy()
    result = backtest(ohlcv_bars, strategy=strat, symbol="WIN$")

    captured_requests: list[httpx.Request] = []

    def mock_handler(request: httpx.Request) -> httpx.Response:
        captured_requests.append(request)
        return httpx.Response(200, json={"run_id": "run-prov"})

    mock_client = httpx.Client(transport=httpx.MockTransport(mock_handler))
    with patch("q_backend.research.publishing.httpx.Client", return_value=mock_client):
        result.publish()

    body = json.loads(captured_requests[0].content.decode())
    prov = body["provenance"]
    assert prov["script"] == "scripts/my_script.py"
    assert "MockPublishedStrategy" in prov["strategy_class"]
    assert prov["strategy_source"] is not None
    assert "class MockPublishedStrategy" in prov["strategy_source"]
    assert prov["git_revision"] == head_rev
    assert prov["git_dirty"] is False

    # Modify file -> dirty flag
    script_path.write_text("class RepoStrategy:\n    # modified\n    pass\n")
    mock_client_dirty = httpx.Client(transport=httpx.MockTransport(mock_handler))
    with patch("q_backend.research.publishing.httpx.Client", return_value=mock_client_dirty):
        result.publish()

    body_dirty = json.loads(captured_requests[1].content.decode())
    assert body_dirty["provenance"]["git_dirty"] is True


def test_publish_provenance_degrades_to_null_when_unavailable(ohlcv_bars: pd.DataFrame, monkeypatch) -> None:
    monkeypatch.setattr(sys, "argv", [""])
    monkeypatch.delattr(sys.modules["__main__"], "__file__", raising=False)

    result = backtest(ohlcv_bars, strategy="MACrossover", symbol="WIN$")

    captured_requests: list[httpx.Request] = []

    def mock_handler(request: httpx.Request) -> httpx.Response:
        captured_requests.append(request)
        return httpx.Response(200, json={"run_id": "run-prov-null"})

    mock_client = httpx.Client(transport=httpx.MockTransport(mock_handler))
    with patch("q_backend.research.publishing.httpx.Client", return_value=mock_client):
        result.publish()

    body = json.loads(captured_requests[0].content.decode())
    prov = body["provenance"]
    assert prov["script"] is None


def test_publish_connection_error_raises_connection_error(ohlcv_bars: pd.DataFrame) -> None:
    result = backtest(ohlcv_bars, strategy=MockPublishedStrategy(), symbol="WIN$")

    def mock_handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("Connection refused", request=request)

    mock_client = httpx.Client(transport=httpx.MockTransport(mock_handler))
    with patch("q_backend.research.publishing.httpx.Client", return_value=mock_client):
        with pytest.raises(ConnectionError) as exc_info:
            result.publish(api_url="http://127.0.0.1:8000")

    msg = str(exc_info.value)
    assert "127.0.0.1:8000" in msg
    assert "./dev research" in msg


def test_publish_422_raises_value_error(ohlcv_bars: pd.DataFrame) -> None:
    result = backtest(ohlcv_bars, strategy=MockPublishedStrategy(), symbol="WIN$")

    def mock_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(422, json={"detail": "Bars must not be empty"})

    mock_client = httpx.Client(transport=httpx.MockTransport(mock_handler))
    with patch("q_backend.research.publishing.httpx.Client", return_value=mock_client):
        with pytest.raises(ValueError) as exc_info:
            result.publish()

    assert "Bars must not be empty" in str(exc_info.value)


def test_publish_other_error_raises_runtime_error(ohlcv_bars: pd.DataFrame) -> None:
    result = backtest(ohlcv_bars, strategy=MockPublishedStrategy(), symbol="WIN$")

    def mock_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="Internal server error")

    mock_client = httpx.Client(transport=httpx.MockTransport(mock_handler))
    with patch("q_backend.research.publishing.httpx.Client", return_value=mock_client):
        with pytest.raises(RuntimeError) as exc_info:
            result.publish()

    assert "500" in str(exc_info.value)
    assert "Internal server error" in str(exc_info.value)


def test_publish_never_mutates_result_and_creates_two_runs(ohlcv_bars: pd.DataFrame) -> None:
    result = backtest(ohlcv_bars, strategy=MockPublishedStrategy(), symbol="WIN$")
    metrics_before = dict(result.metrics)
    config_before = dict(result.config)

    captured_requests: list[httpx.Request] = []
    run_ids = ["run-1", "run-2"]

    def mock_handler(request: httpx.Request) -> httpx.Response:
        captured_requests.append(request)
        return httpx.Response(200, json={"run_id": run_ids.pop(0)})

    mock_client_1 = httpx.Client(transport=httpx.MockTransport(mock_handler))
    mock_client_2 = httpx.Client(transport=httpx.MockTransport(mock_handler))
    with patch("q_backend.research.publishing.httpx.Client", side_effect=[mock_client_1, mock_client_2]):
        first_id = result.publish()
        second_id = result.publish()

    assert first_id == "run-1"
    assert second_id == "run-2"
    assert len(captured_requests) == 2
    assert result.metrics == metrics_before
    assert dict(result.config) == config_before
