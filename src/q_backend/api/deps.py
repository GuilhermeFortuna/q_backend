import os
from collections.abc import Generator
from functools import lru_cache

from fastapi import HTTPException, Request
from sqlalchemy.orm import Session

from q_backend.storage.db.engine import create_session_factory
from q_backend.time import MarketClock, MarketClockConfigError, market_clock_from_env


@lru_cache(maxsize=1)
def get_market_clock() -> MarketClock:
    """One configured market-time source for control-plane activation cutoffs."""
    try:
        return market_clock_from_env(os.environ)
    except MarketClockConfigError as exc:
        raise HTTPException(status_code=503, detail="invalid market clock configuration") from exc


def get_session(request: Request) -> Generator[Session, None, None]:
    managed_session = getattr(request.state, "idempotency_session", None)
    if managed_session is not None:
        yield managed_session
        return
    session = create_session_factory()()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
