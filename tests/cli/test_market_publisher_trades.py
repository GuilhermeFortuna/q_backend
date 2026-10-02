"""Publisher CLI wiring for the session trade tape."""

from __future__ import annotations

import fakeredis

from q_backend.cli.q_market_publisher import build_trade_service
from q_backend.market_data.clients.remote import RemoteMt5Client
from q_backend.storage.settings import Settings


def test_build_trade_service_creates_one_coordinator_per_symbol_from_settings(tmp_path):
    settings = Settings(
        _env_file=None,
        trade_cache_dir=str(tmp_path / "cache"),
        trade_cache_max_backfills=1,
        trade_group_settle_ms=1500,
        trade_poll_interval_s=0.5,
    )
    service = build_trade_service(
        fakeredis.FakeRedis(), RemoteMt5Client(base_url="http://127.0.0.1:9"), ("WINZ26", "WDOZ26"), settings
    )

    assert list(service.coordinators) == ["WINZ26", "WDOZ26"]
    assert service.max_backfills == 1
    assert service.cache.root == tmp_path / "cache"
    coordinator = service.coordinators["WINZ26"]
    assert coordinator.settle_ms == 1500 and coordinator.poll_interval_s == 0.5
    # One sink: both symbols share the topic-level watermark.
    assert coordinator.sink is service.coordinators["WDOZ26"].sink
