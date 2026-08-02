"""Privacy-safe Sentry setup for Q backend processes.

Telemetry is dormant unless a DSN is explicitly configured. Request bodies are
disabled at the SDK level, default PII is never sent, and strategy-builder free
text is redacted as a second line of defense.
"""

from __future__ import annotations

import logging
import os
import platform
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from typing import Any

import sentry_sdk
from sentry_sdk.integrations.fastapi import FastApiIntegration
from sentry_sdk.integrations.sqlalchemy import SqlalchemyIntegration
from sentry_sdk.integrations.starlette import StarletteIntegration

from q_backend.storage.settings import Settings

logger = logging.getLogger(__name__)

_REDACTED = "[redacted]"
_SENSITIVE_FIELDS = frozenset(
    {"prompt", "message", "conversation", "description", "content"}
)
_TRADING_TAGS = frozenset(
    {"symbol", "strategy", "backtest_id", "study_id", "dataset", "worker_id"}
)
_COMPONENTS = frozenset({"api", "worker", "cli"})
_initialized = False


def _redact_sensitive_fields(value: object) -> None:
    if isinstance(value, dict):
        for key, nested in value.items():
            if str(key).lower() in _SENSITIVE_FIELDS:
                value[key] = _REDACTED
            else:
                _redact_sensitive_fields(nested)
    elif isinstance(value, list):
        for item in value:
            _redact_sensitive_fields(item)


def _scrub_event(event: dict[str, Any], hint: dict[str, Any]) -> dict[str, Any]:
    """Redact strategy-builder free text if a request payload is ever present."""
    del hint
    request = event.get("request")
    if not isinstance(request, dict):
        return event
    url = str(request.get("url", ""))
    if "/strategy-builder/" not in url:
        return event
    data = request.get("data")
    if isinstance(data, dict):
        _redact_sensitive_fields(data)
    return event


_resolved_release: str | None = None


def get_git_sha() -> str:
    """Resolve the git SHA, fallback to 'unknown'."""
    try:
        import subprocess
        res = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=2.0,
        )
        if res.returncode == 0:
            sha = res.stdout.strip()
            if sha:
                return sha
    except Exception:  # noqa: BLE001, S110 - git resolution is best-effort fallback
        pass
    return "unknown"


def resolve_release(*, use_cache: bool = True) -> str:
    """Resolve the release tag once at process start, formatted as q@<sha>."""
    global _resolved_release
    if use_cache and _resolved_release is not None:
        return _resolved_release

    release = os.environ.get("Q_RELEASE")
    if release:
        val = release if release.startswith("q@") else f"q@{release}"
    else:
        sha = get_git_sha()
        val = f"q@{sha}"

    if use_cache:
        _resolved_release = val
    return val


RELEASE = resolve_release()


def init_sentry(
    settings: Settings,
    *,
    component: str,
    extra_integrations: Iterable[object] = (),
) -> bool:
    """Initialize Sentry once; production trigger is process/app startup."""
    global _initialized

    if component not in _COMPONENTS:
        raise ValueError(f"Unsupported Sentry component: {component}")
    if not settings.sentry_dsn:
        logger.info("sentry: disabled (no DSN)")
        return False
    if _initialized:
        return True

    try:
        sentry_sdk.init(
            dsn=settings.sentry_dsn,
            environment=settings.sentry_environment,
            release=RELEASE,
            integrations=[
                FastApiIntegration(),
                StarletteIntegration(),
                SqlalchemyIntegration(),
                *extra_integrations,
            ],
            send_default_pii=False,
            max_request_body_size="never",
            traces_sample_rate=settings.sentry_traces_sample_rate,
            profiles_sample_rate=settings.sentry_profiles_sample_rate,
            before_send=_scrub_event,
        )
        sentry_sdk.set_tag("component", component)
        sentry_sdk.set_tag("python_version", platform.python_version())
        sentry_sdk.set_tag("os", platform.system())
        _initialized = True
        logger.info(
            "sentry: enabled env=%s traces=%s",
            settings.sentry_environment,
            settings.sentry_traces_sample_rate,
        )
        return True
    except Exception:  # noqa: BLE001 - observability must never break the process
        logger.warning(
            "Sentry initialization failed; continuing without telemetry",
            exc_info=True,
        )
        return False


@contextmanager
def trading_context(**tags: object) -> Iterator[None]:
    """Push a scope containing only approved trading identifiers and symbols."""
    unknown = set(tags) - _TRADING_TAGS
    if unknown:
        names = ", ".join(sorted(unknown))
        raise ValueError(f"Unsupported trading context tag(s): {names}")

    with sentry_sdk.new_scope() as scope:
        for key, value in tags.items():
            if value is not None:
                scope.set_tag(key, value)
        yield
