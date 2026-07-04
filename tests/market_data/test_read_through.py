"""Tests for research-pipeline OHLCV read-through (WO189)."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from q_backend.market_data import local_store
from q_backend.market_data.models import OHLCV
from q_backend.market_data.read_through import read_ohlcv_fresh
from q_backend.market_data.service import MarketDataService
from q_backend.tasks import worker_context


def _bar(t: datetime, close: float = 100.0) -> OHLCV:
    return OHLCV(
        time=t,
        open=close,
        high=close + 1.0,
        low=close - 1.0,
        close=close,
        tick_volume=100,
    )


def test_read_ohlcv_fresh_uses_injected_service():
    start = datetime(2024, 1, 1, tzinfo=timezone.utc)
    end = datetime(2024, 1, 2, tzinfo=timezone.utc)
    expected = [_bar(start), _bar(end)]
    fake = MagicMock(spec=MarketDataService)
    fake.get_ohlcv.return_value = expected

    result = read_ohlcv_fresh("WIN$", "H1", start, end, service=fake)

    fake.get_ohlcv.assert_called_once_with("WIN$", "H1", start, end)
    assert result == expected
    assert isinstance(result[0], OHLCV)


@pytest.fixture
def market_root(tmp_path, monkeypatch):
    root = tmp_path / "market"
    monkeypatch.setenv("Q_MARKET_DATA_ROOT", str(root))
    return root


def test_read_ohlcv_fresh_connection_error_falls_back_to_local(
    market_root, caplog
):
    start = datetime(2024, 1, 1, 12, tzinfo=timezone.utc)
    end = datetime(2024, 1, 1, 13, tzinfo=timezone.utc)
    local_store.write_ohlcv("WIN$", "H1", [_bar(start), _bar(end)])

    fake = MagicMock(spec=MarketDataService)
    fake.get_ohlcv.side_effect = ConnectionError("gateway down")

    with caplog.at_level("WARNING"):
        result = read_ohlcv_fresh("WIN$", "H1", start, end, service=fake)

    assert len(result) == 2
    assert result[0].time == start.replace(tzinfo=None)
    assert "falling back to local parquet" in caplog.text


def test_read_ohlcv_fresh_prefers_initialized_worker_service(monkeypatch):
    start = datetime(2024, 1, 1, tzinfo=timezone.utc)
    end = datetime(2024, 1, 2, tzinfo=timezone.utc)
    worker_svc = MagicMock(spec=MarketDataService)
    worker_svc.get_ohlcv.return_value = [_bar(start)]
    api_svc = MagicMock(spec=MarketDataService)

    monkeypatch.setattr(worker_context, "_service", worker_svc, raising=False)
    monkeypatch.setattr(
        "q_backend.api.dependencies.market_data_service",
        api_svc,
        raising=False,
    )

    result = read_ohlcv_fresh("EURUSD", "H1", start, end)

    worker_svc.get_ohlcv.assert_called_once()
    api_svc.get_ohlcv.assert_not_called()
    assert len(result) == 1


def test_read_ohlcv_fresh_falls_back_to_dependencies_singleton(monkeypatch):
    start = datetime(2024, 1, 1, tzinfo=timezone.utc)
    end = datetime(2024, 1, 2, tzinfo=timezone.utc)
    api_svc = MagicMock(spec=MarketDataService)
    api_svc.get_ohlcv.return_value = [_bar(start), _bar(end)]

    monkeypatch.setattr(worker_context, "_service", None, raising=False)
    monkeypatch.setattr(
        "q_backend.api.dependencies.market_data_service",
        api_svc,
        raising=False,
    )

    result = read_ohlcv_fresh("EURUSD", "H1", start, end)

    api_svc.get_ohlcv.assert_called_once()
    assert len(result) == 2


def test_peek_worker_market_data_service_never_creates(monkeypatch):
    created = {"count": 0}
    original_init = worker_context.init_worker_market_data

    def counting_init() -> None:
        created["count"] += 1
        original_init()

    monkeypatch.setattr(worker_context, "_service", None, raising=False)
    monkeypatch.setattr(worker_context, "init_worker_market_data", counting_init)

    assert worker_context.peek_worker_market_data_service() is None
    assert created["count"] == 0


def test_no_direct_local_store_reads_outside_market_data():
    src_root = Path(__file__).resolve().parents[2] / "src" / "q_backend"
    offenders: list[str] = []
    for path in sorted(src_root.rglob("*.py")):
        if "market_data" in path.parts:
            continue
        text = path.read_text(encoding="utf-8")
        if (
            "local_store import read_ohlcv" in text
            or "local_store.read_ohlcv" in text
        ):
            offenders.append(str(path.relative_to(src_root)))
    assert offenders == []
