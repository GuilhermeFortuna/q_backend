from __future__ import annotations

import os
import socket
from typing import Final

EX_CONFIG: Final[int] = 78  # sysexits.h; excluded from restart in units
EX_FAILED_CLOSED: Final[int] = 79  # execution recovery failed closed; excluded from restart in units


def notify(state: str) -> bool:
    """Send one datagram to $NOTIFY_SOCKET ('@' prefix = abstract). False, and no error, when unset."""
    socket_path = os.environ.get("NOTIFY_SOCKET")
    if not socket_path:
        return False

    if socket_path.startswith("@"):
        socket_path = "\0" + socket_path[1:]

    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as sock:
            sock.sendto(state.encode("utf-8"), socket_path)
        return True
    except OSError:
        return False


def notify_ready() -> bool:
    """Send READY=1 notification."""
    return notify("READY=1")


def notify_status(text: str) -> bool:
    """Send STATUS=<text> notification shown by systemctl status."""
    return notify(f"STATUS={text}")


def notify_watchdog() -> bool:
    """Send WATCHDOG=1 to feed the systemd watchdog."""
    return notify("WATCHDOG=1")
