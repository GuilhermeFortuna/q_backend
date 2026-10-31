"""Recorded ingress reproduces broker outcomes deterministically (Q-145 reproducibility)."""

from __future__ import annotations

import uuid

from q_backend.replay.journal import ReplayRunStore
from q_backend.replay.service import ReplaySimulator

from .support import QUOTE, TRADE, VirtualTime, config_for, ms, spec, write_dataset, BASE_US


def _scenario(tmp_path, name: str):
    run_dir = tmp_path / name
    run_dir.mkdir()
    ticks = {
        "WIN$N": [
            (ms(0), 99.0, 100.0, 0.0, 0.0, QUOTE),
            (ms(0.1), 100.0, 101.0, 100.5, 2.0, TRADE),
            (ms(0.2), 102.0, 103.0, 102.5, 3.0, TRADE),
            (ms(0.3), 97.0, 98.0, 97.5, 1.0, TRADE),
            (ms(0.5), 98.0, 99.0, 98.5, 1.0, TRADE),
        ]
    }
    write_dataset(run_dir, ticks, {"WIN$N": spec()})
    clock = VirtualTime()
    store = ReplayRunStore(run_dir / "replay.sqlite3")
    sim = ReplaySimulator(store, dataset_root=run_dir, mono_ns=clock)
    sim.create(
        config_for(
            latency={
                "order_transport_ms": 50,
                "broker_accept_ms": 50,
                "broker_execution_ms": 150,
                "broker_response_ms": 100,
            }
        ),
        run_id="repro",
        generation="gen-repro",
    )
    sim.command(command_id=str(uuid.uuid4()), expected_revision=0, action="resume")
    for gap_ms, body in [
        (0, {"symbol": "WIN$N", "side": "buy", "volume": 1.0}),
        (300, {"symbol": "WIN$N", "side": "sell", "volume": 2.0}),
    ]:
        clock.advance_ms(gap_ms)
        sim.ingress_intent(str(uuid.uuid4()), "deal", body)
        for _ in range(4):
            clock.advance_ms(100)
            sim.tick()
    deals = sim.broker().deals(BASE_US + 2_000_000, 0, 2**62)
    return [(d["entry"], d["volume"], d["price"], d["time_msc"], d["reason"]) for d in deals]


def test_recorded_ingress_reproduces_identical_deals(tmp_path) -> None:
    first = _scenario(tmp_path, "run-a")
    second = _scenario(tmp_path, "run-b")
    assert first and first == second
