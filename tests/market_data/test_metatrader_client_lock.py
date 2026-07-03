"""The native client's lock must be reentrant.

Locked fetch paths call _ensure_connected -> connect, which takes the lock
again; with a plain threading.Lock the thread deadlocks itself while holding
the lock, and every other API request then queues behind it forever.
"""

from __future__ import annotations

import threading
from types import SimpleNamespace

from q_backend.market_data.clients import metatrader
from q_backend.market_data.clients.metatrader import MetaTraderClient


def test_locked_fetch_can_reenter_for_reconnect(monkeypatch):
    monkeypatch.setattr(
        metatrader,
        "mt5",
        SimpleNamespace(initialize=lambda **kwargs: True, last_error=lambda: (0, "")),
    )
    client = MetaTraderClient()

    def _fetch_needing_reconnect():
        # Mirrors _fetch: runs under the lock, then reconnects (lock again).
        client._ensure_connected()
        return "ok"

    result: list[str] = []

    def _run():
        result.append(client._run_locked(_fetch_needing_reconnect))

    worker = threading.Thread(target=_run, daemon=True)
    worker.start()
    worker.join(timeout=5)

    assert not worker.is_alive(), "reentrant lock acquisition deadlocked"
    assert result == ["ok"]
    assert client._is_initialized
