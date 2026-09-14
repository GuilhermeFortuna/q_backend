import random
import threading
import time
import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import sessionmaker

from q_backend.storage.db.models import BacktestConfig, BacktestRun
from q_backend.streaming.jobs import record_job_terminal
from q_backend.streaming.snapshot import read_job_snapshot


@pytest.fixture(autouse=True)
def check_postgres():
    try:
        from q_backend.storage.db.engine import get_engine

        engine = get_engine()
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
    except SQLAlchemyError:
        pytest.skip("Postgres is not available or misconfigured")


def _read_watermark_separate_transaction(session_factory):
    """Negative control: watermark read outside the snapshot transaction."""
    from q_backend.streaming.outbox import read_watermark

    with session_factory() as session:
        wm = read_watermark(session, ["jobs.terminal"])
        return wm["jobs.terminal"][1]


@pytest.mark.integration
def test_job_snapshot_race_is_consistent_over_two_hundred_trials():
    from q_backend.storage.db.engine import get_engine

    engine = get_engine()
    session_factory = sessionmaker(bind=engine, expire_on_commit=False)

    with session_factory() as session:
        cfg = BacktestConfig(name=f"race-{uuid.uuid4().hex[:8]}", config={"symbol": "TEST"})
        session.add(cfg)
        session.flush()
        run = BacktestRun(backtest_config_id=cfg.id, config={"symbol": "TEST"}, status="running")
        session.add(run)
        session.commit()
        job_id = str(run.id)

    import fakeredis

    fake_redis = fakeredis.FakeRedis(decode_responses=True)

    terminal_side = 0
    active_side = 0
    negative_control_failures = 0

    for _trial in range(200):
        event_seq_holder: list[int] = []

        def record_terminal() -> None:
            time.sleep(random.uniform(0, 0.005))
            with session_factory() as session:
                recorded = record_job_terminal(session, "backtest", job_id, "completed")
                session.commit()
                if recorded:
                    event = session.execute(
                        text(
                            "SELECT seq FROM stream_outbox "
                            "WHERE topic = 'jobs.terminal' AND routing_key->>'job_id' = :jid "
                            "ORDER BY seq DESC LIMIT 1"
                        ),
                        {"jid": job_id},
                    ).scalar_one()
                    event_seq_holder.append(int(event))

        thread = threading.Thread(target=record_terminal)
        thread.start()
        snapshot = read_job_snapshot(session_factory, fake_redis)
        thread.join()

        assert event_seq_holder, "terminal event should be recorded each trial"
        event_seq = event_seq_holder[0]
        item = next((job for job in snapshot.jobs if job.job_id == job_id), None)
        watermark_seq = snapshot.watermark["jobs.terminal"]["seq"]

        if item is not None and item.status == "completed":
            assert watermark_seq >= event_seq
            terminal_side += 1
        else:
            assert watermark_seq < event_seq
            active_side += 1

        separate_wm = _read_watermark_separate_transaction(session_factory)
        if separate_wm >= event_seq and (item is None or item.status != "completed"):
            negative_control_failures += 1

        with session_factory() as session:
            session.execute(
                text("DELETE FROM stream_job_terminal_markers WHERE kind = 'backtest' AND job_id = :jid"),
                {"jid": job_id},
            )
            session.execute(
                text("DELETE FROM stream_outbox WHERE topic = 'jobs.terminal' AND routing_key->>'job_id' = :jid"),
                {"jid": job_id},
            )
            session.commit()

    assert terminal_side + active_side == 200
    assert negative_control_failures > 0
