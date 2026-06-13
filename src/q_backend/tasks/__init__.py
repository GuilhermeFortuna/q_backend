"""Background task layer.

This package is the entry point for the Dramatiq worker:

    dramatiq q_backend.tasks --processes $Q_WORKER_PROCESSES --threads 1

Importing it configures the global broker and registers every actor.
"""

from q_backend.tasks.broker import broker  # noqa: F401  (sets the global broker)

# Actor modules are imported for their @dramatiq.actor side effects (registration).
from q_backend.tasks import actors  # noqa: F401,E402

__all__ = ["broker", "actors"]
