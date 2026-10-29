"""Immutable strategy versions: freezing bundles, idempotence and survival across file edits."""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import func, select

from q_backend.research import strategy_versions
from q_backend.research.strategy_files import StrategyFileError
from q_backend.research.strategy_versions import bundle_of, freeze_strategy_file
from q_backend.storage.db.models import BacktestConfig, BacktestRun, ResearchStrategyVersion

STRATEGY = '''from __future__ import annotations

from q_backend.research import IntParam, ResearchStrategy


class Ramp(ResearchStrategy):
    """A ramp strategy used by version tests."""

    parameters = {"length": IntParam(min=1, max=50, label="Length")}

    def __init__(self, length: int = 5):
        self.length = length

    def entry_strategy(self, frame, positions=()):
        return None
'''


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


@pytest.fixture
def strategy_root(tmp_path: Path) -> Path:
    root = tmp_path / "strategies"
    _write(root / "ramp.py", STRATEGY)
    return root


@pytest.fixture
def bundle_root(tmp_path: Path) -> Path:
    return tmp_path / "bundles"


def test_freezing_creates_one_version_and_reuses_it_for_the_same_bundle(db_session, strategy_root, bundle_root):
    first = freeze_strategy_file(db_session, strategy_root, "ramp", bundle_root=bundle_root)
    second = freeze_strategy_file(db_session, strategy_root, "ramp", bundle_root=bundle_root)
    db_session.flush()

    assert first.id == second.id
    assert first.strategy_id == "ramp"
    assert first.class_name == "Ramp"
    assert [spec["name"] for spec in first.param_specs] == ["length"]
    assert first.param_specs[0]["type"] == "int"
    assert {entry["path"] for entry in first.source_files} == {"ramp.py"}
    assert db_session.scalar(select(func.count()).select_from(ResearchStrategyVersion)) == 1


def test_editing_the_file_freezes_a_second_version_and_keeps_the_first(db_session, strategy_root, bundle_root):
    original = freeze_strategy_file(db_session, strategy_root, "ramp", bundle_root=bundle_root)
    original_hash = original.bundle_hash
    original_source = original.source_files

    _write(strategy_root / "ramp.py", STRATEGY.replace("length: int = 5", "length: int = 7"))
    edited = freeze_strategy_file(db_session, strategy_root, "ramp", bundle_root=bundle_root)
    db_session.flush()

    assert edited.id != original.id
    assert edited.bundle_hash != original_hash
    assert original.source_files == original_source
    assert "length: int = 5" in original.source_files[0]["content"]


def test_a_version_survives_deleting_the_file(db_session, strategy_root, bundle_root):
    version = freeze_strategy_file(db_session, strategy_root, "ramp", bundle_root=bundle_root)
    db_session.flush()
    expected = bundle_of(version)

    (strategy_root / "ramp.py").unlink()
    db_session.refresh(version)

    assert bundle_of(version) == expected


def test_bundle_of_rejects_source_that_no_longer_matches_its_hash(db_session, strategy_root, bundle_root):
    version = freeze_strategy_file(db_session, strategy_root, "ramp", bundle_root=bundle_root)
    version.source_files = [{"path": "ramp.py", "content": "tampered\n"}]

    with pytest.raises(StrategyFileError, match="does not match its bundle hash"):
        bundle_of(version)


def test_freezing_an_invalid_file_is_refused(db_session, tmp_path, bundle_root):
    root = tmp_path / "broken"
    _write(root / "broken.py", "raise RuntimeError('no')\n")

    with pytest.raises(StrategyFileError, match="RuntimeError: no"):
        freeze_strategy_file(db_session, root, "broken", bundle_root=bundle_root)

    assert db_session.scalar(select(func.count()).select_from(ResearchStrategyVersion)) == 0


def test_a_concurrent_freeze_of_the_same_bundle_returns_the_winning_row(
    db_session, strategy_root, bundle_root, monkeypatch
):
    winner = freeze_strategy_file(db_session, strategy_root, "ramp", bundle_root=bundle_root)
    db_session.flush()
    real_lookup = strategy_versions.version_by_hash
    calls = {"count": 0}

    def lookup_missing_first(session, bundle_hash):
        # The first lookup misses, as it would for a writer that loses the race after it checked.
        calls["count"] += 1
        return None if calls["count"] == 1 else real_lookup(session, bundle_hash)

    monkeypatch.setattr(strategy_versions, "version_by_hash", lookup_missing_first)
    loser = freeze_strategy_file(db_session, strategy_root, "ramp", bundle_root=bundle_root)

    assert loser.id == winner.id
    assert db_session.scalar(select(func.count()).select_from(ResearchStrategyVersion)) == 1


def test_a_run_references_the_version_it_executed(db_session, strategy_root, bundle_root):
    version = freeze_strategy_file(db_session, strategy_root, "ramp", bundle_root=bundle_root)
    config = BacktestConfig(name="ramp", config={})
    db_session.add(config)
    db_session.flush()
    run = BacktestRun(backtest_config_id=config.id, config={}, strategy_version_id=version.id)
    db_session.add(run)
    db_session.flush()
    db_session.expire_all()

    assert db_session.get(BacktestRun, run.id).strategy_version_id == version.id


def test_migration_adds_versions_table_and_run_link_and_reverts(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    from alembic import command
    from alembic.config import Config
    from alembic.autogenerate import compare_metadata
    from alembic.migration import MigrationContext
    from sqlalchemy import create_engine, inspect

    from q_backend.storage.db.base import Base
    from q_backend.storage.db.migrations import alembic_ini_path
    from q_backend.storage.settings import get_settings

    db_url = f"sqlite:///{tmp_path / 'versions.db'}"
    monkeypatch.setenv("Q_DATABASE_URL", db_url)
    get_settings.cache_clear()
    config = Config(str(alembic_ini_path()))
    config.set_main_option("sqlalchemy.url", db_url)
    engine = create_engine(db_url)

    command.upgrade(config, "20261014_0030")
    assert "research_strategy_versions" not in inspect(engine).get_table_names()

    command.upgrade(config, "head")
    inspector = inspect(engine)
    assert "research_strategy_versions" in inspector.get_table_names()
    assert "strategy_version_id" in {column["name"] for column in inspector.get_columns("backtest_runs")}

    with engine.connect() as connection:
        ours = [
            diff
            for diff in compare_metadata(MigrationContext.configure(connection), Base.metadata)
            if "research_strategy_versions" in str(diff) or "strategy_version_id" in str(diff)
        ]
    assert ours == []

    command.downgrade(config, "20261014_0030")
    inspector = inspect(engine)
    assert "research_strategy_versions" not in inspector.get_table_names()
    assert "strategy_version_id" not in {column["name"] for column in inspector.get_columns("backtest_runs")}
    get_settings.cache_clear()
