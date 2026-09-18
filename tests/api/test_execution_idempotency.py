"""Execution command idempotency protocol tests."""

from __future__ import annotations

import json
import uuid
from datetime import timedelta

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from q_backend.api.idempotency import IdempotentCommand, canonical_body_hash, prune_idempotency
from q_backend.api.main import app
from q_backend.storage.db import execution_models  # noqa: F401
from q_backend.storage.db import models  # noqa: F401
from q_backend.storage.db.base import Base, utc_now
from q_backend.storage.db.idempotency_models import CommandIdempotency
from q_backend.storage.db.execution_models import PaperAccount


@pytest.fixture
def idempotency_session() -> Session:
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine, expire_on_commit=False)()
    try:
        yield session
    finally:
        session.close()
        Base.metadata.drop_all(engine)
        engine.dispose()


def _command(key: uuid.UUID, *, body: dict | None = None, enforced: bool = True) -> IdempotentCommand:
    return IdempotentCommand(
        key=key,
        method="POST",
        path="/api/v1/execution/accounts",
        body=body or {"name": "desk"},
        enforced=enforced,
    )


def test_canonical_body_hash_is_order_independent():
    assert canonical_body_hash({"b": 2, "a": 1}) == canonical_body_hash({"a": 1, "b": 2})
    assert canonical_body_hash(None) == canonical_body_hash({})


def test_first_result_is_stored_and_replayed(idempotency_session: Session):
    key = uuid.uuid4()
    calls: list[int] = []
    command = _command(key)

    first = command.execute(idempotency_session, lambda: calls.append(1) or {"accepted": True})
    idempotency_session.commit()

    replay = _command(key).execute(idempotency_session, lambda: calls.append(2) or {"accepted": False})

    assert first == {"accepted": True}
    assert replay.status_code == 200
    assert json.loads(replay.body) == {"accepted": True}
    assert replay.headers["Idempotency-Replayed"] == "true"
    assert calls == [1]


def test_key_reuse_with_different_body_is_refused(idempotency_session: Session):
    key = uuid.uuid4()
    _command(key).execute(idempotency_session, lambda: {"accepted": True})
    idempotency_session.commit()

    response = _command(key, body={"name": "other"}).execute(
        idempotency_session,
        lambda: pytest.fail("reused key must not execute"),
    )

    assert response.status_code == 409
    assert json.loads(response.body)["code"] == "idempotency_key_reused"


def test_missing_key_is_enforced_or_logged(idempotency_session: Session, caplog: pytest.LogCaptureFixture):
    enforced = IdempotentCommand(
        key=None,
        method="PUT",
        path="/api/v1/execution/kill-switch",
        body={},
        enforced=True,
    )
    response = enforced.execute(idempotency_session, lambda: pytest.fail("missing key must be refused"))
    assert response.status_code == 400
    assert json.loads(response.body)["code"] == "idempotency_key_required"

    caplog.clear()
    accepted = IdempotentCommand(
        key=None,
        method="PUT",
        path="/api/v1/execution/kill-switch",
        body={},
        enforced=False,
    )
    assert accepted.execute(idempotency_session, lambda: {"accepted": True}) == {"accepted": True}
    assert "accepted without Idempotency-Key" in caplog.text


def test_business_refusal_is_stored_and_replayed(idempotency_session: Session):
    key = uuid.uuid4()
    first = _command(key).execute(
        idempotency_session,
        lambda: (_ for _ in ()).throw(HTTPException(status_code=409, detail="illegal transition")),
    )
    idempotency_session.commit()

    replay = _command(key).execute(
        idempotency_session,
        lambda: pytest.fail("stored refusal must not execute"),
    )

    assert first.status_code == replay.status_code == 409
    assert first.body == replay.body
    assert replay.headers["Idempotency-Replayed"] == "true"


def test_503_rolls_back_claim_and_allows_retry(idempotency_session: Session):
    key = uuid.uuid4()
    failed = _command(key).execute(
        idempotency_session,
        lambda: (_ for _ in ()).throw(HTTPException(status_code=503, detail="database unavailable")),
    )
    assert failed.status_code == 503
    assert idempotency_session.get(CommandIdempotency, key) is None

    retried = _command(key).execute(idempotency_session, lambda: {"accepted": True})
    idempotency_session.commit()
    assert retried == {"accepted": True}
    assert idempotency_session.get(CommandIdempotency, key).status_code == 200


def test_prune_removes_only_expired_results(idempotency_session: Session):
    old_key = uuid.uuid4()
    new_key = uuid.uuid4()
    now = utc_now()
    idempotency_session.add_all(
        [
            CommandIdempotency(
                key=old_key,
                method="POST",
                path="/old",
                body_sha256="a" * 64,
                status_code=200,
                response_body={},
                created_at=now - timedelta(hours=25),
            ),
            CommandIdempotency(
                key=new_key,
                method="POST",
                path="/new",
                body_sha256="b" * 64,
                status_code=200,
                response_body={},
                created_at=now - timedelta(hours=1),
            ),
        ]
    )
    idempotency_session.flush()

    assert prune_idempotency(idempotency_session) == 1
    assert idempotency_session.get(CommandIdempotency, old_key) is None
    assert idempotency_session.get(CommandIdempotency, new_key) is not None


def test_request_validation_failure_is_stored_and_replayed(
    idempotency_session: Session,
    monkeypatch: pytest.MonkeyPatch,
):
    from q_backend.api import idempotency as idempotency_module

    monkeypatch.setattr(
        idempotency_module,
        "create_session_factory",
        lambda: sessionmaker(bind=idempotency_session.get_bind(), expire_on_commit=False),
    )
    key = str(uuid.uuid4())
    client = TestClient(app)
    payload = {"initial_balance": "100"}  # missing the required account name
    first = client.post("/api/v1/execution/accounts", json=payload, headers={"Idempotency-Key": key})
    replay = client.post("/api/v1/execution/accounts", json=payload, headers={"Idempotency-Key": key})

    assert first.status_code == replay.status_code == 422
    assert first.json() == replay.json()
    row = idempotency_session.get(CommandIdempotency, uuid.UUID(key))
    assert row is not None and row.status_code == 422
    assert replay.headers["Idempotency-Replayed"] == "true"


def test_execution_route_replay_does_not_create_a_second_account(
    idempotency_session: Session,
    monkeypatch: pytest.MonkeyPatch,
):
    from q_backend.api import idempotency as idempotency_module

    monkeypatch.setattr(
        idempotency_module,
        "create_session_factory",
        lambda: sessionmaker(bind=idempotency_session.get_bind(), expire_on_commit=False),
    )
    key = str(uuid.uuid4())
    client = TestClient(app)
    payload = {"name": "desk", "initial_balance": "100"}

    first = client.post("/api/v1/execution/accounts", json=payload, headers={"Idempotency-Key": key})
    replay = client.post("/api/v1/execution/accounts", json=payload, headers={"Idempotency-Key": key})

    assert first.status_code == replay.status_code == 200
    assert first.json() == replay.json()
    assert replay.headers["Idempotency-Replayed"] == "true"
    assert idempotency_session.query(PaperAccount).count() == 1
