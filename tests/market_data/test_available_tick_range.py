"""Tick history is whatever the active provider can serve, not the local cache."""

from datetime import datetime
from unittest.mock import MagicMock

import numpy as np
import pytest

from q_backend.market_data.clients.remote import RemoteMt5Client
from q_backend.market_data.clients.shared import TickPage
from q_backend.market_data.service import MarketDataService

FIRST_TICK_MSC = 1_582_018_028_883  # 2020-02-18 09:07:08.883 broker wall clock


def _page(times):
    columns = {
        "time_msc": np.asarray(times, dtype=np.int64),
        "bid": np.zeros(len(times)),
        "ask": np.zeros(len(times)),
        "last": np.zeros(len(times)),
        "volume": np.zeros(len(times)),
        "flags": np.zeros(len(times), dtype=np.int32),
    }
    return TickPage(columns=columns, next_cursor_msc=None, exhausted=True)


def test_remote_first_tick_time_retries_an_unsynchronized_empty_page(monkeypatch):
    client = RemoteMt5Client.__new__(RemoteMt5Client)
    pages = iter([_page([]), _page([FIRST_TICK_MSC])])
    client.get_tick_page = lambda *args, **kwargs: next(pages)
    monkeypatch.setattr("q_backend.market_data.clients.remote.time.sleep", lambda _: None)

    first = client.get_first_tick_time("WSP$N")

    assert first is not None and first.year == 2020


def test_remote_first_tick_time_is_none_when_history_stays_empty(monkeypatch):
    client = RemoteMt5Client.__new__(RemoteMt5Client)
    client.get_tick_page = lambda *args, **kwargs: _page([])
    monkeypatch.setattr("q_backend.market_data.clients.remote.time.sleep", lambda _: None)

    assert client.get_first_tick_time("WSP$N") is None


@pytest.mark.parametrize("first", [datetime(2020, 2, 18, 9, 7), None])
def test_service_reports_provider_history_not_local_cache(monkeypatch, first):
    service = MarketDataService.__new__(MarketDataService)
    remote = MagicMock()
    remote.get_first_tick_time.return_value = first
    service._remote_client = remote
    service._local_client = MagicMock()
    monkeypatch.setattr(service, "_resolve_provider", lambda: remote)

    result = service.get_available_tick_range("WSP$N")

    if first is None:
        assert result is None
    else:
        assert result is not None
        assert result.start == first and result.end > first
