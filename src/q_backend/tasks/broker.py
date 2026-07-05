"""Dramatiq broker configuration — the single CPU budget for all heavy work.

Importing this module sets the global Dramatiq broker (Redis-backed, reusing the
existing ``Q_REDIS_URL``). The worker is launched separately from the API process:

    dramatiq q_backend.tasks --processes $Q_WORKER_PROCESSES --threads 1

The worker pool is the *only* source of OS-level parallelism in the backend: every
heavy unit of work (a backtest, an Optuna trial batch, a walk-forward window, a
discovery candidate) is an actor message that drains into this fixed pool, so total
in-flight CPU work never exceeds the configured budget regardless of how many jobs
are launched concurrently. Threads are pinned to 1 because the work is CPU-bound and
GIL-bound — only separate processes give real parallelism.
"""

import logging
import os

import dramatiq
import sentry_sdk
from dramatiq.brokers.redis import RedisBroker
from dramatiq.middleware import Middleware
from sentry_sdk.integrations.dramatiq import DramatiqIntegration, SentryMiddleware

from q_backend.observability.sentry import init_sentry
from q_backend.observability.worker import SentryTradingContextMiddleware
from q_backend.storage.settings import get_settings
from q_backend.tasks.worker_context import shutdown_worker_market_data

logger = logging.getLogger(__name__)

# Generous default actor time limit (ms). A single walk-forward window or trial
# batch can run for a long time; the default 10 min is too tight. Actors may
# override per-actor.
DEFAULT_TIME_LIMIT_MS = 6 * 60 * 60 * 1000  # 6 hours

# Heavy jobs are not idempotent (they mutate DB/lake state); a crashed actor should
# not silently re-run the whole thing. Keep retries off by default — orphaned runs
# are reconciled to "cancelled" on API startup instead.
DEFAULT_MAX_RETRIES = 0


class MarketDataMiddleware(Middleware):
    """Close this worker process's MT5 connection on shutdown.

    The connection is opened lazily on first use (see
    ``worker_context.get_worker_market_data_service``) rather than eagerly at boot,
    so launching a large pool doesn't trigger N simultaneous MT5 handshakes — idle
    workers never connect, and a worker only connects once it runs a data-backed job.
    """

    def before_worker_shutdown(self, broker, worker):  # noqa: D401 - dramatiq hook
        shutdown_worker_market_data()
        sentry_sdk.flush(timeout=2)


def _build_broker() -> RedisBroker:
    settings = get_settings()
    worker_sentry_enabled = False
    if os.environ.get("Q_DRAMATIQ_WORKER") == "1":
        worker_sentry_enabled = init_sentry(
            settings,
            component="worker",
            extra_integrations=[DramatiqIntegration()],
        )
    redis_broker = RedisBroker(url=settings.redis_url)
    trading_context_middleware = SentryTradingContextMiddleware(
        enabled=worker_sentry_enabled,
        worker_id=settings.execution_worker_id,
    )
    if worker_sentry_enabled:
        # ``after_*`` hooks run in reverse. Placing our scope before Sentry's
        # middleware keeps tags alive until Sentry captures the failure.
        redis_broker.add_middleware(
            trading_context_middleware,
            before=SentryMiddleware,
        )
    else:
        redis_broker.add_middleware(trading_context_middleware)
    redis_broker.add_middleware(MarketDataMiddleware())
    return redis_broker


broker = _build_broker()
dramatiq.set_broker(broker)
