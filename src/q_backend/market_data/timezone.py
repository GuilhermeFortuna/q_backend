from datetime import datetime, timezone
from zoneinfo import ZoneInfo

# B3 / MetaTrader broker timestamps are interpreted in Brasília local time.
BRASILIA_TZ = ZoneInfo("America/Sao_Paulo")


def to_brasilia_naive(dt: datetime) -> datetime:
    """Convert an aware datetime to naive Brasília local for MT5 queries."""
    if dt.tzinfo is None:
        return dt
    return dt.astimezone(BRASILIA_TZ).replace(tzinfo=None)


def unix_seconds_to_brasilia_naive(seconds: int) -> datetime:
    """
    Convert an MT5 unix epoch to naive Brasília wall clock.

    B3 MetaTrader stores bar/tick open times so the UTC wall clock of the unix
    timestamp matches broker-local time (e.g. a 09:00 BRT bar has epoch seconds
    whose UTC components read 09:00:00, not 12:00:00).
    """
    utc_wall = datetime.fromtimestamp(seconds, tz=timezone.utc)
    return datetime(
        utc_wall.year,
        utc_wall.month,
        utc_wall.day,
        utc_wall.hour,
        utc_wall.minute,
        utc_wall.second,
        utc_wall.microsecond,
    )


def unix_seconds_to_utc_iso(seconds: int) -> str:
    """Serialize an MT5 unix epoch as a proper UTC ISO-8601 instant."""
    return mt5_datetime_to_utc_iso(unix_seconds_to_brasilia_naive(seconds))


def mt5_datetime_to_utc_iso(dt: datetime) -> str:
    """
    Serialize a naive Brasília broker datetime as UTC ISO-8601 for API clients.
    """
    if dt.tzinfo is not None:
        dt = dt.astimezone(BRASILIA_TZ).replace(tzinfo=None)
    return dt.replace(tzinfo=BRASILIA_TZ).astimezone(timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
