"""Dramatiq message context for Sentry worker events.

Q actors have retries disabled, so the SDK's Dramatiq integration observes only
final failures. The ``attempt`` tag remains explicit for diagnosis if an actor
later opts into retries. Message payloads are never attached by this middleware.
"""

from __future__ import annotations

import threading
from typing import Any

import sentry_sdk
from dramatiq.middleware import Middleware


_POSITIONAL_IDS: dict[str, tuple[str | None, ...]] = {
    "optimization_coordinator": ("study_id", None, None),
    "run_optimization_trials": ("study_id", None, None, None),
    "walkforward_coordinator": ("study_id", None, None),
    "run_walkforward_window": ("study_id", None, None, None),
    "discovery_coordinator": ("study_id", None, None),
    "evaluate_discovery_candidate": ("study_id", None, None, None),
    "evaluate_genetic_candidate": ("study_id", None, None, None, None),
    "run_backtest": ("backtest_id", None),
}
_MESSAGE_TAGS = frozenset(
    {"symbol", "strategy", "backtest_id", "study_id", "dataset", "worker_id"}
)


class SentryTradingContextMiddleware(Middleware):
    """Attach safe trading identifiers to the scope for one actor message."""

    def __init__(self, *, enabled: bool, worker_id: str) -> None:
        self.enabled = enabled
        self.worker_id = worker_id
        self._contexts: dict[str, Any] = {}
        self._lock = threading.Lock()

    def before_process_message(self, broker: Any, message: Any) -> None:
        del broker
        if not self.enabled:
            return

        manager = sentry_sdk.new_scope()
        scope = manager.__enter__()
        with self._lock:
            self._contexts[message.message_id] = manager
        scope.set_tag("actor", message.actor_name)
        scope.set_tag("worker_id", self.worker_id)
        scope.set_tag("attempt", int(message.options.get("retries", 0)) + 1)

        for key, value in message.kwargs.items():
            if key in _MESSAGE_TAGS and value is not None:
                scope.set_tag(key, value)
        positional_keys = _POSITIONAL_IDS.get(message.actor_name, ())
        for key, value in zip(positional_keys, message.args, strict=False):
            if key is not None and value is not None:
                scope.set_tag(key, value)

    def after_process_message(
        self,
        broker: Any,
        message: Any,
        *,
        result: Any = None,
        exception: BaseException | None = None,
    ) -> None:
        del broker, result, exception
        with self._lock:
            manager = self._contexts.pop(message.message_id, None)
        if manager is not None:
            manager.__exit__(None, None, None)

    def after_skip_message(self, broker: Any, message: Any) -> None:
        self.after_process_message(broker, message)
