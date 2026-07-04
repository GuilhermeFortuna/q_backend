"""Coverage planning and coverage-aware auto+remote OHLCV reads (WO188)."""

from __future__ import annotations

from datetime import datetime
from unittest.mock import patch

import pytest

from q_backend.market_data import local_store
from q_backend.market_data.coverage import CoveragePlan, plan_ohlcv_read
from q_backend.market_data.models import OHLCV
from q_backend.market_data.service import MarketDataService
from q_backend.storage.runtime_config import set_data_source


@pytest.fixture
def market_root(tmp_path, monkeypatch):
    root = tmp_path / "market"
    monkeypatch.setenv("Q_MARKET_DATA_ROOT", str(root))
    return root


@pytest.fixture(autouse=True)
def isolated_config(tmp_path, monkeypatch):
    monkeypatch.setenv("Q_RUNTIME_CONFIG_PATH", str(tmp_path / "runtime_config.json"))
    monkeypatch.delenv("Q_MT5_GATEWAY_URL", raising=False)
    monkeypatch.delenv("Q_MT5_GATEWAY_TOKEN", raising=False)
    set_data_source("auto")
    yield


def _bar(day: int, hour: int = 10) -> OHLCV:
    return OHLCV(
        time=datetime(2024, 1, day, hour),
        open=1.0,
        high=1.0,
        low=1.0,
        close=1.0,
        tick_volume=1,
    )


def _seed_local(symbol: str, timeframe: str, days: range) -> None:
    local_store.write_ohlcv(symbol, timeframe, [_bar(day) for day in days])


class _CountingRemote:
    def __init__(self, *, bars_by_range: dict[tuple[datetime, datetime], list[OHLCV]] | None = None):
        self.calls: list[tuple[str, str, datetime, datetime]] = []
        self._bars_by_range = bars_by_range or {}

    def is_supported(self) -> bool:
        return True

    def is_available(self) -> bool:
        return True

    def get_ohlcv(self, symbol, timeframe, start, end):
        self.calls.append((symbol, timeframe, start, end))
        if self._bars_by_range:
            return list(self._bars_by_range.get((start, end), []))
        # Default: one bar at segment start for tail/head fetches.
        return [_bar(start.day, start.hour)]


def _auto_remote_service(remote: _CountingRemote) -> MarketDataService:
    service = MarketDataService()
    service._remote_client = remote
    return service


def test_plan_fully_covered(market_root):
    _seed_local("PETR4", "H1", range(10, 16))
    local = local_store.available_range("PETR4", "H1")
    assert local is not None

    plan = plan_ohlcv_read(
        "PETR4",
        "H1",
        datetime(2024, 1, 11),
        datetime(2024, 1, 14),
    )
    assert plan == CoveragePlan(serve_from="local", missing=[])


def test_plan_tail_gap_only(market_root):
    _seed_local("PETR4", "H1", range(10, 16))
    local = local_store.available_range("PETR4", "H1")
    assert local is not None

    plan = plan_ohlcv_read(
        "PETR4",
        "H1",
        datetime(2024, 1, 11),
        datetime(2024, 1, 20),
    )
    assert plan.serve_from == "provider"
    assert plan.missing == [(local.end, datetime(2024, 1, 20))]


def test_plan_head_gap_only(market_root):
    _seed_local("PETR4", "H1", range(10, 16))
    local = local_store.available_range("PETR4", "H1")
    assert local is not None

    plan = plan_ohlcv_read(
        "PETR4",
        "H1",
        datetime(2024, 1, 5),
        datetime(2024, 1, 12),
    )
    assert plan.serve_from == "provider"
    assert plan.missing == [(datetime(2024, 1, 5), local.start)]


def test_plan_head_and_tail(market_root):
    _seed_local("PETR4", "H1", range(10, 16))
    local = local_store.available_range("PETR4", "H1")
    assert local is not None

    plan = plan_ohlcv_read(
        "PETR4",
        "H1",
        datetime(2024, 1, 5),
        datetime(2024, 1, 20),
    )
    assert plan.serve_from == "provider"
    assert plan.missing == [
        (datetime(2024, 1, 5), local.start),
        (local.end, datetime(2024, 1, 20)),
    ]


def test_plan_empty_local_store(market_root):
    plan = plan_ohlcv_read(
        "PETR4",
        "H1",
        datetime(2024, 1, 1),
        datetime(2024, 1, 10),
    )
    assert plan == CoveragePlan(
        serve_from="provider",
        missing=[(datetime(2024, 1, 1), datetime(2024, 1, 10))],
    )


def test_fully_covered_served_from_local_zero_remote_calls(market_root, monkeypatch):
    _seed_local("PETR4", "H1", range(10, 16))
    remote = _CountingRemote()
    service = _auto_remote_service(remote)
    monkeypatch.setattr(
        "q_backend.market_data.routing.symbol_selectable_in_mt5",
        lambda svc, symbol: False,
    )

    bars = service.get_ohlcv(
        "PETR4",
        "H1",
        datetime(2024, 1, 11, 10),
        datetime(2024, 1, 14, 10),
    )

    assert len(bars) == 4
    assert remote.calls == []


def test_tail_gap_single_remote_call_and_persist(market_root, monkeypatch):
    _seed_local("PETR4", "H1", range(10, 16))
    local = local_store.available_range("PETR4", "H1")
    assert local is not None
    tail_start = local.end
    tail_end = datetime(2024, 1, 20, 10)
    tail_bars = [_bar(17), _bar(18)]

    remote = _CountingRemote(
        bars_by_range={(tail_start, tail_end): tail_bars},
    )
    service = _auto_remote_service(remote)
    monkeypatch.setattr(
        "q_backend.market_data.routing.symbol_selectable_in_mt5",
        lambda svc, symbol: False,
    )

    bars = service.get_ohlcv(
        "PETR4",
        "H1",
        datetime(2024, 1, 11, 10),
        tail_end,
    )

    assert len(remote.calls) == 1
    assert remote.calls[0][2:] == (tail_start, tail_end)
    assert len(bars) == 7
    stored = local_store.read_ohlcv(
        "PETR4", "H1", datetime(2024, 1, 11, 10), tail_end
    )
    assert len(stored) == 7


def test_head_gap_single_remote_call(market_root, monkeypatch):
    _seed_local("PETR4", "H1", range(10, 16))
    local = local_store.available_range("PETR4", "H1")
    assert local is not None
    head_start = datetime(2024, 1, 5)
    head_end = local.start
    head_bars = [_bar(6), _bar(7)]

    remote = _CountingRemote(
        bars_by_range={(head_start, head_end): head_bars},
    )
    service = _auto_remote_service(remote)
    monkeypatch.setattr(
        "q_backend.market_data.routing.symbol_selectable_in_mt5",
        lambda svc, symbol: False,
    )

    bars = service.get_ohlcv(
        "PETR4",
        "H1",
        head_start,
        datetime(2024, 1, 12, 10),
    )

    assert len(remote.calls) == 1
    assert remote.calls[0][2:] == (head_start, head_end)
    assert len(bars) == 5


def test_empty_local_full_range_remote_fetch(market_root, monkeypatch):
    full_bars = [_bar(1), _bar(2), _bar(3)]
    remote = _CountingRemote()
    remote.get_ohlcv = lambda symbol, timeframe, start, end: (
        remote.calls.append((symbol, timeframe, start, end)) or list(full_bars)
    )
    service = _auto_remote_service(remote)
    monkeypatch.setattr(
        "q_backend.market_data.routing.symbol_selectable_in_mt5",
        lambda svc, symbol: False,
    )

    start = datetime(2024, 1, 1)
    end = datetime(2024, 1, 10)
    bars = service.get_ohlcv("PETR4", "H1", start, end)

    assert len(remote.calls) == 1
    assert remote.calls[0][2:] == (start, end)
    assert bars == full_bars


def test_connection_error_mid_tail_skipped_when_head_fills_envelope(
    market_root, monkeypatch
):
    _seed_local("PETR4", "H1", range(10, 15))
    local = local_store.available_range("PETR4", "H1")
    assert local is not None
    head_start = datetime(2024, 1, 5, 10)
    head_end = local.start
    tail_end = datetime(2024, 1, 20, 10)
    generous_head = [_bar(day) for day in range(5, 21)]

    remote = _CountingRemote()

    def _head_only(symbol, timeframe, start, end):
        remote.calls.append((symbol, timeframe, start, end))
        assert (start, end) == (head_start, head_end)
        return generous_head

    remote.get_ohlcv = _head_only
    service = _auto_remote_service(remote)
    monkeypatch.setattr(
        "q_backend.market_data.routing.symbol_selectable_in_mt5",
        lambda svc, symbol: False,
    )

    bars = service.get_ohlcv("PETR4", "H1", head_start, tail_end)

    assert len(remote.calls) == 1
    assert len(bars) == 16


def test_connection_error_serves_local_when_envelope_covers(market_root, monkeypatch):
    _seed_local("PETR4", "H1", range(10, 16))
    remote = _CountingRemote()
    fetch_started = {"value": False}

    def _fail(symbol, timeframe, start, end):
        remote.calls.append((symbol, timeframe, start, end))
        fetch_started["value"] = True
        raise ConnectionError("gateway down")

    remote.get_ohlcv = _fail
    service = _auto_remote_service(remote)
    monkeypatch.setattr(
        "q_backend.market_data.routing.symbol_selectable_in_mt5",
        lambda svc, symbol: False,
    )
    monkeypatch.setattr(
        "q_backend.market_data.service.plan_ohlcv_read",
        lambda symbol, timeframe, start, end: CoveragePlan(
            serve_from="provider",
            missing=[(datetime(2024, 1, 20, 10), datetime(2024, 1, 25, 10))],
        ),
    )

    def _covers_after_fetch_attempt(symbol, timeframe, start, end):
        return fetch_started["value"]

    monkeypatch.setattr(
        "q_backend.market_data.service.envelope_covers",
        _covers_after_fetch_attempt,
    )

    bars = service.get_ohlcv(
        "PETR4",
        "H1",
        datetime(2024, 1, 11, 10),
        datetime(2024, 1, 14, 10),
    )

    assert len(bars) == 4
    assert len(remote.calls) == 1


def test_connection_error_mid_fetch_serves_local_when_already_covered(
    market_root, monkeypatch
):
    _seed_local("PETR4", "H1", range(10, 16))
    remote = _CountingRemote()
    call_count = 0

    def _fail_tail(symbol, timeframe, start, end):
        nonlocal call_count
        call_count += 1
        remote.calls.append((symbol, timeframe, start, end))
        if call_count > 1:
            raise ConnectionError("gateway down")
        return [_bar(17)]

    remote.get_ohlcv = _fail_tail
    service = _auto_remote_service(remote)
    monkeypatch.setattr(
        "q_backend.market_data.routing.symbol_selectable_in_mt5",
        lambda svc, symbol: False,
    )

    # Fully covered request — no fetch attempted, so no error.
    bars = service.get_ohlcv(
        "PETR4",
        "H1",
        datetime(2024, 1, 11, 10),
        datetime(2024, 1, 14, 10),
    )
    assert len(bars) == 4
    assert remote.calls == []


def test_connection_error_without_coverage_raises(market_root, monkeypatch):
    _seed_local("PETR4", "H1", range(10, 16))
    local = local_store.available_range("PETR4", "H1")
    assert local is not None

    remote = _CountingRemote()

    def _fail(symbol, timeframe, start, end):
        remote.calls.append((symbol, timeframe, start, end))
        raise ConnectionError("gateway down")

    remote.get_ohlcv = _fail
    service = _auto_remote_service(remote)
    monkeypatch.setattr(
        "q_backend.market_data.routing.symbol_selectable_in_mt5",
        lambda svc, symbol: False,
    )

    with pytest.raises(ConnectionError, match="gateway down"):
        service.get_ohlcv(
            "PETR4",
            "H1",
            datetime(2024, 1, 11),
            datetime(2024, 1, 20),
        )


def test_persist_failure_stitches_in_memory(market_root, monkeypatch, caplog):
    _seed_local("PETR4", "H1", range(10, 16))
    local = local_store.available_range("PETR4", "H1")
    assert local is not None
    tail_start = local.end
    tail_end = datetime(2024, 1, 20, 10)
    tail_bars = [_bar(17), _bar(18)]

    remote = _CountingRemote(
        bars_by_range={(tail_start, tail_end): tail_bars},
    )
    service = _auto_remote_service(remote)
    monkeypatch.setattr(
        "q_backend.market_data.routing.symbol_selectable_in_mt5",
        lambda svc, symbol: False,
    )

    def _boom(*_args, **_kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(local_store, "write_ohlcv", _boom)

    with caplog.at_level("WARNING"):
        bars = service.get_ohlcv(
            "PETR4",
            "H1",
            datetime(2024, 1, 11, 10),
            tail_end,
        )

    assert len(bars) == 7
    assert any("Fetch-through write_ohlcv failed" in rec.message for rec in caplog.records)


def test_explicit_remote_full_range_even_when_local_covers(market_root):
    _seed_local("PETR4", "H1", range(10, 16))
    full_bars = [_bar(1), _bar(2), _bar(3)]
    remote = _CountingRemote()
    remote.get_ohlcv = lambda symbol, timeframe, start, end: (
        remote.calls.append((symbol, timeframe, start, end)) or list(full_bars)
    )
    service = MarketDataService()
    service._remote_client = remote
    set_data_source("remote")

    start = datetime(2024, 1, 11)
    end = datetime(2024, 1, 14)
    result = service.get_ohlcv("PETR4", "H1", start, end)

    assert len(remote.calls) == 1
    assert remote.calls[0][2:] == (start, end)
    assert result == full_bars
