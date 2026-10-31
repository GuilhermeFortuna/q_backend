"""Simulated broker semantics under virtual market time (fake monotonic clock, explicit ticks)."""

from __future__ import annotations

import uuid
from pathlib import Path

import pytest

from q_backend.replay.broker import DuplicateIntentError
from q_backend.replay.service import ReplaySimulator

from .support import BASE_US, QUOTE, TRADE, VirtualTime, config_for, ms, reopen, spec, virtual_simulator

SYMBOL = "WIN$N"


def book(seconds: float, bid: float, ask: float) -> tuple:
    return (ms(seconds), bid, ask, 0.0, 0.0, QUOTE)


def trade(seconds: float, bid: float, ask: float, last: float, volume: float = 1.0) -> tuple:
    return (ms(seconds), bid, ask, last, volume, TRADE)


def cmd(sim: ReplaySimulator, action: str, **params):
    run = sim.status()
    return sim.command(command_id=str(uuid.uuid4()), expected_revision=run.revision, action=action, **params)


def at(seconds: float) -> int:
    return BASE_US + int(round(seconds * 1_000_000))


def cursor(sim: ReplaySimulator) -> int:
    return sim._cursor_us()  # noqa: SLF001 - the committed cursor is the visibility watermark


def intent(sim: ReplaySimulator, body: dict, action: str = "deal") -> str:
    intent_id = str(uuid.uuid4())
    sim.ingress_intent(intent_id, action, body)
    return intent_id


def buy(volume: float = 1.0, **extra) -> dict:
    return {"symbol": SYMBOL, "side": "buy", "volume": volume, **extra}


def pending(price: float, order_type: int, volume: float = 1.0, **extra) -> dict:
    return {"symbol": SYMBOL, "type": order_type, "volume": volume, "price": price, **extra}


def run_to(sim: ReplaySimulator, clock: VirtualTime, seconds_total: float) -> None:
    """Advance the monotonic clock to ``seconds_total`` of market time since the run start."""
    target = int(round(seconds_total * 1000))
    current = (cursor(sim) - BASE_US) // 1000
    clock.advance_ms(target - current)
    sim.tick()


def outcome(sim: ReplaySimulator, intent_id: str):
    return sim.broker().intent_outcome(intent_id, cursor(sim))


def latency(**values) -> dict:
    return {"order_transport_ms": 0, "broker_accept_ms": 0, "broker_execution_ms": 0, "broker_response_ms": 0, **values}


@pytest.fixture
def clock() -> VirtualTime:
    return VirtualTime()


def _sim(tmp_path: Path, clock, ticks, *, lat=None, specs=None, end=60, balance="100000.00"):
    config = config_for(latency=lat or latency(), end_seconds=end, balance=balance)
    specs = specs or {SYMBOL: spec()}
    sim = virtual_simulator(tmp_path, clock, {SYMBOL: ticks}, specs, config)
    cmd(sim, "resume")
    return sim


def test_latency_example_fills_at_execution_price_and_visible_at_response(tmp_path, clock) -> None:
    ticks = [book(0, 99, 100), book(0.1, 100, 101), book(0.2, 102, 103)]
    sim = _sim(
        tmp_path,
        clock,
        ticks,
        lat=latency(order_transport_ms=50, broker_accept_ms=50, broker_execution_ms=150, broker_response_ms=100),
    )
    intent_id = intent(sim, buy())
    run_to(sim, clock, 0.340)
    assert outcome(sim, intent_id) is None  # not yet visible: response delay is 100 ms after 250 ms
    run_to(sim, clock, 0.350)
    result = outcome(sim, intent_id)
    assert result["outcome"] == "accepted" and result["retcode"] == 10009
    deals = sim.broker().deals(at(0.35), 0, 2**62)
    assert [deal["price"] for deal in deals] == [103.0]  # fill at the quote present at 250 ms


def test_zero_latency_market_order_fills_at_ingress_quote(tmp_path, clock) -> None:
    ticks = [book(0, 99, 100), book(0.1, 100, 101)]
    sim = _sim(tmp_path, clock, ticks)
    intent_id = intent(sim, buy())
    run_to(sim, clock, 0.0)
    assert outcome(sim, intent_id)["outcome"] == "accepted"
    assert sim.broker().deals(at(0), 0, 2**62)[0]["price"] == 100.0


def test_triggered_stop_is_committed_and_fills_at_execution_quote(tmp_path, clock) -> None:
    ticks = [book(0, 99, 100), book(0.2, 102, 103), book(0.3, 97, 98)]
    sim = _sim(tmp_path, clock, ticks, lat=latency(broker_execution_ms=150))
    placed = intent(sim, pending(102.0, 4), action="pending")
    run_to(sim, clock, 0.0)
    order = outcome(sim, placed)["order_ticket"]
    run_to(sim, clock, 0.25)  # trigger at 0.2 s, execution at 0.35 s
    assert sim.broker().orders(at(0.25))[0]["ticket"] == order
    run_to(sim, clock, 0.4)
    deals = sim.broker().deals(at(0.4), 0, 2**62)
    assert [deal["price"] for deal in deals] == [98.0]  # the retreat after the trigger does not cancel it


def test_limit_that_is_no_longer_executable_at_due_time_keeps_working(tmp_path, clock) -> None:
    ticks = [book(0, 99, 100), book(0.1, 97, 98), book(0.2, 100, 101)]
    sim = _sim(tmp_path, clock, ticks, lat=latency(broker_execution_ms=150))
    placed = intent(sim, pending(99.0, 2), action="pending")
    run_to(sim, clock, 0.0)
    order = outcome(sim, placed)["order_ticket"]
    run_to(sim, clock, 0.35)  # limit became executable at 0.1 s; due at 0.25 s when ask is 101
    assert sim.broker().deals(at(0.35), 0, 2**62) == []
    assert [row["ticket"] for row in sim.broker().orders(at(0.35))] == [order]


def test_expiry_is_exclusive_of_a_trigger_at_the_same_instant(tmp_path, clock) -> None:
    expiration_s = int(BASE_US // 1_000_000) + 1
    ticks = [book(0, 99, 100), book(1.0, 97, 98)]
    sim = _sim(tmp_path, clock, ticks)
    placed = intent(sim, pending(99.0, 2, expiration=expiration_s), action="pending")
    run_to(sim, clock, 0.0)
    order = outcome(sim, placed)["order_ticket"]
    run_to(sim, clock, 1.0)
    assert sim.broker().deals(at(1.0), 0, 2**62) == []
    assert sim.broker().orders(at(1.0)) == []  # expired at the boundary, before the tick
    assert order in [
        row[0]
        for row in sim._store._conn.execute(  # noqa: SLF001
            "SELECT ticket FROM broker_orders WHERE status = 'expired'"
        ).fetchall()
    ]


def test_cancel_of_working_order_succeeds_and_cancel_of_triggered_stop_is_refused(tmp_path, clock) -> None:
    ticks = [book(0, 99, 100), book(0.2, 102, 103)]
    sim = _sim(tmp_path, clock, ticks)
    limit = intent(sim, pending(99.0, 2), action="pending")
    stop = intent(sim, pending(102.0, 4), action="pending")
    run_to(sim, clock, 0.0)
    limit_ticket = outcome(sim, limit)["order_ticket"]
    stop_ticket = outcome(sim, stop)["order_ticket"]
    removed = intent(sim, {"symbol": SYMBOL, "order_ticket": limit_ticket}, action="remove")
    run_to(sim, clock, 0.1)
    assert outcome(sim, removed)["outcome"] == "accepted"
    assert [row["ticket"] for row in sim.broker().orders(at(0.1))] == [stop_ticket]
    run_to(sim, clock, 0.25)  # stop triggered at 0.2 s; it is committed now
    refused = intent(sim, {"symbol": SYMBOL, "order_ticket": stop_ticket}, action="remove")
    run_to(sim, clock, 0.3)
    assert outcome(sim, refused)["outcome"] == "rejected"


def test_insufficient_margin_is_refused_at_acceptance(tmp_path, clock) -> None:
    ticks = [book(0, 99, 100)]
    sim = _sim(tmp_path, clock, ticks, balance="150.00")
    intent_id = intent(sim, buy(volume=2.0))  # 2 x 100 margin exceeds 150
    run_to(sim, clock, 0.0)
    result = outcome(sim, intent_id)
    assert result["outcome"] == "rejected" and result["retcode"] == 10019
    assert sim.broker().deals(at(0), 0, 2**62) == []


def test_protective_stop_exits_with_execution_latency_and_flattens(tmp_path, clock) -> None:
    ticks = [book(0, 99, 100), book(0.1, 97, 98)]
    sim = _sim(tmp_path, clock, ticks, lat=latency(broker_execution_ms=50))
    intent_id = intent(sim, buy(sl=98.0))
    run_to(sim, clock, 0.05)  # entry executes after 50 ms of broker execution latency
    assert outcome(sim, intent_id)["outcome"] == "accepted"
    run_to(sim, clock, 0.15)  # stop triggers at 0.1 s; exit executes at 0.15 s at the bid then
    deals = sim.broker().deals(at(0.15), 0, 2**62)
    assert [deal["reason"] for deal in deals] == ["client", "sl"]
    assert deals[1]["price"] == 97.0 and deals[1]["profit"] == pytest.approx(-3.0)
    assert sim.broker().positions(at(0.15)) == []


def test_net_reduction_and_reversal_produce_correct_deals_and_basis(tmp_path, clock) -> None:
    ticks = [book(0, 99, 100), book(0.5, 99, 100)]
    sim = _sim(tmp_path, clock, ticks)
    first = intent(sim, buy(volume=2.0))
    run_to(sim, clock, 0.0)
    second = intent(sim, {"symbol": SYMBOL, "side": "sell", "volume": 3.0})
    run_to(sim, clock, 0.1)
    assert outcome(sim, first)["outcome"] == "accepted" and outcome(sim, second)["outcome"] == "accepted"
    deals = sim.broker().deals(at(0.1), 0, 2**62)
    assert [(deal["entry"], deal["volume"]) for deal in deals] == [(0, 2.0), (1, 2.0), (0, 1.0)]
    assert deals[1]["profit"] == pytest.approx(-2.0)  # bought 2 at 100, closed 2 at bid 99
    position = sim.broker().positions(at(0.1))[0]
    assert position["type"] == 1 and position["volume"] == 1.0 and position["price_open"] == 99.0


def test_commission_is_a_negative_cash_flow_on_each_deal(tmp_path, clock) -> None:
    ticks = [book(0, 99, 100)]
    specs = {SYMBOL: spec(commission_per_contract="1.50")}
    sim = _sim(tmp_path, clock, ticks, specs=specs)
    intent_id = intent(sim, buy())
    run_to(sim, clock, 0.0)
    assert outcome(sim, intent_id)["outcome"] == "accepted"
    deal = sim.broker().deals(at(0), 0, 2**62)[0]
    assert deal["commission"] == pytest.approx(-1.5)
    assert sim.broker().account(at(0))["balance"] == pytest.approx(100000.0 - 1.5)


def test_stale_quote_refuses_a_market_order(tmp_path, clock) -> None:
    ticks = [book(0, 99, 100)]
    sim = _sim(tmp_path, clock, ticks, end=120)
    run_to(sim, clock, 61.0)
    intent_id = intent(sim, buy())
    run_to(sim, clock, 61.0)
    result = outcome(sim, intent_id)
    assert result["outcome"] == "rejected" and result["retcode"] == 10021


def test_repeated_intent_is_refused_and_never_resubmitted(tmp_path, clock) -> None:
    ticks = [book(0, 99, 100)]
    sim = _sim(tmp_path, clock, ticks)
    intent_id = str(uuid.uuid4())
    sim.ingress_intent(intent_id, "deal", buy())
    with pytest.raises(DuplicateIntentError):
        sim.ingress_intent(intent_id, "deal", buy())
    run_to(sim, clock, 0.0)
    assert len(sim.broker().deals(at(0), 0, 2**62)) == 1


def test_restart_after_durable_fill_reconciles_to_one_fill(tmp_path, clock) -> None:
    ticks = [book(0, 99, 100), book(0.2, 99, 100)]
    sim = _sim(tmp_path, clock, ticks, lat=latency(broker_execution_ms=300, broker_response_ms=100))
    intent_id = intent(sim, buy())
    run_to(sim, clock, 0.25)  # the fill is scheduled; the client is about to time out
    sim._store.close()  # noqa: SLF001 - simulated crash between advances
    restarted = reopen(tmp_path, clock)
    assert restarted.status().state == "paused"  # recovered paused, never resumed by itself
    cmd(restarted, "resume")
    run_to(restarted, clock, 1.0)
    deals = restarted.broker().deals(at(1.0), 0, 2**62)
    assert len(deals) == 1
    assert outcome(restarted, intent_id)["outcome"] == "accepted"


def test_lookup_is_indeterminate_while_in_flight_and_definitive_after(tmp_path, clock) -> None:
    ticks = [book(0, 99, 100)]
    sim = _sim(tmp_path, clock, ticks, lat=latency(order_transport_ms=100, broker_response_ms=100))
    intent_id = intent(sim, buy())
    assert sim.broker().lookup(intent_id, "deal", at(0))["outcome"] == "unavailable"
    assert sim.broker().lookup(str(uuid.uuid4()), "deal", at(0))["outcome"] == "not_found"
    run_to(sim, clock, 0.2)
    found = sim.broker().lookup(intent_id, "deal", at(0.2))
    assert found["outcome"] == "filled" and found["closes_intent"] is True


def test_check_is_advisory_and_reports_margin(tmp_path, clock) -> None:
    ticks = [book(0, 99, 100)]
    sim = _sim(tmp_path, clock, ticks)
    intent_id = intent(sim, buy(volume=3.0), action="check")
    run_to(sim, clock, 0.0)
    result = outcome(sim, intent_id)
    assert result["allowed"] is True and result["margin"] == pytest.approx(300.0)
    assert sim.broker().deals(at(0), 0, 2**62) == []


def test_end_of_data_cancels_working_orders_without_inventing_fills(tmp_path, clock) -> None:
    ticks = [book(0, 99, 100)]
    sim = _sim(tmp_path, clock, ticks, end=2)
    limit = intent(sim, pending(98.0, 2), action="pending")
    run_to(sim, clock, 0.0)
    order = outcome(sim, limit)["order_ticket"]
    run_to(sim, clock, 3.0)
    assert sim.status().state == "completed"
    assert sim.broker().orders(at(2.0)) == []
    assert sim.broker().deals(at(2.0), 0, 2**62) == []
    status = sim._store._conn.execute(  # noqa: SLF001
        "SELECT status, reason FROM broker_orders WHERE ticket = ?", (order,)
    ).fetchone()
    assert status == ("cancelled", "end_of_data")
