"""A slow history call never stalls the market scheduler (Q-145 acceptance 3)."""

from __future__ import annotations

import threading
import time
import uuid

import httpx

from q_backend.replay.gateway import Ingress, ServerThread, create_gateway_app
from q_backend.replay.journal import ReplayRunStore
from q_backend.replay.service import Driver, ReplaySimulator

from .support import QUOTE, config_for, ms, spec, write_dataset


def test_slow_history_request_does_not_stall_the_market(tmp_path) -> None:
    book = [(ms(t / 10), 99.0, 100.0, 0.0, 0.0, QUOTE) for t in range(0, 600)]
    write_dataset(tmp_path, {"WIN$N": book}, {"WIN$N": spec()})
    store = ReplayRunStore(tmp_path / "iso.sqlite3")
    simulator = ReplaySimulator(store, dataset_root=tmp_path)
    simulator.create(config_for(end_seconds=600), run_id="iso", generation="gen-iso")
    simulator.command(command_id=str(uuid.uuid4()), expected_revision=0, action="resume")
    driver = Driver(simulator, interval_s=0.002)
    driver.start()

    market = simulator.market
    entered = threading.Event()
    real_ohlcv = market.ohlcv

    def slow_ohlcv(*args, **kwargs):
        entered.set()
        time.sleep(1.5)  # a history query stuck for 1.5 s
        return real_ohlcv(*args, **kwargs)

    market.ohlcv = slow_ohlcv  # type: ignore[method-assign]
    app = create_gateway_app(market=market, ingress=simulator.gateway_ingress)
    server = ServerThread(app)
    url = server.start()
    try:
        result: dict = {}

        def request() -> None:
            with httpx.Client(base_url=url, timeout=10.0) as client:
                result["status"] = client.get(
                    "/v1/ohlcv",
                    params={
                        "symbol": "WIN$N",
                        "timeframe": "M1",
                        "start": "2026-10-01T08:00:00",
                        "end": "2026-10-01T10:00:00",
                    },
                ).status_code

        thread = threading.Thread(target=request)
        thread.start()
        assert entered.wait(5.0)
        before = simulator.status().canonical_cursor_us
        time.sleep(0.5)  # the request is still stuck in the slow query
        after = simulator.status().canonical_cursor_us
        assert after > before, "the market must keep moving while a history query is slow"
        thread.join(10.0)
        assert result.get("status") == 200
    finally:
        driver.stop()
        server.stop()
        store.close()
