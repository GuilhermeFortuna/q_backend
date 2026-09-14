from unittest.mock import MagicMock

import fakeredis
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from q_backend.api.main import app
from q_backend.api.routers import stream_replay as stream_replay_router
from q_backend.storage.db.base import utc_now
from q_backend.storage.db.engine import get_engine
from q_backend.streaming.outbox import record_event, rotate_epoch
from q_backend.streaming.snapshot import StreamUnavailable
from tests.streaming.replay_schema import assert_valid_replay


@pytest.fixture(autouse=True)
def check_postgres_for_integration(request):
    if "integration" not in request.keywords:
        return
    try:
        engine = get_engine()
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
    except SQLAlchemyError:
        pytest.skip("Postgres is not available or misconfigured")


def _seed_history(session: Session, count: int = 10) -> str:
    session.execute(text("DELETE FROM stream_outbox WHERE topic = 'jobs.terminal'"))
    session.execute(text("UPDATE stream_outbox_topic_state SET last_seq = 0 WHERE topic = 'jobs.terminal'"))
    session.commit()

    epoch = session.execute(
        text("SELECT epoch FROM stream_outbox_topic_state WHERE topic = 'jobs.terminal'")
    ).scalar_one()
    for seq in range(1, count + 1):
        record_event(
            session,
            "jobs.terminal",
            {
                "job_id": f"job-{seq}",
                "kind": "backtest",
                "status": "completed",
                "finished_at": utc_now().isoformat(),
            },
            payload_schema="schema/stream/payloads/job-terminal.schema.json",
            producer_id=f"producer-{seq}",
        )
    session.commit()
    return epoch


@pytest.mark.integration
def test_history_route_success_expired_and_epoch_mismatch():
    engine = get_engine()
    session_factory = sessionmaker(bind=engine, expire_on_commit=False)

    with session_factory() as session:
        epoch = _seed_history(session, count=10)

    client = TestClient(app)
    ok = client.get(
        "/api/v1/stream/jobs.terminal/history",
        params={"epoch": epoch, "from_seq": 5, "limit": 3},
    )
    assert ok.status_code == 200
    body = ok.json()
    assert_valid_replay("history-page", body)
    assert [entry["seq"] for entry in body["entries"]] == [5, 6, 7]
    assert body["next_seq"] == 8

    with session_factory() as session:
        from datetime import timedelta

        from q_backend.streaming.outbox import prune_relayed

        session.execute(
            text("UPDATE stream_outbox_topic_state " "SET last_relayed_seq = 6 WHERE topic = 'jobs.terminal'")
        )
        old_time = utc_now() - timedelta(days=32)
        session.execute(
            text("UPDATE stream_outbox SET recorded_at = :old_time WHERE topic = 'jobs.terminal'"),
            {"old_time": old_time},
        )
        session.commit()
        with session_factory() as prune_session:
            prune_relayed(prune_session, older_than=timedelta(days=30))
            prune_session.commit()

    expired = client.get(
        "/api/v1/stream/jobs.terminal/history",
        params={"epoch": epoch, "from_seq": 3, "limit": 3},
    )
    assert expired.status_code == 410
    expired_body = expired.json()
    assert_valid_replay("history-expired", expired_body)
    assert expired_body["requested_from_seq"] == 3
    assert expired_body["oldest_available_seq"] == 7

    with session_factory() as session:
        rotate_epoch(session, "jobs.terminal", reason="route test")
        session.commit()
        current_epoch = session.execute(
            text("SELECT epoch FROM stream_outbox_topic_state WHERE topic = 'jobs.terminal'")
        ).scalar_one()

    mismatch = client.get(
        "/api/v1/stream/jobs.terminal/history",
        params={"epoch": epoch, "from_seq": 1, "limit": 3},
    )
    assert mismatch.status_code == 409
    mismatch_body = mismatch.json()
    assert mismatch_body == {
        "topic": "jobs.terminal",
        "requested_epoch": epoch,
        "current_epoch": current_epoch,
    }


@pytest.mark.integration
def test_history_ephemeral_topic_returns_400():
    client = TestClient(app)
    response = client.get(
        "/api/v1/stream/quotes/history",
        params={"epoch": "epoch-1", "from_seq": 1, "limit": 3},
    )
    assert response.status_code == 400
    assert response.json()["code"] == "invalid_topic"


@pytest.mark.integration
def test_history_works_while_redis_is_unavailable(monkeypatch):
    engine = get_engine()
    session_factory = sessionmaker(bind=engine, expire_on_commit=False)

    with session_factory() as session:
        epoch = _seed_history(session, count=3)

    broken = MagicMock()
    broken.ping.side_effect = StreamUnavailable()

    def _broken_redis():
        return broken

    monkeypatch.setattr(stream_replay_router, "get_stream_redis", _broken_redis)

    client = TestClient(app)
    response = client.get(
        "/api/v1/stream/jobs.terminal/history",
        params={"epoch": epoch, "from_seq": 1, "limit": 3},
    )
    assert response.status_code == 200
    assert len(response.json()["entries"]) == 3


def test_latest_route_and_stream_unavailable():
    import numpy as np

    from q_backend.streaming.market.arrow import ticks_to_ipc
    from q_backend.streaming.publisher import EphemeralPublisher

    fake = fakeredis.FakeRedis(decode_responses=False)
    app.dependency_overrides[stream_replay_router.get_latest_redis] = lambda: fake
    try:
        publisher = EphemeralPublisher(fake, "quotes", producer_id="route-test")
        publisher.publish(
            routing_key={"symbol": "WIN$N"},
            payload_kind="arrow_ipc",
            payload_schema="schema/api/arrow/ticks.schema.json",
            payload=ticks_to_ipc(
                {
                    "time_msc": np.arange(3, dtype=np.int64),
                    "bid": np.ones(3),
                    "ask": np.ones(3) + 0.1,
                    "last": np.ones(3),
                    "volume": np.ones(3),
                    "flags": np.ones(3, dtype=np.int32),
                }
            ),
        )

        client = TestClient(app)
        ok = client.get("/api/v1/stream/quotes/latest")
        assert ok.status_code == 200
        assert_valid_replay("latest", ok.json())

        broken = MagicMock()
        broken.ping.side_effect = StreamUnavailable()
        app.dependency_overrides[stream_replay_router.get_latest_redis] = lambda: broken

        unavailable = client.get("/api/v1/stream/quotes/latest")
        assert unavailable.status_code == 503
        assert unavailable.json()["code"] == "stream_unavailable"
    finally:
        app.dependency_overrides.clear()


def test_job_snapshot_route_validates(run_jobs_sync):
    fake = run_jobs_sync
    app.dependency_overrides[stream_replay_router.get_stream_redis] = lambda: fake

    client = TestClient(app)
    response = client.get("/api/v1/stream/jobs/snapshot")
    assert response.status_code == 200
    body = response.json()
    assert "jobs" in body
    assert_valid_replay("watermark", body["watermark"])
    app.dependency_overrides.clear()
