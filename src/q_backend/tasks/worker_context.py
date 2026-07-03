"""Per-process worker state for the Dramatiq pool.

Each Dramatiq worker process owns its own MetaTrader 5 connection. The connection
is opened once when the process boots (see ``broker.MarketDataMiddleware``) and
reused by every actor that runs in that process, so parallel workers don't each
re-handshake the terminal on every job. Market data fetched through the service
should be cached in the data lake by callers to keep terminal load low.
"""

import logging
import threading
from typing import Optional

from q_backend.market_data.service import MarketDataService

logger = logging.getLogger(__name__)

_service: Optional[MarketDataService] = None
_lock = threading.Lock()


def init_worker_market_data() -> None:
    """Open this process's MT5 connection. Best-effort: failures are logged, not raised,
    so a worker still boots and can serve cache-backed work when MT5 is unavailable."""
    global _service
    with _lock:
        if _service is None:
            _service = MarketDataService()
        try:
            if not _service.initialize():
                logger.error("MT5 initialization returned False on worker boot")
        except Exception:
            logger.exception("MT5 initialization raised on worker boot")


def get_worker_market_data_service() -> MarketDataService:
    """Return this process's MarketDataService, initializing it lazily if needed."""
    if _service is None:
        init_worker_market_data()
    assert _service is not None
    return _service


def shutdown_worker_market_data() -> None:
    global _service
    with _lock:
        if _service is not None:
            try:
                _service.shutdown()
            except Exception:  # noqa: BLE001 - best-effort MT5 shutdown; logged
                logger.debug("MT5 shutdown raised on worker shutdown", exc_info=True)
            _service = None
