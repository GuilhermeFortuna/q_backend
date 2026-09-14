import asyncio

import pytest
import redis.asyncio
from fastapi.testclient import TestClient
from redis.exceptions import ConnectionError, ResponseError
from starlette.testclient import WebSocketDenialResponse

from q_backend.api.main import app
from q_backend.streaming.ws.session import StreamSession
from tests.streaming.ws.conftest import FakeSocket, eventually


def test_handshake_refuses_a_closed_redis_port_with_503(monkeypatch):
    """Accepting a socket during a Redis outage strands clients without recovery semantics."""
    monkeypatch.setattr(
        "q_backend.api.routers.stream.get_async_binary_redis",
        lambda: redis.asyncio.Redis(host="127.0.0.1", port=6399, socket_connect_timeout=0.05),
    )

    with TestClient(app) as client:
        with pytest.raises(WebSocketDenialResponse) as denial:
            with client.websocket_connect("/api/v1/stream"):
                pass
    assert denial.value.status_code == 503
    assert denial.value.json() == {"code": "stream_unavailable"}


class _RedisLostAfterSubscribe:
    """Answers the subscription, then fails every stream read as a dropped connection would."""

    def __init__(self) -> None:
        self.closed = False

    async def get(self, _key):
        return None

    async def xinfo_stream(self, _key):
        raise ResponseError("no such key")

    async def xread(self, *_args, **_kwargs):
        raise ConnectionError("down")

    async def aclose(self):
        self.closed = True


def test_mid_connection_redis_loss_rejects_then_closes():
    """A silent socket close makes reconnecting clients mistake an outage for a protocol fault."""

    async def exercise():
        socket = FakeSocket()
        client = _RedisLostAfterSubscribe()
        socket.subscribe(["jobs.terminal"])
        await asyncio.wait_for(StreamSession(socket, client).run(), 5)
        return socket, client

    socket, client = asyncio.run(exercise())
    assert socket.frames[-1] == {"type": "rejected", "reason": "stream_unavailable"}
    assert socket.close_code == 1011
    assert client.closed


def test_redis_loss_during_a_subscription_rejects_then_closes():
    class LostOnSubscribe(_RedisLostAfterSubscribe):
        async def xinfo_stream(self, _key):
            raise ConnectionError("down")

    async def exercise():
        socket = FakeSocket()
        session = StreamSession(socket, LostOnSubscribe())
        task = asyncio.create_task(session.run())
        socket.subscribe(["jobs.terminal"])
        await eventually(lambda: socket.close_code is not None)
        await asyncio.wait_for(task, 5)
        return socket

    socket = asyncio.run(exercise())
    assert socket.frames == [{"type": "rejected", "reason": "stream_unavailable"}]
    assert socket.close_code == 1011
