"""Shared fixtures for the replay simulator tests: fake monotonic time and small run configs."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

import pytest

from q_backend.replay.config import ReplayRunConfig
from q_backend.replay.controller import ReplayController
from q_backend.replay.journal import ReplayRunStore

START = datetime(2026, 10, 1, 12, 0, 0, tzinfo=UTC)
RUN_ID = "0b8f1f0e-6a7e-4d59-9a4b-3f1c2a7e9d10"
GENERATION = "gen-0001"


class FakeMono:
    """Monotonic nanosecond clock the test controls explicitly."""

    def __init__(self, start_ns: int = 1_000_000_000) -> None:
        self.ns = start_ns

    def __call__(self) -> int:
        return self.ns

    def advance_ms(self, ms: float) -> None:
        self.ns += int(ms * 1_000_000)


def make_config(
    *,
    end_seconds: int = 60,
    rate: float = 1.0,
    max_queued_events: int = 100,
    max_journal_bytes: int = 1_000_000,
    market_data_ms: int = 0,
    warmup: bool = False,
) -> ReplayRunConfig:
    from datetime import timedelta

    raw = {
        "manifest_path": "manifests/test.json",
        "symbols": ["WDO$N", "WIN$N"],
        "start_utc": START,
        "end_utc": START + timedelta(seconds=end_seconds),
        "warmup_start_utc": START - timedelta(minutes=5) if warmup else None,
        "initial_balance": "100000.00",
        "account": {"login": 900001, "currency": "BRL"},
        "latency": {"market_data_ms": market_data_ms},
        "rate": rate,
        "limits": {
            "max_queued_events": max_queued_events,
            "max_journal_bytes": max_journal_bytes,
        },
    }
    return ReplayRunConfig.model_validate(raw)


@pytest.fixture
def mono() -> FakeMono:
    return FakeMono()


@pytest.fixture
def store(tmp_path: Path) -> ReplayRunStore:
    instance = ReplayRunStore(tmp_path / "replay.sqlite3")
    yield instance
    instance.close()


@pytest.fixture
def controller_factory(tmp_path: Path, mono: FakeMono) -> Callable[..., ReplayController]:
    """Open controllers on the same store file, so restart and recovery can be exercised."""

    def open_controller(handler=None) -> ReplayController:
        kwargs = {"mono_ns": mono}
        if handler is not None:
            kwargs["handler"] = handler
        return ReplayController(ReplayRunStore(tmp_path / "replay.sqlite3"), **kwargs)

    return open_controller
