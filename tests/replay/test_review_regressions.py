"""Regressions for Q-145's reopened broker, visibility and recovery review."""

from __future__ import annotations

import json
import uuid

import pytest
from fastapi.testclient import TestClient

from q_backend.replay.dataset import DatasetError
from q_backend.replay.edge import create_edge_app

from .support import VirtualTime, reopen
from .test_broker import SYMBOL, _sim, at, book, buy, cmd, intent, latency, outcome, run_to


def test_retreated_take_profit_rearms_once_without_reversing(tmp_path):
    clock = VirtualTime()
    sim = _sim(
        tmp_path,
        clock,
        [book(0, 99, 100), book(0.2, 105, 106), book(0.25, 102, 103), book(0.4, 105, 106), book(0.6, 105, 106)],
        lat=latency(broker_execution_ms=100),
    )
    intent(sim, buy(tp=105))
    run_to(sim, clock, 0.1)
    run_to(sim, clock, 0.35)
    run_to(sim, clock, 0.6)
    assert sim.broker().positions(at(0.6)) == []
    assert [d["reason"] for d in sim.broker().deals(at(0.6), 0, 2**62)] == ["client", "tp"]


@pytest.mark.parametrize("side, expected", [("buy", 5), ("sell", -7)])
def test_account_equity_marks_visible_positions_at_ingress(tmp_path, side, expected):
    clock = VirtualTime()
    sim = _sim(tmp_path, clock, [book(0, 99, 100), book(0.2, 105, 106)])
    intent(sim, {**buy(), "side": side})
    run_to(sim, clock, 0.2)
    account = sim.broker().account(at(0.2))
    assert account["equity"] == pytest.approx(100000 + expected)
    assert account["margin_free"] == pytest.approx(account["equity"] - 100)
    assert account["balance"] == 100000


@pytest.mark.parametrize("ending", ["completed", "stopped"])
def test_finished_run_reads_are_immediate_and_submissions_refused(tmp_path, ending):
    clock = VirtualTime()
    sim = _sim(tmp_path, clock, [book(0, 99, 100)], lat=latency(broker_response_ms=100), end=1)
    if ending == "completed":
        run_to(sim, clock, 1)
    else:
        cmd(sim, "stop")
    assert sim.read(lambda t: sim.broker().account(t), timeout_s=0.01)["balance"] == 100000
    with pytest.raises(ValueError, match="not active"):
        sim.submit(str(uuid.uuid4()), "deal", buy(), timeout_s=0.01)
    with TestClient(create_edge_app(sim)) as client:
        reply = client.post("/v1/submit", json={"intent_id": str(uuid.uuid4()), "order": buy()})
    assert reply.status_code == 400 and reply.json()["code"] == "invalid_request"


def test_completion_drains_last_fill_response_without_executing_at_end(tmp_path):
    clock = VirtualTime()
    sim = _sim(
        tmp_path,
        clock,
        [book(0, 99, 100), book(1, 200, 201)],
        lat=latency(broker_execution_ms=950, broker_response_ms=100),
        end=1,
    )
    filled = intent(sim, buy())
    run_to(sim, clock, 1)
    assert sim.status().state == "completed"
    assert sim.status().canonical_cursor_us == at(1)
    assert outcome(sim, filled)["outcome"] == "accepted"
    result = sim.read(lambda t: sim.broker().positions(t), timeout_s=0.01)
    assert len(result) == 1 and result[0]["price_open"] == 100


def test_finish_resolves_an_intent_still_in_transport(tmp_path):
    clock = VirtualTime()
    sim = _sim(tmp_path, clock, [book(0, 99, 100)], lat=latency(order_transport_ms=2000), end=1)
    pending = intent(sim, buy())
    run_to(sim, clock, 1)
    assert outcome(sim, pending)["outcome"] == "rejected"
    assert sim.status().queue_depth == 0


def test_paused_read_timeout_has_contracted_error(tmp_path, monkeypatch):
    import q_backend.replay.edge as edge

    clock = VirtualTime()
    sim = _sim(tmp_path, clock, [book(0, 99, 100)], lat=latency(broker_response_ms=100))
    cmd(sim, "pause")
    monkeypatch.setattr(edge, "READ_TIMEOUT_S", 0.01)
    with TestClient(create_edge_app(sim), raise_server_exceptions=False) as client:
        result = client.get("/v1/account")
    assert result.status_code == 504
    assert result.json()["code"] == "read_timeout"


@pytest.mark.parametrize("mutation", ["data", "manifest"])
def test_recovery_reverifies_frozen_dataset_identity(tmp_path, mutation):
    clock = VirtualTime()
    sim = _sim(tmp_path, clock, [book(0, 99, 100)])
    sim._store.close()
    if mutation == "data":
        with (tmp_path / "WINN.npz").open("ab") as stream:
            stream.write(b"changed")
    else:
        import hashlib

        path = tmp_path / "WINN.npz"
        with path.open("ab") as stream:
            stream.write(b"changed")
        manifest = json.loads((tmp_path / "manifest.json").read_text())
        manifest["files"][0]["sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
        manifest["files"][0]["size_bytes"] = path.stat().st_size
        (tmp_path / "manifest.json").write_text(json.dumps(manifest))
    with pytest.raises(DatasetError):
        reopen(tmp_path, clock)


def test_status_reports_scheduler_lag_and_unsettled_intents(tmp_path):
    clock = VirtualTime()
    sim = _sim(tmp_path, clock, [book(0, 99, 100)], lat=latency(broker_response_ms=100))
    intent(sim, buy())
    sim.tick()
    assert sim.status().outstanding_intents == 1
    clock.advance_ms(250)
    assert sim.status().lag_ms == 250
    sim.tick()
    assert sim.status().lag_ms == 0
    assert sim.status().outstanding_intents == 0
    assert sim.status().source_coverage["coverage_start_utc"]


@pytest.mark.parametrize("ending", ["completed", "stopped"])
def test_final_quote_and_equity_never_include_post_cutoff_ticks(tmp_path, ending):
    clock = VirtualTime()
    sim = _sim(
        tmp_path,
        clock,
        [book(0, 99, 100), book(0.55, 199, 200), book(1, 299, 300)],
        lat=latency(broker_response_ms=100),
        end=1,
    )
    intent(sim, buy())
    run_to(sim, clock, 0.5)
    if ending == "stopped":
        cmd(sim, "stop")
        expected_bid = 99
    else:
        run_to(sim, clock, 1)
        expected_bid = 199
    quote = sim.read(lambda t: sim.broker().quote(SYMBOL, t), timeout_s=0.01)
    account = sim.read(lambda t: sim.broker().account(t), timeout_s=0.01)
    assert quote["bid"] == expected_bid
    assert account["equity"] == pytest.approx(100000 + expected_bid - 100)
