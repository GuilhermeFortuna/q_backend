import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from q_backend.storage.db.base import Base
from q_backend.storage.db import models  # noqa: F401
from q_backend.storage.db import execution_models  # noqa: F401
from q_backend.storage.db import outbox_models  # noqa: F401
from q_backend.storage.db.outbox_models import INITIAL_EPOCH

SEEDED_EPOCH = INITIAL_EPOCH


@pytest.fixture
def db_engine():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    yield engine
    Base.metadata.drop_all(engine)
    engine.dispose()


@pytest.fixture
def db_session(db_engine) -> Session:
    session_factory = sessionmaker(
        bind=db_engine,
        autoflush=False,
        autocommit=False,
        expire_on_commit=False,
    )
    session = session_factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


@pytest.fixture
def seeded_session(db_session: Session) -> Session:
    """Durable topic state is seeded when create_all builds the table."""
    return db_session
