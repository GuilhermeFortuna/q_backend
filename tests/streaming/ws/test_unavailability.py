import redis.asyncio
from fastapi.testclient import TestClient
from redis.exceptions import ConnectionError
from starlette.testclient import WebSocketDenialResponse

from q_backend.api.main import app
from q_backend.streaming.ws.queue import TopicQueue
from q_backend.streaming.ws.session import StreamSession
from q_contracts.topics import TOPICS


def test_handshake_refuses_a_closed_redis_port_with_503(monkeypatch):
    """Accepting a socket during a Redis outage strands clients without recovery semantics."""
    monkeypatch.setattr(
        "q_backend.api.routers.stream.get_async_binary_redis",
        lambda: redis.asyncio.Redis(host="127.0.0.1", port=6399, socket_connect_timeout=0.05),
    )

    with TestClient(app) as client:
        try:
            with client.websocket_connect("/api/v1/stream"):
                raise AssertionError("the stream handshake unexpectedly succeeded")
        except WebSocketDenialResponse as exc:
            assert exc.status_code == 503
            assert exc.json() == {"code": "stream_unavailable"}


def test_mid_connection_redis_loss_rejects_then_closes():
    """A silent socket close makes reconnecting clients mistake an outage for a protocol fault."""

    class Socket:
        def __init__(self):
            self.messages = []
            self.close_code = None

        async def send_text(self, value):
            self.messages.append(value)

        async def close(self, code):
            self.close_code = code

    class FailingRedis:
        async def xread(self, *_args, **_kwargs):
            raise ConnectionError("down")

    async def exercise():
        socket = Socket()
        session = StreamSession(socket, FailingRedis())
        session.cursors["jobs.terminal"] = "0-0"
        session.queues["jobs.terminal"] = TopicQueue("jobs.terminal", TOPICS["jobs.terminal"], 2)
        await session._read_loop()
        assert socket.messages == ['{"reason":"stream_unavailable"}']
        assert socket.close_code == 1011

    import asyncio

    asyncio.run(exercise())
