"""Live Redis-stream WebSocket endpoint."""

from __future__ import annotations

from fastapi import APIRouter, WebSocket
from redis.exceptions import RedisError
from starlette.responses import JSONResponse

from q_backend.streaming.redis_binary import get_async_binary_redis
from q_backend.streaming.ws.session import StreamSession

router = APIRouter(tags=["stream"])


@router.websocket("/api/v1/stream")
async def stream_socket(websocket: WebSocket) -> None:
    redis = get_async_binary_redis()
    try:
        await redis.ping()
    except RedisError:
        await redis.aclose()
        await websocket.send_denial_response(JSONResponse({"code": "stream_unavailable"}, status_code=503))
        return
    await websocket.accept()
    await StreamSession(websocket, redis).run()
