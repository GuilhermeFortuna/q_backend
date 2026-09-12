import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from q_backend.storage.db.base import Base
from q_backend.storage.db import models  # noqa: F401
from q_backend.storage.db import execution_models  # noqa: F401
from q_backend.storage.db import outbox_models  # noqa: F401
from q_backend.storage.db.outbox_models import OutboxTopicState
from q_contracts.topics import TOPICS

SEEDED_EPOCH = "20260912-00000001"


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
    for name, policy in TOPICS.items():
        if policy.topic_class == "durable":
            db_session.add(
                OutboxTopicState(
                    topic=name,
                    epoch=SEEDED_EPOCH,
                    last_seq=0,
                    last_relayed_seq=0,
                )
            )
    db_session.commit()
    return db_session
