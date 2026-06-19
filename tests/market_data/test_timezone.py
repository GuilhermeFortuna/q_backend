from datetime import datetime, timezone

import numpy as np

from q_backend.market_data import api_service as market_service
from q_backend.market_data.models import OHLCV
from q_backend.market_data.timezone import (
    mt5_datetime_to_utc_iso,
    to_brasilia_naive,
    unix_seconds_to_brasilia_naive,
    unix_seconds_to_utc_iso,
)


def _mt5_unix_for_broker_time(year: int, month: int, day: int, hour: int, minute: int = 0) -> int:
    """Build MT5-style unix where UTC wall clock equals broker-local time."""
    return int(datetime(year, month, day, hour, minute, tzinfo=timezone.utc).timestamp())


def test_unix_seconds_to_utc_iso():
    # MT5 unix for a 12:00 BRT bar encodes UTC wall clock 12:00:00
    seconds = _mt5_unix_for_broker_time(2024, 1, 1, 12, 0)
    assert unix_seconds_to_brasilia_naive(seconds) == datetime(2024, 1, 1, 12, 0, 0)
    assert unix_seconds_to_utc_iso(seconds) == "2024-01-01T15:00:00Z"


def test_unix_seconds_to_utc_iso_dec_2023_9am_session():
    seconds = _mt5_unix_for_broker_time(2023, 12, 10, 9, 0)
    assert unix_seconds_to_utc_iso(seconds) == "2023-12-10T12:00:00Z"


def test_mt5_datetime_to_utc_iso_from_naive_brasilia():
    naive = datetime(2024, 1, 1, 12, 0, 0)
    assert mt5_datetime_to_utc_iso(naive) == "2024-01-01T15:00:00Z"


def test_to_brasilia_naive_from_utc():
    aware_utc = datetime(2024, 1, 1, 15, 0, 0, tzinfo=timezone.utc)
    assert to_brasilia_naive(aware_utc) == datetime(2024, 1, 1, 12, 0, 0)


def test_ohlcv_to_bar_response_from_unix_row():
    row = np.array(
        (
            _mt5_unix_for_broker_time(2024, 1, 1, 12, 0),
            1.0,
            2.0,
            0.5,
            1.5,
            100,
            0,
            0,
        ),
        dtype=[
            ("time", "<i8"),
            ("open", "<f8"),
            ("high", "<f8"),
            ("low", "<f8"),
            ("close", "<f8"),
            ("tick_volume", "<u8"),
            ("spread", "<i4"),
            ("real_volume", "<u8"),
        ],
    )

    bar = market_service.ohlcv_to_bar_response(row)

    assert bar["timestamp"] == "2024-01-01T15:00:00Z"


def test_ohlcv_to_bar_response_from_ohlcv_model():
    bar = market_service.ohlcv_to_bar_response(
        OHLCV(
            time=datetime(2024, 1, 1, 12, 0, 0),
            open=1.0,
            high=2.0,
            low=0.5,
            close=1.5,
            tick_volume=100,
            real_volume=0,
        )
    )

    assert bar["timestamp"] == "2024-01-01T15:00:00Z"
