"""Launch the Dramatiq worker pool — the backend's single CPU budget.

Run alongside the API process:

    uv run worker          # or: python -m q_backend.cli.worker

The pool size comes from ``Q_WORKER_PROCESSES`` (default 14). Threads are pinned to
1 because the work is CPU-bound and GIL-bound; only separate processes give real
parallelism. Every heavy job (backtests, optimization trials, walk-forward windows,
discovery candidates) drains into this fixed pool, so concurrent jobs share the
budget instead of oversubscribing the machine.
"""

import os
import sys

from q_backend.storage.settings import get_settings


def main() -> None:
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

    processes = str(get_settings().worker_processes)
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
