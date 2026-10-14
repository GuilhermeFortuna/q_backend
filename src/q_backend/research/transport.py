"""HTTP dispatch and error mapping shared by the Research publishing helpers."""

from __future__ import annotations

import os
from typing import Any, Optional

DEFAULT_API_URL = "http://127.0.0.1:8001"


def post_research_json(
    path: str,
    payload: dict[str, Any],
    *,
    noun: str,
    endpoint: str,
    api_url: Optional[str],
    client: Any,
    conflict_error: bool = False,
) -> Any:
    """POST ``payload`` to the Research API and return the decoded JSON body.

    ``noun`` names the published object in error messages (``"figure"``), and
    ``endpoint`` names its collection in unexpected-response messages (``"figures"``).
    Status and transport errors map to ``ConnectionError``, ``ValueError`` (422, 413, and
    409 when ``conflict_error`` is set) and ``RuntimeError``. Callers decide what the
    success body must contain.
    """
    base_url = api_url.rstrip("/") if api_url is not None else os.getenv("Q_API_URL", DEFAULT_API_URL).rstrip("/")
    target_url = f"{base_url}{path}"

    import httpx

    try:
        http_client = client if client is not None else httpx.Client()
        response = http_client.post(target_url, json=payload)
    except httpx.RequestError as exc:
        raise ConnectionError(
            f"Cannot connect to Research API at {base_url}. Start the stack with './dev research'."
        ) from exc

    if response.status_code in (422, 413) or (conflict_error and response.status_code == 409):
        try:
            detail = response.json().get("detail", response.text)
        except Exception:  # noqa: BLE001
            detail = response.text
        if response.status_code == 422:
            raise ValueError(f"Server rejected {noun} publish (422): {detail}")
        if response.status_code == 409:
            raise ValueError(f"{noun.capitalize()} already exists and replace=False (409): {detail}")
        raise ValueError(f"{noun.capitalize()} too large for Research API (413): {detail}")

    if response.is_error:
        raise RuntimeError(f"{noun.capitalize()} publish failed with status {response.status_code}: {response.text}")

    try:
        return response.json()
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(f"Unexpected response format from {endpoint} endpoint: {response.text}") from exc
