"""Background task layer.

This package is the entry point for the Dramatiq worker:

    dramatiq q_backend.tasks --processes $Q_WORKER_PROCESSES --threads 1

Importing it configures the global broker and registers every actor.
"""

import os as _os

# Pin the numeric libraries to a SINGLE thread per worker process. This must run
# before numpy/pyarrow are imported (the broker import below pulls them in). The
# pool's parallelism comes from the worker *processes*, not from BLAS/OpenMP threads
# — without this each of the N workers spawns a thread pool sized to the CPU core
# count, and N×cores threads exhausts memory (OpenBLAS allocation failures, and
# pyarrow DLL-load failures from an exhausted Windows paging file). `setdefault`
# leaves any explicit override from the environment untouched.
for _var in (
    "OMP_NUM_THREADS",
    "OPENBLAS_NUM_THREADS",
    "MKL_NUM_THREADS",
    "NUMEXPR_NUM_THREADS",
    "NUMBA_NUM_THREADS",
):
    _os.environ.setdefault(_var, "1")

from q_backend.tasks.broker import broker  # noqa: E402,F401  (sets the global broker)

# Actor modules are imported for their @dramatiq.actor side effects (registration).
from q_backend.tasks import actors  # noqa: E402,F401

__all__ = ["broker", "actors"]
