from collections.abc import Generator

from fastapi import Request
from sqlalchemy.orm import Session

from q_backend.storage.db.engine import create_session_factory


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
