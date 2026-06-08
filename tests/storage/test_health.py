from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy.exc import SQLAlchemyError

from q_backend.storage.health import check_postgres, check_redis, storage_status


def test_check_postgres_ok():
    conn = MagicMock()
    engine = MagicMock()
    engine.connect.return_value.__enter__.return_value = conn

    with patch("q_backend.storage.health.get_engine", return_value=engine):
        result = check_postgres()

    assert result == {"status": "ok"}
    conn.execute.assert_called_once()


def test_check_postgres_error():
    engine = MagicMock()
    engine.connect.side_effect = SQLAlchemyError("connection refused")

    with patch("q_backend.storage.health.get_engine", return_value=engine):
        result = check_postgres()

    assert result["status"] == "error"
    assert "connection refused" in result["error"]


def test_check_redis_ok():
    client = MagicMock()
    client.ping.return_value = True

    with patch("q_backend.storage.health.get_redis", return_value=client):
        result = check_redis()

    assert result == {"status": "ok"}
    client.ping.assert_called_once()


def test_check_redis_error():
    client = MagicMock()
    client.ping.side_effect = ConnectionError("redis down")

    with patch("q_backend.storage.health.get_redis", return_value=client):
        result = check_redis()

    assert result["status"] == "error"
    assert "redis down" in result["error"]


def test_storage_status_returns_both_services():
    with (
        patch("q_backend.storage.health.check_postgres", return_value={"status": "ok"}),
        patch("q_backend.storage.health.check_redis", return_value={"status": "error", "error": "x"}),
    ):
        result = storage_status()

    assert set(result.keys()) == {"postgres", "redis"}
    assert result["postgres"]["status"] == "ok"
    assert result["redis"]["status"] == "error"
