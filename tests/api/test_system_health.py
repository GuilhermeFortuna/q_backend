from unittest.mock import patch

from q_backend.api.main import get_system_health, market_data_service

LEGACY_HEALTH_FIELDS = ("status", "backendVersion", "dataLakeStatus", "lastSyncAt")


def test_system_health_backward_compatible_fields():
    with patch(
        "q_backend.api.main.storage_status",
        return_value={
            "postgres": {"status": "ok"},
            "redis": {"status": "ok"},
        },
    ):
        body = get_system_health()

    for field in LEGACY_HEALTH_FIELDS:
        assert field in body


def test_system_health_includes_storage_status():
    with patch(
        "q_backend.api.main.storage_status",
        return_value={
            "postgres": {"status": "ok"},
            "redis": {"status": "error", "error": "unavailable"},
        },
    ):
        body = get_system_health()

    assert body["storageStatus"]["postgres"]["status"] == "ok"
    assert body["storageStatus"]["redis"]["status"] == "error"
    assert body["storageStatus"]["redis"]["error"] == "unavailable"


def test_storage_failure_does_not_change_top_level_health():
    with patch.object(market_data_service, "mt5_connected", return_value=True):
        try:
            with patch(
                "q_backend.api.main.storage_status",
                return_value={
                    "postgres": {"status": "error", "error": "db down"},
                    "redis": {"status": "error", "error": "redis down"},
                },
            ):
                body = get_system_health()

            assert body["status"] == "healthy"
            assert body["dataLakeStatus"] == "online"
        finally:
            pass
