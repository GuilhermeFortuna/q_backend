"""Launch the Dramatiq worker pool — the backend's single CPU budget.

Run alongside the API process:

    uv run worker          # or: python -m q_backend.cli.worker

The pool size comes from ``Q_WORKER_PROCESSES`` (default 14). Threads are pinned to
1 because the work is CPU-bound and GIL-bound; only separate processes give real
parallelism. Every heavy job (backtests, optimization trials, walk-forward windows,
discovery candidates) drains into this fixed pool, so concurrent jobs share the
budget instead of oversubscribing the machine.
"""

import logging
import os
import sys

from sentry_sdk.integrations.dramatiq import DramatiqIntegration

from q_backend.observability.sentry import init_sentry
from q_backend.storage.settings import get_settings

logger = logging.getLogger(__name__)


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    settings = get_settings()
    init_sentry(
        settings,
        component="worker",
        extra_integrations=[DramatiqIntegration()],
    )
    # Log the configured policy without importing torch / initializing CUDA here.
    # Device resolution happens inside neural jobs when they construct encoders.
    configured = os.environ.get("Q_TORCH_DEVICE", "cpu")
    logger.info("worker torch device policy Q_TORCH_DEVICE=%s", configured)
    # execv replaces this process; the marker makes the Dramatiq process repeat
    # initialization before broker construction, while API imports stay inert.
    os.environ["Q_DRAMATIQ_WORKER"] = "1"
    # Pin numeric libraries to one thread per worker process before the worker pool
    # boots; parallelism comes from the processes, not from BLAS/OpenMP threads.
    # (q_backend.tasks sets these too, but doing it here covers the master process
    # and any direct numpy import before the broker loads.)
    for var in (
        "OMP_NUM_THREADS",
        "OPENBLAS_NUM_THREADS",
        "MKL_NUM_THREADS",
        "NUMEXPR_NUM_THREADS",
        "NUMBA_NUM_THREADS",
    ):
        os.environ.setdefault(var, "1")

    processes = str(settings.worker_processes)
    argv = [
        sys.executable,
        "-m",
        "dramatiq",
        "q_backend.tasks",
        "--processes",
        processes,
        "--threads",
        "1",
    ]
    os.execv(sys.executable, argv)


if __name__ == "__main__":
    main()
