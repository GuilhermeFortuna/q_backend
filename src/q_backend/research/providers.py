"""Gateway configuration helpers for the research library."""

from __future__ import annotations


def resolve_gateway_url(explicit: str | None) -> str | None:
    if explicit is not None:
        return explicit or None
    from q_backend.storage.runtime_config import get_remote_gateway_url

    return get_remote_gateway_url()


def resolve_gateway_token(explicit: str | None) -> str | None:
    if explicit is not None:
        return explicit or None
    from q_backend.storage.runtime_config import get_remote_gateway_token

    return get_remote_gateway_token()
