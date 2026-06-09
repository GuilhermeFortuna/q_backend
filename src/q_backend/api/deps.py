from collections.abc import Generator

from sqlalchemy.orm import Session

from q_backend.storage.db.engine import create_session_factory


def get_session() -> Generator[Session, None, None]:
    session = create_session_factory()()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
