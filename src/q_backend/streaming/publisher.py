from collections.abc import Mapping
from datetime import datetime
import json
from pathlib import Path
import secrets
from typing import Any, Literal
import base64
import redis

from q_backend.storage.db.base import utc_now
from q_backend.streaming.codec import routing_key_string
from q_backend.streaming.keys import latest_key, seq_key, stream_key, topic_epoch_key
from q_backend.streaming.outbox import PAYLOAD_MODELS
from q_contracts.topics import TOPICS

_LUA_PATH = Path(__file__).parent / "lua" / "publish_ephemeral.lua"
_LUA_SCRIPT_TEXT = _LUA_PATH.read_text(encoding="utf-8")


class EphemeralPublishError(RuntimeError):
    """Raised when an ephemeral publish fails."""


class EphemeralPublisher:
    def __init__(self, client: redis.Redis, topic: str, *, producer_id: str) -> None:
        if topic not in TOPICS or TOPICS[topic].topic_class != "ephemeral":
            raise ValueError(f"Topic {topic!r} is not a declared ephemeral topic")
        if not producer_id:
            raise ValueError("producer_id must be non-empty")

        self.client = client
        self.topic = topic
        self.producer_id = producer_id
        self.policy = TOPICS[topic]
        self._script = self.client.register_script(_LUA_SCRIPT_TEXT)

    def publish(
        self,
        *,
        routing_key: Mapping[str, str],
        payload_kind: Literal["arrow_ipc", "control"],
        payload_schema: str,
        payload: bytes | Mapping[str, Any],
        origin_ts: datetime | None = None,
    ) -> tuple[str, int]:
        # 1. Routing key validation
        if self.policy.coalesce_key:
            for k in self.policy.coalesce_key:
                if not routing_key or k not in routing_key or routing_key[k] is None or str(routing_key[k]) == "":
                    raise ValueError(f"Missing required routing key field: {k!r} for topic {self.topic!r}")
            rk_str = routing_key_string(self.topic, routing_key)
        else:
            rk_str = ""

        # 2. Payload validation
        if payload_kind == "control":
            if not isinstance(payload, Mapping):
                raise ValueError("Payload for control kind must be a mapping")
            if payload_schema in PAYLOAD_MODELS:
                try:
                    PAYLOAD_MODELS[payload_schema](**payload)
                except Exception as exc:
                    raise ValueError(f"Payload does not match schema {payload_schema}: {exc}") from exc
            elif (
                self.policy.payload_schema != "schema/stream/envelope.schema.json"
                and payload_schema != self.policy.payload_schema
            ):
                raise ValueError(
                    f"Payload schema {payload_schema!r} does not match topic schema {self.policy.payload_schema!r}"
                )
            payload_bytes = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        elif payload_kind == "arrow_ipc":
            if isinstance(payload, bytes):
                payload_bytes = payload
            elif isinstance(payload, str):
                payload_bytes = base64.b64decode(payload.encode("ascii"))
            else:
                raise ValueError("Payload for arrow_ipc kind must be bytes or base64 str")
            if (
                self.policy.payload_schema != "schema/stream/envelope.schema.json"
                and payload_schema != self.policy.payload_schema
            ):
                raise ValueError(
                    f"Payload schema {payload_schema!r} does not match topic schema {self.policy.payload_schema!r}"
                )
        else:
            raise ValueError(f"Unsupported payload_kind: {payload_kind}")

        # 3. Origin timestamp
        if origin_ts is None:
            origin_ts = utc_now()
        origin_ts_str = origin_ts.isoformat()

        # 4. Construct envelope header without seq and epoch
        header: dict[str, Any] = {
            "topic": self.topic,
            "schema_major": 1,
            "producer_id": self.producer_id,
            "origin_ts": origin_ts_str,
            "payload_kind": payload_kind,
            "payload_schema": payload_schema,
        }
        if routing_key:
            header["key"] = dict(routing_key)
        header_json = json.dumps(header, separators=(",", ":"))

        candidate_epoch = f"{utc_now().strftime('%Y%m%d')}-{secrets.token_hex(4)}"

        # 5. Execute atomic Lua script
        try:
            res = self._script(
                keys=[
                    seq_key(self.topic),
                    topic_epoch_key(self.topic),
                    stream_key(self.topic),
                    latest_key(self.topic),
                ],
                args=[
                    candidate_epoch,
                    header_json,
                    payload_bytes,
                    self.policy.retention_entries,
                    rk_str,
                ],
            )
        except Exception as exc:
            raise EphemeralPublishError(f"Publish failed for topic {self.topic}: {exc}") from exc

        epoch_raw, seq_raw, _ = res
        epoch = epoch_raw.decode("utf-8") if isinstance(epoch_raw, bytes) else str(epoch_raw)
        seq = int(seq_raw)
        return epoch, seq
