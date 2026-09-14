from __future__ import annotations

import argparse
import os
import socket
import sys

import uvicorn

from q_backend.observability.systemd import EX_CONFIG, notify_ready
from q_backend.storage.db.engine import get_engine
from q_backend.storage.db.migrations import alembic_ini_path, schema_revision
from q_backend.storage.settings import get_settings


class NotifyingServer(uvicorn.Server):
    """Uvicorn server that signals systemd readiness after sockets are bound and listening."""

    async def startup(self, sockets: list[socket.socket] | None = None) -> None:
        await super().startup(sockets=sockets)
        if self.started:
            notify_ready()


def main(argv: list[str] | None = None) -> int:
    """Check Postgres and schema head, then serve API without reload."""
    parser = argparse.ArgumentParser(description="Run the Q API production server.")
    parser.add_argument("--host", type=str, default=None, help="Host to bind (default: 0.0.0.0 or $HOST)")
    parser.add_argument("--port", type=int, default=None, help="Port to bind (default: 8000 or $PORT)")

    if argv is None:
        argv = sys.argv[1:]
    args = parser.parse_args(argv)

    engine = get_engine()
    ini_path = alembic_ini_path()
    rev = schema_revision(engine, ini_path)

    if rev.current != rev.head:
        sys.stderr.write(f"Database schema revision {rev.current} is behind head {rev.head}. Run migrations first.\n")
        return EX_CONFIG

    settings = get_settings()
    host = args.host or os.environ.get("HOST") or "0.0.0.0"
    port = args.port
    if port is None:
        port_env = os.environ.get("PORT")
        if port_env:
            port = int(port_env)
        else:
            port = settings.port

    config = uvicorn.Config(
        "q_backend.api.main:app",
        host=host,
        port=port,
        log_level="info",
    )
    server = NotifyingServer(config=config)
    server.run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
