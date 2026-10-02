"""Trade snapshot and history routes against a real publisher-side service over fakeredis."""

from __future__ import annotations

import io
import threading
from datetime import datetime, timedelta, timezone

import pyarrow as pa
import pytest
from fastapi.testclient import TestClient

from q_backend.api.main import app
from q_backend.api.routers import trades as trades_router
from q_backend.api.schemas.trades import HISTORY_HEADER_NAMES
from tests.streaming.replay_schema import assert_valid_replay, load_replay_validator
from tests.streaming.trade_fakes import FakeTradeSource, World, row


class _Pump:
    def __init__(self, world):
        self.world, self.stop = world, threading.Event()
        self.thread = threading.Thread(target=self._run, daemon=True)

    def _run(self):
        while not self.stop.is_set():
            self.world.service.step()
            self.stop.wait(0.005)

    def __enter__(self):
        self.world.service.start()
        self.thread.start()
        return self

    def __exit__(self, *_):
        self.stop.set()
        self.thread.join(timeout=5)


@pytest.fixture
def api(tmp_path):
    world = World(tmp_path, FakeTradeSource([row(-9000 + i // 2, 100.0 + i) for i in range(30)]))
    app.dependency_overrides[trades_router.get_trade_client] = lambda: world.client
    app.dependency_overrides[trades_router.get_trade_reader] = lambda: world.reader
    yield TestClient(app), world
    app.dependency_overrides.pop(trades_router.get_trade_client, None)
    app.dependency_overrides.pop(trades_router.get_trade_reader, None)


def test_snapshot_then_pages_match_the_contract(api):
    client, world = api
    world.backfill()
    with _Pump(world):
        response = client.get("/api/v1/market/trades/snapshot", params={"symbol": world.symbol})
        assert response.status_code == 200
        body = response.json()
        assert_valid_replay("trade-snapshot", body)
        assert body["trade_count"] == 30 and body["coverage"]["coverage_state"] == "complete"
        assert body["source_generation"] == world.coordinator.generation

        rows, url = [], body["first_page_url"]
        cursor_headers = []
        while url:
            page = client.get(url)
            assert page.status_code == 200
            assert page.headers["content-type"].startswith("application/vnd.apache.arrow.stream")
            headers = {name: page.headers[header] for name, header in HISTORY_HEADER_NAMES.items()}
            contract_headers = {
                **headers,
                "page_count": int(headers["page_count"]),
                "frozen_seq": int(headers["frozen_seq"]),
                "next_cursor": headers["next_cursor"] or None,
            }
            errors = list(load_replay_validator("trade-history-headers").iter_errors(contract_headers))
            assert not errors, errors[0].message
            assert headers["snapshot_id"] == body["snapshot_id"]
            assert headers["frozen_epoch"] == body["frozen_watermark"]["epoch"]
            assert int(headers["frozen_seq"]) == body["frozen_watermark"]["seq"]
            assert headers["volume_field"] == body["volume_field"]
            table = pa.ipc.open_stream(io.BytesIO(page.content)).read_all()
            rows += table.to_pylist()
            cursor_headers.append(headers["next_cursor"])
            url = (
                f"/api/v1/market/trades/history?snapshot_id={body['snapshot_id']}&limit=7&cursor={headers['next_cursor']}"
                if headers["next_cursor"]
                else None
            )
        assert len(rows) == 30
        assert cursor_headers[-1] == ""


def test_pages_are_identical_on_every_read_while_live_trades_continue(api):
    client, world = api
    world.backfill()
    with _Pump(world):
        body = client.get("/api/v1/market/trades/snapshot", params={"symbol": world.symbol}).json()
        url = f"/api/v1/market/trades/history?snapshot_id={body['snapshot_id']}&limit=10"
        first = client.get(url)
        world.live(row(0), row(0), advance_s=3)
        second = client.get(url)

    assert first.content == second.content
    assert first.headers["X-Q-Trade-Frozen-Seq"] == second.headers["X-Q-Trade-Frozen-Seq"]


def test_backfill_in_progress_returns_202_with_retry_after_never_an_empty_snapshot(api):
    client, world = api
    with _Pump(world):
        response = client.get("/api/v1/market/trades/snapshot", params={"symbol": world.symbol})
        # The pump started backfilling already; hold a coordinator that cannot finish.
        world.coordinator.phase = "backfilling"
        pending = client.get("/api/v1/market/trades/snapshot", params={"symbol": world.symbol})

    assert response.status_code in (200, 202)
    assert pending.status_code == 202
    assert pending.headers["Retry-After"] == "1"
    assert_valid_replay("trade-history-pending", pending.json())
    assert "X-Q-Trade-Backfill-Rows" in pending.headers


def test_unknown_symbol_and_malformed_requests(api):
    client, world = api
    world.backfill()
    with _Pump(world):
        unknown = client.get("/api/v1/market/trades/snapshot", params={"symbol": "NOPE"})
        invalid = client.get("/api/v1/market/trades/snapshot", params={"symbol": "../../etc/passwd"})
        missing = client.get("/api/v1/market/trades/snapshot")

    assert unknown.status_code == 404 and unknown.json()["code"] == "unknown_symbol"
    assert invalid.status_code == 404
    assert missing.status_code == 422


def test_history_errors_distinguish_unknown_expired_and_replaced_tokens(api):
    client, world = api
    world.backfill()
    with _Pump(world):
        body = client.get("/api/v1/market/trades/snapshot", params={"symbol": world.symbol}).json()
        snapshot_id = body["snapshot_id"]
        unknown = client.get("/api/v1/market/trades/history", params={"snapshot_id": "A" * 22})
        traversal = client.get("/api/v1/market/trades/history", params={"snapshot_id": "../../etc"})
        bad_cursor = client.get("/api/v1/market/trades/history", params={"snapshot_id": snapshot_id, "cursor": "zz"})
        too_big = client.get("/api/v1/market/trades/history", params={"snapshot_id": snapshot_id, "limit": 50001})

        world.coordinator._begin_generation("overlap_mismatch")
        world.service.step()
        replaced = client.get("/api/v1/market/trades/history", params={"snapshot_id": snapshot_id})

        world.reader.cache.clock = lambda: datetime.now(timezone.utc) + timedelta(hours=1)
        expired = client.get("/api/v1/market/trades/history", params={"snapshot_id": snapshot_id})

    assert unknown.status_code == 404 and traversal.status_code == 404
    assert bad_cursor.status_code == 400 and too_big.status_code == 422
    assert replaced.status_code == 410 and replaced.json()["code"] == "snapshot_expired"
    assert "generation" in replaced.json()["message"]
    assert expired.status_code == 410


def test_publisher_down_is_a_503_not_an_empty_history(api):
    client, world = api
    snapshot = client.get("/api/v1/market/trades/snapshot", params={"symbol": world.symbol})
    assert snapshot.status_code == 503 and snapshot.json()["code"] == "trade_source_unavailable"

    world.backfill()
    with _Pump(world):
        snapshot_id = client.get("/api/v1/market/trades/snapshot", params={"symbol": world.symbol}).json()[
            "snapshot_id"
        ]
    world.redis.delete("q:trades:instance")  # the publisher stopped heartbeating
    history = client.get("/api/v1/market/trades/history", params={"snapshot_id": snapshot_id})

    assert history.status_code == 503 and history.json()["code"] == "trade_source_unavailable"


def test_resource_limit_and_unavailable_source_are_503s_with_distinct_codes(api):
    client, world = api
    world.backfill()
    world.service.cache.max_bytes = 100
    with _Pump(world):
        limited = client.get("/api/v1/market/trades/snapshot", params={"symbol": world.symbol})

    assert limited.status_code == 503 and limited.json()["code"] == "trade_cache_resource_limit"
