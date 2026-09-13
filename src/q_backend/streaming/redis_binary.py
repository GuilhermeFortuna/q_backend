import secrets
import redis

from q_backend.storage.db.base import utc_now
from q_backend.storage.settings import get_settings
from q_backend.streaming.keys import STREAM_EPOCH_KEY


def get_binary_redis() -> redis.Redis:
    """Return a Redis client with decode_responses=False for streaming byte safety."""
    settings = get_settings()
    return redis.Redis.from_url(settings.redis_url, decode_responses=False)


def ensure_stream_epoch(client: redis.Redis) -> tuple[str, bool]:
    """(epoch, created) - SET NX a new epoch if absent; created=True means Redis lost its data."""
    candidate_epoch = f"{utc_now().strftime('%Y%m%d')}-{secrets.token_hex(4)}"
    created = bool(client.set(STREAM_EPOCH_KEY, candidate_epoch, nx=True))
    if created:
        return candidate_epoch, True
    existing = client.get(STREAM_EPOCH_KEY)
    if existing is None:
        # In case of concurrent deletion or flush, recurse once to ensure key exists
        return ensure_stream_epoch(client)
    epoch_str = existing.decode("utf-8") if isinstance(existing, bytes) else str(existing)
    return epoch_str, False
