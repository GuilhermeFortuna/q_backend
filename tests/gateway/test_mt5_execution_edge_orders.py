"""Execution edge pending, protective, order-listing and deal-reason behaviour (edge 1.2)."""

from __future__ import annotations

import json
import time

import pytest

from tests.gateway.conftest import http_get, http_post, running_edge_server
from tests.gateway.fake_metatrader5 import (
    make_deal,
    make_history_order,
    make_position,
    make_working_order,
    reset_order_send,
)
from tests.gateway.test_mt5_execution_edge import (
    INTENT_ID,
    SCHEMA_MAJOR_HEADER,
    _submit_payload,
    _validate,
)

PENDING_INTENT = "cccccccc-1111-2222-3333-444444444444"
SLTP_INTENT = "dddddddd-1111-2222-3333-444444444444"
REMOVE_INTENT = "eeeeeeee-1111-2222-3333-444444444444"
FUTURE_EXPIRY = int(time.time()) + 3600


def _pending_payload(intent_id: str = PENDING_INTENT, **order_overrides):
    order = {
        "symbol": "WIN$",
        "volume": 1.0,
        "type": 4,
        "price": 130100.0,
        "sl": 129900.0,
        "tp": 130400.0,
        "deviation": 5,
    }
    order.update(order_overrides)
    return {"intent_id": intent_id, "action": "pending", "order": order}


def _post_submit(base: str, payload: dict):
    status, _headers, body = http_post(base, "/v1/submit", payload, headers=SCHEMA_MAJOR_HEADER)
    return status, json.loads(body)


def _post_lookup(base: str, payload: dict):
    status, _headers, body = http_post(base, "/v1/lookup", payload, headers=SCHEMA_MAJOR_HEADER)
    assert status == 200
    return json.loads(body)


def _lookup_window(intent_id: str, kind: str = "pending") -> dict:
    return {
        "intent_id": intent_id,
        "kind": kind,
        "window_start": "2020-01-01T00:00:00",
        "window_end": "2030-01-01T00:00:00",
    }


def test_health_advertises_edge_1_2(edge, fake_mt5):
    with running_edge_server(edge) as base:
        status, _headers, body = http_get(base, "/v1/health")
    assert status == 200
    assert json.loads(body)["schema_version"] == "1.2"


def test_deal_with_explicit_action_carries_protective_levels(edge, fake_mt5):
    reset_order_send(fake_mt5)
    intent = "11111111-aaaa-bbbb-cccc-dddddddddddd"
    payload = _submit_payload(intent_id=intent, sl=129900.0, tp=130300.0)
    payload["action"] = "deal"
    with running_edge_server(edge) as base:
        status, outcome = _post_submit(base, payload)
    assert status == 200
    assert outcome["outcome"] == "accepted"
    _validate(outcome, "execution/submit-outcome.schema.json")
    request = fake_mt5._state["order_send_requests"][-1]
    assert request["action"] == fake_mt5.TRADE_ACTION_DEAL
    assert request["sl"] == 129900.0
    assert request["tp"] == 130300.0


def test_market_shape_without_action_still_refuses_levels(edge, fake_mt5):
    reset_order_send(fake_mt5)
    with running_edge_server(edge) as base:
        status, outcome = _post_submit(
            base,
            _submit_payload(intent_id="22222222-aaaa-bbbb-cccc-dddddddddddd", tp=130300.0),
        )
    assert status == 400
    assert outcome["code"] == "invalid_request"
    assert fake_mt5._state["order_send_calls"] == 0


def test_pending_stop_with_expiration_sends_specified_time_order(edge, fake_mt5):
    reset_order_send(fake_mt5)
    payload = _pending_payload(expiration=FUTURE_EXPIRY)
    with running_edge_server(edge) as base:
        status, outcome = _post_submit(base, payload)
    assert status == 200
    assert outcome["outcome"] == "accepted"
    _validate(outcome, "execution/submit-outcome.schema.json")
    request = fake_mt5._state["order_send_requests"][-1]
    assert request["action"] == fake_mt5.TRADE_ACTION_PENDING
    assert request["type"] == fake_mt5.ORDER_TYPE_BUY_STOP
    assert request["type_time"] == fake_mt5.ORDER_TIME_SPECIFIED
    assert request["expiration"] == FUTURE_EXPIRY
    assert request["price"] == 130100.0
    assert request["sl"] == 129900.0
    assert request["tp"] == 130400.0
    assert request["magic"] == edge.intent_magic(PENDING_INTENT)
    assert request["comment"] == edge.intent_comment(PENDING_INTENT)


@pytest.mark.parametrize(
    ("order_type", "expected"),
    [(2, "ORDER_TYPE_BUY_LIMIT"), (3, "ORDER_TYPE_SELL_LIMIT"), (5, "ORDER_TYPE_SELL_STOP")],
)
def test_pending_without_expiration_is_good_till_cancelled(edge, fake_mt5, order_type, expected):
    reset_order_send(fake_mt5)
    intent = f"33333333-0000-0000-0000-00000000000{order_type}"
    with running_edge_server(edge) as base:
        status, outcome = _post_submit(base, _pending_payload(intent_id=intent, type=order_type))
    assert status == 200
    assert outcome["outcome"] == "accepted"
    request = fake_mt5._state["order_send_requests"][-1]
    assert request["type"] == getattr(fake_mt5, expected)
    assert request["type_time"] == fake_mt5.ORDER_TIME_GTC
    assert "expiration" not in request


def test_pending_rejects_market_type_missing_price_and_past_expiry(edge, fake_mt5):
    reset_order_send(fake_mt5)
    cases = [
        (_pending_payload(intent_id="44444444-0000-0000-0000-000000000001", type=0), "invalid_request"),
        (
            _pending_payload(intent_id="44444444-0000-0000-0000-000000000002", price=None),
            "invalid_request",
        ),
        (
            _pending_payload(intent_id="44444444-0000-0000-0000-000000000003", expiration=1_000_000_000),
            "invalid_request",
        ),
        (
            _pending_payload(intent_id="44444444-0000-0000-0000-000000000004", type_time=0),
            "invalid_request",
        ),
    ]
    with running_edge_server(edge) as base:
        for payload, code in cases:
            if payload["order"].get("price") is None:
                payload["order"].pop("price", None)
            status, outcome = _post_submit(base, payload)
            assert status == 400, payload
            assert outcome["code"] == code
    assert fake_mt5._state["order_send_calls"] == 0


def test_pending_intent_field_mismatch_makes_no_terminal_calls(edge, fake_mt5):
    reset_order_send(fake_mt5)
    payload = _pending_payload(intent_id="55555555-0000-0000-0000-000000000001", magic=7)
    with running_edge_server(edge) as base:
        status, outcome = _post_submit(base, payload)
    assert status == 400
    assert outcome["code"] == "intent_field_mismatch"
    assert fake_mt5._state["order_send_calls"] == 0


def test_sltp_sends_position_levels_and_reports_position_ticket(edge, fake_mt5):
    reset_order_send(fake_mt5)
    payload = {
        "intent_id": SLTP_INTENT,
        "action": "sltp",
        "symbol": "WIN$",
        "position": 42,
        "sl": 129950.0,
        "tp": 130500.0,
    }
    with running_edge_server(edge) as base:
        status, outcome = _post_submit(base, payload)
    assert status == 200
    assert outcome == {"outcome": "accepted", "order_ticket": 42, "retcode": fake_mt5.TRADE_RETCODE_DONE}
    request = fake_mt5._state["order_send_requests"][-1]
    assert request == {
        "action": fake_mt5.TRADE_ACTION_SLTP,
        "symbol": "WIN$",
        "position": 42,
        "sl": 129950.0,
        "tp": 130500.0,
    }


def test_remove_cancels_the_named_pending_order(edge, fake_mt5):
    reset_order_send(fake_mt5)
    payload = {"intent_id": REMOVE_INTENT, "action": "remove", "symbol": "WIN$", "order_ticket": 223344}
    with running_edge_server(edge) as base:
        status, outcome = _post_submit(base, payload)
    assert status == 200
    assert outcome["outcome"] == "accepted"
    assert outcome["order_ticket"] == 223344
    assert fake_mt5._state["order_send_requests"][-1] == {
        "action": fake_mt5.TRADE_ACTION_REMOVE,
        "order": 223344,
    }


def test_duplicate_protective_intent_calls_order_send_once(edge, fake_mt5):
    reset_order_send(fake_mt5)
    payload = {"intent_id": SLTP_INTENT, "action": "sltp", "symbol": "WIN$", "position": 42, "sl": 1.0, "tp": 2.0}
    with running_edge_server(edge) as base:
        first_status, _ = _post_submit(base, payload)
        second_status, second = _post_submit(base, payload)
    assert first_status == 200
    assert second_status == 400
    assert second["code"] == "duplicate_intent"
    assert fake_mt5._state["order_send_calls"] == 1


@pytest.mark.parametrize(
    "payload",
    [
        {"intent_id": "66666666-0000-0000-0000-000000000001", "action": "sltp", "symbol": "WIN$", "position": 1},
        {"intent_id": "66666666-0000-0000-0000-000000000002", "action": "remove", "symbol": "WIN$"},
        {"intent_id": "66666666-0000-0000-0000-000000000003", "action": "modify", "symbol": "WIN$"},
    ],
)
def test_protective_actions_require_their_fields(edge, fake_mt5, payload):
    reset_order_send(fake_mt5)
    with running_edge_server(edge) as base:
        status, outcome = _post_submit(base, payload)
    assert status == 400
    assert outcome["code"] == "invalid_request"
    assert fake_mt5._state["order_send_calls"] == 0


def test_orders_lists_working_pending_orders_with_symbol_filter(edge, fake_mt5):
    fake_mt5._state["active_orders"] = [
        make_working_order(ticket=223344, symbol="WIN$", order_type=4, price=130100.0, sl=129900.0, tp=130400.0),
        make_working_order(ticket=223345, symbol="WINQ26", order_type=3, price=130300.0, time_expiration=FUTURE_EXPIRY),
    ]
    with running_edge_server(edge) as base:
        status, _headers, body = http_get(base, "/v1/orders", headers=SCHEMA_MAJOR_HEADER)
        filtered_status, _headers, filtered_body = http_get(
            base, "/v1/orders", params={"symbol": "WIN$"}, headers=SCHEMA_MAJOR_HEADER
        )
    assert status == 200
    orders = json.loads(body)
    _validate(orders, "execution/orders-response.schema.json")
    assert [row["ticket"] for row in orders] == [223344, 223345]
    assert orders[0] == {
        "ticket": 223344,
        "symbol": "WIN$",
        "type": 4,
        "volume": 1.0,
        "price": 130100.0,
        "expiration": None,
        "sl": 129900.0,
        "tp": 130400.0,
        "magic": 0,
        "comment": "q:test",
        "time_setup": 1_710_000_000,
    }
    assert orders[1]["expiration"] == FUTURE_EXPIRY
    assert filtered_status == 200
    assert [row["ticket"] for row in json.loads(filtered_body)] == [223344]


def test_orders_is_unavailable_when_terminal_disconnected(edge, fake_mt5):
    fake_mt5._state["init_ok"] = False
    with running_edge_server(edge) as base:
        status, _headers, body = http_get(base, "/v1/orders", headers=SCHEMA_MAJOR_HEADER)
    assert status == 503
    assert json.loads(body)["code"] == "mt5_unavailable"


def test_deals_carry_position_id_and_reason(edge, fake_mt5):
    magic = edge.intent_magic(INTENT_ID)
    comment = edge.intent_comment(INTENT_ID)
    fake_mt5._state["history_deals"] = [
        make_deal(magic=magic, comment=comment, position_id=123456, reason=fake_mt5.DEAL_REASON_CLIENT),
        make_deal(ticket=98766, magic=magic, comment="[sl 129900]", position_id=123456, reason=fake_mt5.DEAL_REASON_SL),
        make_deal(ticket=98767, magic=0, comment="", reason=fake_mt5.DEAL_REASON_SO),
    ]
    with running_edge_server(edge) as base:
        status, _headers, body = http_post(
            base,
            "/v1/deals",
            {"window_start": "2020-01-01T00:00:00", "window_end": "2030-01-01T00:00:00"},
            headers=SCHEMA_MAJOR_HEADER,
        )
    assert status == 200
    deals = json.loads(body)
    _validate(deals, "execution/deals-response.schema.json")
    assert [(d["position_id"], d["reason"]) for d in deals] == [
        (123456, "client"),
        (123456, "sl"),
        (0, "so"),
    ]


def test_pending_lookup_placed_while_order_rests(edge, fake_mt5):
    magic = edge.intent_magic(PENDING_INTENT)
    comment = edge.intent_comment(PENDING_INTENT)
    fake_mt5._state["active_orders"] = [
        make_working_order(ticket=223344, magic=magic, comment=comment),
    ]
    with running_edge_server(edge) as base:
        payload = _lookup_window(PENDING_INTENT)
        outcome = _post_lookup(base, payload)
    assert outcome == {"outcome": "placed", "order_ticket": 223344, "closes_intent": False}
    _validate(outcome, "execution/pending-lookup-outcome.schema.json")


def test_pending_lookup_filled_reports_entry_deals(edge, fake_mt5):
    magic = edge.intent_magic(PENDING_INTENT)
    comment = edge.intent_comment(PENDING_INTENT)
    fake_mt5._state["history_deals"] = [
        make_deal(ticket=98800, order=223344, magic=magic, comment=comment, position_id=42, reason=0),
    ]
    with running_edge_server(edge) as base:
        outcome = _post_lookup(base, _lookup_window(PENDING_INTENT))
    assert outcome["outcome"] == "filled"
    assert outcome["closes_intent"] is True
    assert outcome["deals"][0]["position_id"] == 42
    _validate(outcome, "execution/pending-lookup-outcome.schema.json")


@pytest.mark.parametrize(
    ("state_name", "expected"),
    [
        ("ORDER_STATE_CANCELED", "cancelled"),
        ("ORDER_STATE_EXPIRED", "expired"),
        ("ORDER_STATE_REJECTED", "rejected"),
    ],
)
def test_pending_lookup_terminal_states_from_history(edge, fake_mt5, state_name, expected):
    magic = edge.intent_magic(PENDING_INTENT)
    comment = edge.intent_comment(PENDING_INTENT)
    state = getattr(fake_mt5, state_name)
    fake_mt5._state["history_orders"] = [
        make_history_order(ticket=223344, magic=magic, comment=comment, state=state),
    ]
    with running_edge_server(edge) as base:
        outcome = _post_lookup(base, _lookup_window(PENDING_INTENT))
    assert outcome["outcome"] == expected
    assert outcome["closes_intent"] is True
    _validate(outcome, "execution/pending-lookup-outcome.schema.json")


def test_pending_lookup_not_found_closes_intent(edge, fake_mt5):
    with running_edge_server(edge) as base:
        outcome = _post_lookup(base, _lookup_window("77777777-0000-0000-0000-000000000001"))
    assert outcome == {"outcome": "not_found", "closes_intent": True}
    _validate(outcome, "execution/pending-lookup-outcome.schema.json")


def test_pending_lookup_unavailable_when_not_initialized(edge, fake_mt5):
    fake_mt5._state["init_ok"] = False
    with running_edge_server(edge) as base:
        outcome = _post_lookup(base, _lookup_window(PENDING_INTENT))
    assert outcome["outcome"] == "unavailable"
    assert outcome["closes_intent"] is False
    _validate(outcome, "execution/pending-lookup-outcome.schema.json")


def test_market_lookup_is_unchanged_by_pending_branch(edge, fake_mt5):
    magic = edge.intent_magic(PENDING_INTENT)
    comment = edge.intent_comment(PENDING_INTENT)
    fake_mt5._state["active_orders"] = [make_working_order(ticket=223344, magic=magic, comment=comment)]
    with running_edge_server(edge) as base:
        outcome = _post_lookup(base, _lookup_window(PENDING_INTENT, kind="deal"))
    assert outcome == {"outcome": "not_found", "closes_intent": True}
    _validate(outcome, "execution/lookup-outcome.schema.json")


def test_position_fixture_carries_levels(edge, fake_mt5):
    fake_mt5._state["positions"] = [make_position(ticket=42)]
    with running_edge_server(edge) as base:
        status, _headers, body = http_get(base, "/v1/positions", headers=SCHEMA_MAJOR_HEADER)
    assert status == 200
    _validate(json.loads(body), "execution/positions-response.schema.json")
