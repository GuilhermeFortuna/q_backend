"""API package without application startup during schema imports.

Workers import API schemas too. Load the ASGI application only when callers
explicitly request it, so schema imports cannot re-enter partially loaded jobs.
"""

from typing import Any

__all__ = ["app"]


def __getattr__(name: str) -> Any:
    if name == "app":
        from q_backend.api.main import app

        return app
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
