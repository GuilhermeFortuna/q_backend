"""Tests for q_backend.research Research facade and frame contract."""

from __future__ import annotations

import shutil
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest

from q_backend.market_data.catalog.service import LakeCatalog
from q_backend.market_data.timezone import BRASILIA_TZ
from q_backend.research import NoMarketDataError, Research
from q_backend.research.frame import (
    drop_forming_bar,
    empty_inventory_frame,
    is_bar_complete,
    normalize_timeframe,
    parse_bounds,
    validate_bars_frame,
)
from q_backend.research.providers import ResearchResources


def _research(resources: ResearchResources, **kwargs) -> Research:
    return Research(_resources=resources, **kwargs)


def _synthetic_m15_frame() -> pd.DataFrame:
    idx = pd.DatetimeIndex(
        [
            "2018-10-20T09:00:00",
            "2018-10-20T09:15:00",
            "2018-10-20T09:30:00",
        ],
        tz=BRASILIA_TZ,
        name="time",
    )
    return pd.DataFrame(
        {
            "open": [1.0, 2.0, 3.0],
            "high": [1.1, 2.1, 3.1],
            "low": [0.9, 1.9, 2.9],
            "close": [1.05, 2.05, 3.05],
            "tick_volume": [10, 11, 12],
            "spread": [1.0, 1.0, 1.0],
            "real_volume": [100.0, 100.0, 100.0],
        },
        index=idx,
    )


def test_catalog_fixture_schema_and_metadata(research_resources: ResearchResources) -> None:
    frozen = datetime(2026, 1, 15, 12, 0, tzinfo=BRASILIA_TZ)
    research = _research(research_resources, source="local", clock=lambda: frozen)
    frame = research.bars(
        "PETR4",
        timeframe="d1",
        start="2024-12-15",
        end="2025-01-15",
    )
    assert frame.index.name == "time"
    assert str(frame.index.tz) == "America/Sao_Paulo"
    assert list(frame.columns) == [
        "open",
        "high",
        "low",
        "close",
        "tick_volume",
        "spread",
        "real_volume",
    ]
    assert frame["open"].dtype == "float64"
    assert frame["tick_volume"].dtype == "int64"
    meta = frame.attrs["q_research"]
    assert meta["symbol"] == "PETR4"
    assert meta["timeframe"] == "D1"
    assert meta["source"] == "local"
    assert meta["timezone"] == "America/Sao_Paulo"
    assert meta["dataset_id"] is not None
    assert len(frame) == 10


def test_utc_and_brasilia_bounds_select_same_rows(research_resources: ResearchResources) -> None:
    frozen = datetime(2026, 1, 15, tzinfo=BRASILIA_TZ)
    research = _research(research_resources, source="local", clock=lambda: frozen)
    start_local = "2018-10-20T09:00:00-03:00"
    end_local = "2018-10-20T10:30:00-03:00"
    start_utc = "2018-10-20T12:00:00Z"
    end_utc = "2018-10-20T13:30:00Z"
    with patch(
        "q_backend.research.data.read_local_bars",
        side_effect=lambda *args, **kwargs: (_synthetic_m15_frame(), "dataset-test"),
    ):
        local_bounds = research.bars("WIN$N", timeframe="M15", start=start_local, end=end_local)
        utc_bounds = research.bars("WIN$N", timeframe="M15", start=start_utc, end=end_utc)
    pd.testing.assert_frame_equal(local_bounds, utc_bounds)


def test_date_only_end_is_midnight_brasilia(research_resources: ResearchResources) -> None:
    frozen = datetime(2026, 1, 15, tzinfo=BRASILIA_TZ)
    research = _research(research_resources, source="local", clock=lambda: frozen)
    frame = research.bars("PETR4", timeframe="D1", start="2024-12-16", end="2024-12-17")
    # Date-only end is midnight on 2024-12-17, so the 10:00 bar that day is excluded.
    assert frame.index[-1] == pd.Timestamp("2024-12-16T10:00:00", tz=BRASILIA_TZ)


def test_empty_bars_raises_no_market_data(research_resources: ResearchResources) -> None:
    frozen = datetime(2026, 1, 15, tzinfo=BRASILIA_TZ)
    research = _research(research_resources, source="local", clock=lambda: frozen)
    with pytest.raises(NoMarketDataError):
        research.bars("UNKNOWN", timeframe="D1", start="2020-01-01", end="2020-01-02")


def test_typed_empty_inventory(tmp_path: Path) -> None:
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from q_backend.storage.db.base import Base

    empty_root = tmp_path / "empty_lake"
    empty_root.mkdir()
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    sm = sessionmaker(bind=engine)
    empty_catalog = LakeCatalog(session_factory=sm, root=empty_root)
    empty = Research(
        _resources=ResearchResources(
            database_url="sqlite:///:memory:",
            market_data_root=empty_root,
            gateway_url="http://unused",
            gateway_token=None,
            session_factory=sm,
            catalog=empty_catalog,
        )
    )
    inv = empty.inventory()
    assert list(inv.columns) == list(empty_inventory_frame().columns)
    assert len(inv) == 0


def test_inventory_lists_published_bars(research_resources: ResearchResources) -> None:
    research = _research(research_resources, source="local")
    inv = research.inventory()
    assert "PETR4" in set(inv["symbol"])
    assert "D1" in set(inv.loc[inv["symbol"] == "PETR4", "timeframe"])
    assert inv["dataset_id"].notna().all()


def test_invalid_prices_and_duplicates_raise() -> None:
    idx = pd.DatetimeIndex(["2024-01-01"], tz=BRASILIA_TZ, name="time")
    bad_ohlc = pd.DataFrame(
        {
            "open": [10.0],
            "high": [9.0],
            "low": [8.0],
            "close": [9.5],
            "tick_volume": [1],
            "spread": [1.0],
            "real_volume": [1.0],
        },
        index=idx,
    )
    with pytest.raises(ValueError, match="ordering"):
        validate_bars_frame(bad_ohlc)

    dup_idx = pd.DatetimeIndex(["2024-01-01", "2024-01-01"], tz=BRASILIA_TZ, name="time")
    dup = pd.DataFrame(
        {
            "open": [10.0, 10.0],
            "high": [11.0, 11.0],
            "low": [9.0, 9.0],
            "close": [10.5, 10.5],
            "tick_volume": [1, 2],
            "spread": [1.0, 1.0],
            "real_volume": [1.0, 1.0],
        },
        index=dup_idx,
    )
    with pytest.raises(ValueError, match="Duplicate"):
        validate_bars_frame(dup)


def test_close_is_idempotent_and_blocks_calls(research_resources: ResearchResources) -> None:
    research = _research(research_resources, source="local")
    research.close()
    research.close()
    with pytest.raises(RuntimeError, match="closed"):
        research.bars("PETR4", timeframe="D1", start="2024-12-16", end="2024-12-17")


def test_drop_forming_m5_bar() -> None:
    open_time = datetime(2025, 6, 2, 10, 0, tzinfo=BRASILIA_TZ)
    idx = pd.date_range(open_time, periods=3, freq="5min", tz=BRASILIA_TZ, name="time")
    frame = pd.DataFrame({"close": [1.0, 2.0, 3.0]}, index=idx)
    now = open_time + timedelta(minutes=12)
    trimmed = drop_forming_bar(frame, "M5", now=now)
    assert len(trimmed) == 2


def test_mn1_calendar_forming_bar() -> None:
    jan_open = datetime(2025, 1, 1, 0, 0, tzinfo=BRASILIA_TZ)
    assert not is_bar_complete(jan_open, "MN1", now=datetime(2025, 1, 15, tzinfo=BRASILIA_TZ))
    assert is_bar_complete(jan_open, "MN1", now=datetime(2025, 2, 1, tzinfo=BRASILIA_TZ))


def test_auto_selects_local_when_envelope_covers(research_resources: ResearchResources) -> None:
    frozen = datetime(2026, 1, 15, tzinfo=BRASILIA_TZ)
    research = _research(research_resources, source="auto", clock=lambda: frozen)
    with patch("q_backend.research.data.read_remote_bars") as remote:
        frame = research.bars(
            "PETR4",
            timeframe="D1",
            start="2024-12-16T10:00:00",
            end="2024-12-18T10:00:00",
        )
        remote.assert_not_called()
    assert frame.attrs["q_research"]["source"] == "local"


def _synthetic_frame_at(start: str) -> pd.DataFrame:
    idx = pd.DatetimeIndex([start], tz=BRASILIA_TZ, name="time")
    return pd.DataFrame(
        {
            "open": [1.0],
            "high": [1.1],
            "low": [0.9],
            "close": [1.05],
            "tick_volume": [10],
            "spread": [1.0],
            "real_volume": [100.0],
        },
        index=idx,
    )


def test_auto_fetches_remote_when_not_covered(research_resources: ResearchResources) -> None:
    resources = ResearchResources(
        database_url="sqlite:///:memory:",
        market_data_root=research_resources.market_data_root,
        gateway_url="http://gateway.test",
        gateway_token=None,
        session_factory=research_resources._session_factory,
        catalog=research_resources.catalog(),
    )
    frozen = datetime(2099, 2, 1, tzinfo=BRASILIA_TZ)
    research = _research(resources, source="auto", clock=lambda: frozen)
    with patch(
        "q_backend.research.data.read_remote_bars",
        return_value=_synthetic_frame_at("2099-01-01T00:00:00"),
    ) as remote:
        frame = research.bars("PETR4", timeframe="D1", start="2099-01-01", end="2099-01-02")
        remote.assert_called_once()
    assert frame.attrs["q_research"]["source"] == "remote"


def test_auto_without_gateway_raises_coverage_error(research_resources: ResearchResources) -> None:
    frozen = datetime(2026, 1, 15, tzinfo=BRASILIA_TZ)
    research = _research(research_resources, source="auto", clock=lambda: frozen)
    with pytest.raises(ValueError, match="gateway"):
        research.bars("PETR4", timeframe="D1", start="2099-01-01", end="2099-01-02")


def test_remote_never_touches_catalog_db() -> None:
    catalog_mock = MagicMock()
    session_mock = MagicMock()
    resources = ResearchResources(
        database_url="sqlite:///:memory:",
        market_data_root=Path("/tmp/unused"),
        gateway_url="http://gateway.test",
        gateway_token="secret",
        session_factory=session_mock,
        catalog=catalog_mock,
    )
    frozen = datetime(2026, 1, 15, tzinfo=BRASILIA_TZ)
    research = _research(resources, source="remote", clock=lambda: frozen)
    with patch(
        "q_backend.research.data.read_remote_bars",
        return_value=_synthetic_m15_frame(),
    ):
        research.bars("WIN$", timeframe="M15", start="2018-10-20T09:00:00", end="2018-10-20T09:30:00")
    catalog_mock.assert_not_called()
    session_mock.assert_not_called()


def test_local_never_calls_remote(research_resources: ResearchResources) -> None:
    frozen = datetime(2026, 1, 15, tzinfo=BRASILIA_TZ)
    research = _research(research_resources, source="local", clock=lambda: frozen)
    with patch("q_backend.research.data.read_remote_bars") as remote:
        research.bars("PETR4", timeframe="D1", start="2024-12-16", end="2024-12-17")
        remote.assert_not_called()


def test_two_instances_isolated_roots(tmp_path: Path, lake_catalog) -> None:
    lake_a, catalog_a, sm_a = lake_catalog
    lake_b = tmp_path / "lake_b"
    shutil.copytree(Path(__file__).resolve().parents[1] / "fixtures/lake", lake_b)
    catalog_b = LakeCatalog(session_factory=sm_a, root=lake_b)
    catalog_b.adopt()
    res_a = ResearchResources(
        database_url="sqlite:///:memory:",
        market_data_root=lake_a,
        gateway_url=None,
        gateway_token=None,
        session_factory=sm_a,
        catalog=catalog_a,
    )
    res_b = ResearchResources(
        database_url="sqlite:///:memory:",
        market_data_root=lake_b,
        gateway_url=None,
        gateway_token=None,
        session_factory=sm_a,
        catalog=catalog_b,
    )
    frozen = datetime(2026, 1, 15, tzinfo=BRASILIA_TZ)
    a = _research(res_a, source="local", clock=lambda: frozen)
    b = _research(res_b, source="local", clock=lambda: frozen)
    assert len(a.inventory()) == len(b.inventory())


def test_missing_catalog_file_raises(research_resources: ResearchResources, tmp_path: Path) -> None:
    frozen = datetime(2026, 1, 15, tzinfo=BRASILIA_TZ)
    research = _research(research_resources, source="local", clock=lambda: frozen)
    catalog = research_resources.catalog()
    with patch.object(
        catalog,
        "files_for_range",
        return_value=[tmp_path / "missing.parquet"],
    ):
        with pytest.raises(ValueError, match="missing parquet"):
            research.bars("PETR4", timeframe="D1", start="2024-12-16", end="2024-12-17")


def test_bounds_validation() -> None:
    with pytest.raises(ValueError, match="start must be"):
        parse_bounds("2024-01-02", "2024-01-01")
    with pytest.raises(ValueError, match="Unknown timeframe"):
        normalize_timeframe("INVALID")
