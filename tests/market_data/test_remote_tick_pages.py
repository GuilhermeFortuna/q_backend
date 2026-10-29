import io
import json

import httpx
import numpy as np
import pytest

from q_backend.market_data.clients.remote import RemoteMt5Client


def payload(times, cursor=None, exhausted=True, **overrides):
    metadata = dict(
        next_cursor_msc=cursor,
        exhausted=exhausted,
        first_msc=times[0] if times else None,
        last_msc=times[-1] if times else None,
        row_count=len(times),
    )
    metadata.update(overrides)
    stream = io.BytesIO()
    np.savez_compressed(
        stream,
        time_msc=np.array(times, dtype=np.int64),
        bid=np.ones(len(times)),
        ask=np.ones(len(times)),
        last=np.ones(len(times)),
        volume=np.ones(len(times)),
        flags=np.ones(len(times), dtype=np.int32),
        metadata=np.array(json.dumps(metadata)),
    )
    return stream.getvalue()


def client_for(monkeypatch, response):
    original = httpx.Client
    monkeypatch.setattr(
        httpx, "Client", lambda **kwargs: original(**kwargs, transport=httpx.MockTransport(lambda request: response))
    )
    return RemoteMt5Client(base_url="http://gateway.test")


def test_tick_page_decodes_duplicate_ticks_and_next_cursor(monkeypatch):
    client = client_for(monkeypatch, httpx.Response(200, content=payload([1500, 1500], 1800, False)))
    page = client.get_tick_page("WIN$", 1000, 2000, limit=2)
    assert page.columns["time_msc"].tolist() == [1500, 1500]
    assert page.next_cursor_msc == 1800
    assert not page.exhausted


@pytest.mark.parametrize(
    "content",
    [
        payload([1500], 1000, False),
        payload([1500], 1500, False),
        payload([1500], None, False),
        payload([1500], 1800, True),
        payload([1500], row_count=2),
        payload([900]),
        payload([2000]),
        payload([1600, 1500]),
        payload([1500], first_msc=1400),
        payload([], 1800, False),
    ],
)
def test_tick_page_rejects_invalid_provider_progress(monkeypatch, content):
    client = client_for(monkeypatch, httpx.Response(200, content=content))
    with pytest.raises(ConnectionError, match="Invalid tick page"):
        client.get_tick_page("WIN$", 1000, 2000, limit=2)


def test_old_gateway_requires_update_instead_of_empty_result(monkeypatch):
    client = client_for(monkeypatch, httpx.Response(404, json={"code": "not_found"}))
    with pytest.raises(RuntimeError, match="restart"):
        client.get_tick_page("WIN$", 1000, 2000)


def test_default_tick_page_requests_gateway_maximum_batch(monkeypatch):
    requests = []
    original = httpx.Client

    def respond(request):
        requests.append(request)
        return httpx.Response(200, content=payload([1500]))

    monkeypatch.setattr(httpx, "Client", lambda **kwargs: original(**kwargs, transport=httpx.MockTransport(respond)))
    page = RemoteMt5Client(base_url="http://gateway.test").get_tick_page("WIN$", 1000, 2000)
    assert page.columns["time_msc"].tolist() == [1500]
    assert requests[0].url.params["limit"] == "1000000"
