from __future__ import annotations

import json
import threading
from pathlib import Path

import jsonschema
import pytest
from referencing import Registry

from tests.gateway.conftest import SCHEMA_ROOT, http_get, http_post, running_edge_server
from tests.gateway.fake_metatrader5 import (
    make_deal,
    make_history_order,
    make_position,
    make_send_result,
    reset_order_send,
    set_order_send_barrier,
)

INTENT_ID = "12345678-1234-5678-1234-567812345678"
SCHEMA_MAJOR_HEADER = {"X-Schema-Major": "1"}


def _registry() -> Registry:
    resources: list[tuple[str, dict]] = []
    for path in SCHEMA_ROOT.rglob("*.schema.json"):
        payload = json.loads(path.read_text())
        rel = path.relative_to(SCHEMA_ROOT).as_posix()
        for resource_id in {rel, path.name, f"edge/{rel}", str(payload.get("$id", ""))}:
            if resource_id:
                resources.append((resource_id, payload))
    return Registry().with_contents(resources)


def _validate(instance: dict | list, schema_path: str) -> None:
    schema = json.loads((SCHEMA_ROOT / schema_path).read_text())
    jsonschema.Draft202012Validator(schema, registry=_registry()).validate(instance)


def _submit_payload(intent_id: str = INTENT_ID, **order_overrides):
    order = {
        "symbol": "WIN$",
        "volume": 1.0,
        "side": "buy",
        "price": 130010.0,
        "deviation": 5,
    }
    order.update(order_overrides)
    return {"intent_id": intent_id, "order": order}


def test_health_validates(edge, fake_mt5):
    with running_edge_server(edge) as base:
        status, _headers, body = http_get(base, "/v1/health")
    assert status == 200
    payload = json.loads(body)
    _validate(payload, "common/health.schema.json")


def test_schema_major_two_is_refused(edge, fake_mt5):
    with running_edge_server(edge) as base:
        status, _headers, body = http_get(
            base,
            "/v1/quote",
            {"symbol": "WIN$"},
            headers={"X-Schema-Major": "2"},
        )
    assert status == 400
    payload = json.loads(body)
    assert payload["code"] == "schema_major_mismatch"
    _validate(payload, "common/error.schema.json")


def test_account_connected_validates(edge, fake_mt5):
    with running_edge_server(edge) as base:
        status, _headers, body = http_get(base, "/v1/account", headers=SCHEMA_MAJOR_HEADER)
    assert status == 200
    _validate(json.loads(body), "execution/account-response.schema.json")


def test_account_disconnected_returns_mt5_unavailable(edge, fake_mt5):
    fake_mt5._state["init_ok"] = False
    with running_edge_server(edge) as base:
        status, _headers, body = http_get(base, "/v1/account", headers=SCHEMA_MAJOR_HEADER)
    assert status == 503
    payload = json.loads(body)
    assert payload["code"] == "mt5_unavailable"
    _validate(payload, "common/error.schema.json")


def test_quote_validates(edge, fake_mt5):
    with running_edge_server(edge) as base:
        status, _headers, body = http_get(base, "/v1/quote", {"symbol": "WIN$"}, headers=SCHEMA_MAJOR_HEADER)
    assert status == 200
    _validate(json.loads(body), "execution/quote-response.schema.json")


def test_check_allowed_validates(edge, fake_mt5):
    with running_edge_server(edge) as base:
        status, _headers, body = http_post(
            base,
            "/v1/check",
            _submit_payload(),
            headers=SCHEMA_MAJOR_HEADER,
        )
    assert status == 200
    _validate(json.loads(body), "execution/check-response.schema.json")


def test_check_disallowed_validates(edge, fake_mt5):
    fake_mt5._state["order_check_retcode"] = fake_mt5.TRADE_RETCODE_REJECT
    with running_edge_server(edge) as base:
        status, _headers, body = http_post(
            base,
            "/v1/check",
            _submit_payload(),
            headers=SCHEMA_MAJOR_HEADER,
        )
    assert status == 200
    payload = json.loads(body)
    assert payload["allowed"] is False
    _validate(payload, "execution/check-response.schema.json")


def test_positions_and_deals_validate(edge, fake_mt5):
    fake_mt5._state["positions"] = [make_position()]
    fake_mt5._state["history_deals"] = [
        make_deal(magic=edge.intent_magic(INTENT_ID), comment=edge.intent_comment(INTENT_ID))
    ]
    with running_edge_server(edge) as base:
        pos_status, _h1, pos_body = http_get(base, "/v1/positions", headers=SCHEMA_MAJOR_HEADER)
        deals_status, _h2, deals_body = http_post(
            base,
            "/v1/deals",
            {"window_start": "2026-01-01T00:00:00", "window_end": "2026-12-31T00:00:00"},
            headers=SCHEMA_MAJOR_HEADER,
        )
    assert pos_status == 200
    assert deals_status == 200
    _validate(json.loads(pos_body), "execution/positions-response.schema.json")
    _validate(json.loads(deals_body), "execution/deals-response.schema.json")


def test_submit_accepted_validates(edge, fake_mt5):
    reset_order_send(fake_mt5)
    with running_edge_server(edge) as base:
        status, _headers, body = http_post(base, "/v1/submit", _submit_payload(), headers=SCHEMA_MAJOR_HEADER)
    assert status == 200
    payload = json.loads(body)
    assert payload["outcome"] == "accepted"
    assert fake_mt5._state["order_send_calls"] == 1
    _validate(payload, "execution/submit-outcome.schema.json")


def test_submit_rejected_validates(edge, fake_mt5):
    reset_order_send(fake_mt5)
    fake_mt5._state["order_send_default"] = make_send_result(retcode=fake_mt5.TRADE_RETCODE_REJECT)
    with running_edge_server(edge) as base:
        status, _headers, body = http_post(
            base,
            "/v1/submit",
            _submit_payload(intent_id="aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"),
            headers=SCHEMA_MAJOR_HEADER,
        )
    assert status == 200
    payload = json.loads(body)
    assert payload["outcome"] == "rejected"
    assert fake_mt5._state["order_send_calls"] == 1
    _validate(payload, "execution/submit-outcome.schema.json")


@pytest.mark.parametrize(
    "setup",
    [
        lambda fake: fake._state.update({"order_send_queue": [None]}),
        lambda fake: fake._state.update({"order_send_raises": RuntimeError("boom")}),
        lambda fake: fake._state.update({"order_send_default": make_send_result(retcode=fake.TRADE_RETCODE_TIMEOUT)}),
        lambda fake: fake._state.update(
            {"order_send_default": make_send_result(retcode=fake.TRADE_RETCODE_NO_CONNECTION)}
        ),
    ],
)
def test_submit_indeterminate_paths_call_order_send_once(edge, fake_mt5, setup):
    reset_order_send(fake_mt5)
    setup(fake_mt5)
    intent = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
    with running_edge_server(edge) as base:
        status, _headers, body = http_post(
            base,
            "/v1/submit",
            _submit_payload(intent_id=intent),
            headers=SCHEMA_MAJOR_HEADER,
        )
    assert status == 200
    payload = json.loads(body)
    assert payload["outcome"] == "indeterminate"
    assert fake_mt5._state["order_send_calls"] == 1
    _validate(payload, "execution/submit-outcome.schema.json")


def test_concurrent_duplicate_submit_calls_order_send_once(edge, fake_mt5):
    reset_order_send(fake_mt5)
    barrier = set_order_send_barrier(fake_mt5)
    results: list[tuple[int, dict]] = []
    server = edge.build_server("127.0.0.1", 0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address[:2]
    base = f"http://{host}:{port}"
    payload = _submit_payload(intent_id="cccccccc-cccc-cccc-cccc-cccccccccccc")

    def worker():
        status, _headers, body = http_post(base, "/v1/submit", payload, headers=SCHEMA_MAJOR_HEADER)
        results.append((status, json.loads(body)))

    first = threading.Thread(target=worker)
    second = threading.Thread(target=worker)
    first.start()
    second.start()
    barrier.set()
    first.join(timeout=5)
    second.join(timeout=5)
    server.shutdown()
    server.server_close()
    thread.join(timeout=5)

    assert fake_mt5._state["order_send_calls"] == 1
    labels = sorted(payload["code"] if status != 200 else payload["outcome"] for status, payload in results)
    assert labels == ["accepted", "duplicate_intent"] or labels == ["duplicate_intent", "duplicate_intent"]


def test_duplicate_after_completion(edge, fake_mt5):
    reset_order_send(fake_mt5)
    intent = "dddddddd-dddd-dddd-dddd-dddddddddddd"
    with running_edge_server(edge) as base:
        first_status, _h1, first_body = http_post(
            base,
            "/v1/submit",
            _submit_payload(intent_id=intent),
            headers=SCHEMA_MAJOR_HEADER,
        )
        second_status, _h2, second_body = http_post(
            base,
            "/v1/submit",
            _submit_payload(intent_id=intent),
            headers=SCHEMA_MAJOR_HEADER,
        )
        third_status, _h3, third_body = http_post(
            base,
            "/v1/submit",
            _submit_payload(intent_id=intent),
            headers=SCHEMA_MAJOR_HEADER,
        )
    assert first_status == 200
    assert json.loads(first_body)["outcome"] == "accepted"
    assert second_status == 400
    assert json.loads(second_body)["code"] == "duplicate_intent"
    assert third_status == 400
    assert json.loads(third_body)["code"] == "duplicate_intent"
    assert fake_mt5._state["order_send_calls"] == 1


def test_intent_field_mismatch_makes_no_terminal_calls(edge, fake_mt5):
    reset_order_send(fake_mt5)
    with running_edge_server(edge) as base:
        status, _headers, body = http_post(
            base,
            "/v1/submit",
            _submit_payload(magic=999),
            headers=SCHEMA_MAJOR_HEADER,
        )
    assert status == 400
    assert json.loads(body)["code"] == "intent_field_mismatch"
    assert fake_mt5._state["order_send_calls"] == 0


def test_unsupported_order_fields_are_refused(edge, fake_mt5):
    reset_order_send(fake_mt5)
    with running_edge_server(edge) as base:
        status, _headers, body = http_post(
            base,
            "/v1/submit",
            _submit_payload(sl=1.0, intent_id="eeeeeeee-eeee-eeee-eeee-eeeeeeeeeeee"),
            headers=SCHEMA_MAJOR_HEADER,
        )
    assert status == 400
    assert json.loads(body)["code"] == "invalid_request"
    assert fake_mt5._state["order_send_calls"] == 0


def test_lookup_filled_validates(edge, fake_mt5):
    magic = edge.intent_magic(INTENT_ID)
    comment = edge.intent_comment(INTENT_ID)
    fake_mt5._state["history_deals"] = [make_deal(magic=magic, comment=comment)]
    with running_edge_server(edge) as base:
        status, _headers, body = http_post(
            base,
            "/v1/lookup",
            {
                "intent_id": INTENT_ID,
                "window_start": "2020-01-01T00:00:00",
                "window_end": "2030-01-01T00:00:00",
            },
            headers=SCHEMA_MAJOR_HEADER,
        )
    assert status == 200
    payload = json.loads(body)
    assert payload["outcome"] == "filled"
    assert payload["closes_intent"] is True
    _validate(payload, "execution/lookup-outcome.schema.json")


def test_lookup_rejected_validates(edge, fake_mt5):
    magic = edge.intent_magic(INTENT_ID)
    comment = edge.intent_comment(INTENT_ID)
    fake_mt5._state["history_orders"] = [
        make_history_order(
            magic=magic,
            comment=comment,
            state=fake_mt5.ORDER_STATE_REJECTED,
        )
    ]
    with running_edge_server(edge) as base:
        status, _headers, body = http_post(
            base,
            "/v1/lookup",
            {
                "intent_id": INTENT_ID,
                "window_start": "2020-01-01T00:00:00",
                "window_end": "2030-01-01T00:00:00",
            },
            headers=SCHEMA_MAJOR_HEADER,
        )
    assert status == 200
    payload = json.loads(body)
    assert payload["outcome"] == "rejected"
    assert payload["closes_intent"] is True
    _validate(payload, "execution/lookup-outcome.schema.json")


def test_lookup_not_found_validates(edge, fake_mt5):
    with running_edge_server(edge) as base:
        status, _headers, body = http_post(
            base,
            "/v1/lookup",
            {
                "intent_id": INTENT_ID,
                "window_start": "2020-01-01T00:00:00",
                "window_end": "2030-01-01T00:00:00",
            },
            headers=SCHEMA_MAJOR_HEADER,
        )
    assert status == 200
    payload = json.loads(body)
    assert payload["outcome"] == "not_found"
    assert payload["closes_intent"] is True
    _validate(payload, "execution/lookup-outcome.schema.json")


def test_lookup_unavailable_when_not_initialized(edge, fake_mt5):
    fake_mt5._state["init_ok"] = False
    with running_edge_server(edge) as base:
        status, _headers, body = http_post(
            base,
            "/v1/lookup",
            {
                "intent_id": INTENT_ID,
                "window_start": "2020-01-01T00:00:00",
                "window_end": "2030-01-01T00:00:00",
            },
            headers=SCHEMA_MAJOR_HEADER,
        )
    assert status == 200
    payload = json.loads(body)
    assert payload["outcome"] == "unavailable"
    assert payload["closes_intent"] is False
    _validate(payload, "execution/lookup-outcome.schema.json")


def test_lookup_unavailable_when_history_returns_none(edge, fake_mt5):
    fake_mt5._state["history_deals_returns_none"] = True
    with running_edge_server(edge) as base:
        status, _headers, body = http_post(
            base,
            "/v1/lookup",
            {
                "intent_id": INTENT_ID,
                "window_start": "2020-01-01T00:00:00",
                "window_end": "2030-01-01T00:00:00",
            },
            headers=SCHEMA_MAJOR_HEADER,
        )
    assert status == 200
    payload = json.loads(body)
    assert payload["outcome"] == "unavailable"
    assert payload["closes_intent"] is False
    _validate(payload, "execution/lookup-outcome.schema.json")
