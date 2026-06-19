"""Storage API and ingest job tests (WO48)."""

from __future__ import annotations

from datetime import datetime
from unittest.mock import patch

import pytest

from q_backend.api import storage_jobs
from q_backend.api.main import (
    delete_storage_series,
    get_storage_ingest_status,
    get_storage_inventory,
    market_data_service,
    start_storage_ingest,
)
from q_backend.api.storage_jobs import IngestJobRequest
from q_backend.market_data import local_store
from q_backend.market_data.models import OHLCV


def _bars() -> list[OHLCV]:
    return [
        OHLCV(
            time=datetime(2024, 3, 1),
            open=40.0,
            high=41.0,
            low=39.0,
            close=40.5,
            tick_volume=1000,
        ),
        OHLCV(
            time=datetime(2024, 3, 2),
            open=40.5,
            high=41.5,
            low=40.0,
            close=41.0,
            tick_volume=1100,
        ),
    ]


@pytest.fixture
def market_root(tmp_path, monkeypatch):
    root = tmp_path / "market"
    monkeypatch.setenv("Q_MARKET_DATA_ROOT", str(root))
    return root


def test_inventory_lists_written_series(market_root):
    local_store.write_ohlcv("PETR4", "D1", _bars())
    body = get_storage_inventory()
    assert body["root"] == str(market_root.resolve())
    assert len(body["items"]) == 1
    assert body["items"][0]["symbol"] == "PETR4"
    assert body["items"][0]["rows"] == 2
    assert body["items"][0]["kind"] == "bars"


def test_delete_storage_series(market_root):
    local_store.write_ohlcv("PETR4", "D1", _bars())
    body = delete_storage_series("PETR4", "D1")
    assert body["deleted"] is True
    assert get_storage_inventory()["items"] == []


def test_start_ingest_rejects_unknown_timeframe(market_root):
    with patch.object(market_data_service, "mt5_available", return_value=True):
        with pytest.raises(Exception) as exc_info:
            start_storage_ingest(
                IngestJobRequest(
                    symbol="PETR4",
                    timeframes=["BADTF"],
                    start=datetime(2024, 1, 1),
                    end=datetime(2024, 6, 1),
                )
            )
    assert exc_info.value.status_code == 422


def test_start_ingest_requires_mt5(market_root):
    with patch.object(market_data_service, "mt5_available", return_value=False):
        with pytest.raises(Exception) as exc_info:
            start_storage_ingest(
                IngestJobRequest(
                    symbol="PETR4",
                    timeframes=["D1"],
                    start=datetime(2024, 1, 1),
                    end=datetime(2024, 6, 1),
                )
            )
    assert exc_info.value.status_code == 503


def test_ingest_job_completes_with_faked_mt5(market_root, monkeypatch):
    pytest.importorskip("fakeredis")
    import fakeredis

    fake = fakeredis.FakeRedis(decode_responses=True)
    monkeypatch.setattr(storage_jobs, "get_redis", lambda: fake)

    class _SyncActor:
        def send(self, job_id, request_json):
            storage_jobs.run_ingest_job(job_id, request_json)

    monkeypatch.setattr(
        "q_backend.tasks.actors.run_storage_ingest", _SyncActor(), raising=False
    )
    monkeypatch.setattr(
        "q_backend.tasks.worker_context.get_worker_market_data_service",
        lambda: market_data_service,
    )

    def _fake_ohlcv(symbol, timeframe, start, end):
        return _bars()

    with patch.object(market_data_service, "mt5_available", return_value=True):
        with patch.object(
            market_data_service.mt5_client, "get_ohlcv", side_effect=_fake_ohlcv
        ):
            job_id = start_storage_ingest(
                IngestJobRequest(
                    symbol="PETR4",
                    timeframes=["D1", "H1"],
                    start=datetime(2024, 1, 1),
                    end=datetime(2024, 6, 1),
                )
            )["job_id"]

    status = get_storage_ingest_status(job_id)
    assert status["status"] == "completed"
    assert len(status["results"]) == 2
    assert all(row["status"] == "completed" for row in status["results"])
    assert get_storage_inventory()["items"]


def test_ingest_job_isolates_timeframe_failures(market_root, monkeypatch):
    pytest.importorskip("fakeredis")
    import fakeredis

    fake = fakeredis.FakeRedis(decode_responses=True)
    monkeypatch.setattr(storage_jobs, "get_redis", lambda: fake)

    class _SyncActor:
        def send(self, job_id, request_json):
            storage_jobs.run_ingest_job(job_id, request_json)

    monkeypatch.setattr(
        "q_backend.tasks.actors.run_storage_ingest", _SyncActor(), raising=False
    )
    monkeypatch.setattr(
        "q_backend.tasks.worker_context.get_worker_market_data_service",
        lambda: market_data_service,
    )

    def _fake_ohlcv(symbol, timeframe, start, end):
        if timeframe == "H1":
            return []
        return _bars()

    with patch.object(market_data_service, "mt5_available", return_value=True):
        with patch.object(
            market_data_service.mt5_client, "get_ohlcv", side_effect=_fake_ohlcv
        ):
            job_id = start_storage_ingest(
                IngestJobRequest(
                    symbol="VALE3",
                    timeframes=["D1", "H1"],
                    start=datetime(2024, 1, 1),
                    end=datetime(2024, 6, 1),
                )
            )["job_id"]

    status = get_storage_ingest_status(job_id)
    assert status["status"] == "completed"
    by_tf = {row["timeframe"]: row for row in status["results"]}
    assert by_tf["D1"]["status"] == "completed"
    assert by_tf["H1"]["status"] == "failed"
