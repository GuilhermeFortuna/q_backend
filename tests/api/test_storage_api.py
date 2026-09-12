"""Storage API and ingest job tests (WO48)."""

from __future__ import annotations

from datetime import datetime
from unittest.mock import patch

import numpy as np
import pytest

from q_backend.api import storage_jobs
from q_backend.api.dependencies import market_data_service
from q_backend.api.routers.storage import (
    delete_storage_series,
    get_storage_ingest_status,
    get_storage_inventory,
    start_storage_ingest,
)
from q_backend.api.schemas.storage import StorageInventoryResponse
from q_backend.api.storage_jobs import IngestJobRequest
from q_backend.market_data import local_store
from q_backend.market_data.models import OHLCV


def _ticks() -> dict[str, np.ndarray]:
    base = int(datetime(2024, 3, 1).timestamp() * 1000)
    return {
        "time_msc": np.array([base, base + 1000], dtype=np.int64),
        "bid": np.array([40.0, 40.1], dtype=np.float64),
        "ask": np.array([40.2, 40.3], dtype=np.float64),
        "last": np.array([40.1, 40.2], dtype=np.float64),
        "volume": np.array([1, 2], dtype=np.float64),
        "flags": np.array([0, 0], dtype=np.int64),
    }


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


def test_inventory_with_ticks_matches_response_model(market_root):
    """Regression: tick entries have no timeframe; the response model must accept
    them so the HTTP endpoint serializes instead of returning 500."""
    local_store.write_ohlcv("PETR4", "D1", _bars())
    local_store.write_ticks("WDO$", _ticks())

    body = get_storage_inventory()

    # FastAPI validates the dict against this model on the way out over HTTP.
    response = StorageInventoryResponse(**body)
    kinds = {item.kind for item in response.items}
    assert "ticks" in kinds
    ticks_item = next(item for item in response.items if item.kind == "ticks")
    assert ticks_item.symbol == "WDO$"
    assert ticks_item.timeframe is None


def test_delete_storage_series(market_root):
    local_store.write_ohlcv("PETR4", "D1", _bars())
    body = delete_storage_series("PETR4", "D1")
    assert body["deleted"] is True
    assert get_storage_inventory()["items"] == []


def test_start_ingest_rejects_unknown_timeframe(market_root):
    with (
        patch.object(market_data_service, "mt5_available", return_value=True),
        patch.object(market_data_service.mt5_client, "is_supported", return_value=True),
    ):
        with pytest.raises(Exception) as exc_info:
            start_storage_ingest(
                IngestJobRequest(
                    symbol="PETR4",
                    timeframes=["BADTF"],
                    start=datetime(2024, 1, 1),
                    end=datetime(2024, 6, 1),
                ),
                mds=market_data_service,
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
                ),
                mds=market_data_service,
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

    monkeypatch.setattr("q_backend.tasks.actors.run_storage_ingest", _SyncActor(), raising=False)
    monkeypatch.setattr(
        "q_backend.tasks.worker_context.get_worker_market_data_service",
        lambda: market_data_service,
    )

    def _fake_ohlcv(symbol, timeframe, start, end):
        return _bars()

    with (
        patch.object(market_data_service, "mt5_available", return_value=True),
        patch.object(market_data_service.mt5_client, "is_supported", return_value=True),
    ):
        with patch.object(market_data_service.mt5_client, "get_ohlcv", side_effect=_fake_ohlcv):
            job_id = start_storage_ingest(
                IngestJobRequest(
                    symbol="PETR4",
                    timeframes=["D1", "H1"],
                    start=datetime(2024, 1, 1),
                    end=datetime(2024, 6, 1),
                ),
                mds=market_data_service,
            )["job_id"]

    status = get_storage_ingest_status(job_id)
    assert status["status"] == "completed"
    assert len(status["results"]) == 2
    assert all(row["status"] == "completed" for row in status["results"])
    assert get_storage_inventory()["items"]


def test_bars_ingest_via_remote_acquisition_provider(market_root, monkeypatch):
    """Ingest must run on a Linux box with only a gateway configured: the job
    resolves its acquisition provider to the remote client and writes the store."""
    pytest.importorskip("fakeredis")
    import fakeredis

    fake = fakeredis.FakeRedis(decode_responses=True)
    monkeypatch.setattr(storage_jobs, "get_redis", lambda: fake)

    class _SyncActor:
        def send(self, job_id, request_json):
            storage_jobs.run_ingest_job(job_id, request_json)

    monkeypatch.setattr("q_backend.tasks.actors.run_storage_ingest", _SyncActor(), raising=False)
    monkeypatch.setattr(
        "q_backend.tasks.worker_context.get_worker_market_data_service",
        lambda: market_data_service,
    )

    class _StubRemote:
        def get_ohlcv(self, symbol, timeframe, start, end):
            return _bars()

    stub_remote = _StubRemote()

    with patch.object(market_data_service, "acquisition_provider", return_value=stub_remote):
        job_id = start_storage_ingest(
            IngestJobRequest(
                symbol="PETR4",
                timeframes=["D1"],
                start=datetime(2024, 1, 1),
                end=datetime(2024, 6, 1),
            ),
            mds=market_data_service,
        )["job_id"]

    status = get_storage_ingest_status(job_id)
    assert status["status"] == "completed"
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

    monkeypatch.setattr("q_backend.tasks.actors.run_storage_ingest", _SyncActor(), raising=False)
    monkeypatch.setattr(
        "q_backend.tasks.worker_context.get_worker_market_data_service",
        lambda: market_data_service,
    )

    def _fake_ohlcv(symbol, timeframe, start, end):
        if timeframe == "H1":
            return []
        return _bars()

    with (
        patch.object(market_data_service, "mt5_available", return_value=True),
        patch.object(market_data_service.mt5_client, "is_supported", return_value=True),
    ):
        with patch.object(market_data_service.mt5_client, "get_ohlcv", side_effect=_fake_ohlcv):
            job_id = start_storage_ingest(
                IngestJobRequest(
                    symbol="VALE3",
                    timeframes=["D1", "H1"],
                    start=datetime(2024, 1, 1),
                    end=datetime(2024, 6, 1),
                ),
                mds=market_data_service,
            )["job_id"]

    status = get_storage_ingest_status(job_id)
    assert status["status"] == "completed"
    by_tf = {row["timeframe"]: row for row in status["results"]}
    assert by_tf["D1"]["status"] == "completed"
    assert by_tf["H1"]["status"] == "failed"


def _synthetic_ticks(n: int = 50) -> dict:
    import numpy as np

    from q_backend.market_data.clients.metatrader import _naive_local_to_time_msc

    base = datetime(2024, 3, 1, 10, 0, 0)
    time_msc = np.array(
        [_naive_local_to_time_msc(base.replace(second=i % 60)) for i in range(n)],
        dtype=np.int64,
    )
    prices = 40.0 + np.arange(n, dtype=np.float64) * 0.01
    return {
        "time_msc": time_msc,
        "bid": prices - 0.01,
        "ask": prices + 0.01,
        "last": prices,
        "volume": np.ones(n, dtype=np.float64),
        "flags": np.zeros(n, dtype=np.int32),
    }


def test_tick_ingest_job_completes_with_faked_mt5(market_root, monkeypatch):
    pytest.importorskip("fakeredis")
    import fakeredis

    fake = fakeredis.FakeRedis(decode_responses=True)
    monkeypatch.setattr(storage_jobs, "get_redis", lambda: fake)

    class _SyncActor:
        def send(self, job_id, request_json):
            storage_jobs.run_ingest_job(job_id, request_json)

    monkeypatch.setattr("q_backend.tasks.actors.run_storage_ingest", _SyncActor(), raising=False)
    monkeypatch.setattr(
        "q_backend.tasks.worker_context.get_worker_market_data_service",
        lambda: market_data_service,
    )

    call_months: list[str] = []

    def _fake_ticks(symbol, start, end, flags=None, use_cache=True):
        call_months.append(f"{start.year}-{start.month:02d}")
        return _synthetic_ticks()

    with (
        patch.object(market_data_service, "mt5_available", return_value=True),
        patch.object(market_data_service.mt5_client, "is_supported", return_value=True),
    ):
        with patch.object(
            market_data_service.mt5_client,
            "get_ticks_columnar",
            side_effect=_fake_ticks,
        ):
            job_id = start_storage_ingest(
                IngestJobRequest(
                    symbol="PETR4",
                    start=datetime(2024, 3, 1),
                    end=datetime(2024, 4, 15),
                    kind="ticks",
                ),
                mds=market_data_service,
            )["job_id"]

    status = get_storage_ingest_status(job_id)
    assert status["status"] == "completed"
    assert len(status["results"]) == 2
    assert all(row["status"] == "completed" for row in status["results"])
    assert call_months == ["2024-03", "2024-04"]

    inventory = get_storage_inventory()["items"]
    tick_rows = [row for row in inventory if row.get("kind") == "ticks"]
    assert len(tick_rows) == 1
    assert tick_rows[0]["symbol"] == "PETR4"
    assert (market_root / "ticks" / "PETR4").is_dir()
