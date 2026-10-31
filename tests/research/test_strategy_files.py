"""Strategy files: discovery, bundle hashes, validation and loading from a directory."""

from __future__ import annotations

import datetime as dt
from pathlib import Path
import sys

import pytest

from q_backend.research import ResearchStrategy, load_strategy
from q_backend.research.strategy_files import (
    StrategyFileError,
    build_bundle,
    list_strategy_files,
    load_bundle,
    materialize,
    package_name,
    strategy_ids,
    validate_bundle,
)

LATE_SESSION = '''"""Follow the late-session move.

Enters after the configured time.
"""

from __future__ import annotations

import datetime as dt

from q_backend.research import BoolParam, IntParam, ResearchStrategy, TimeParam

from ._lib.ticks import ceil_tick


class LateSessionTrendStrategy(ResearchStrategy):
    """Follow the late-session move.

    Second paragraph is not part of the description.
    """

    label = "Late session trend"
    parameters = {
        "ma_period": IntParam(min=2, max=200, step=1, label="MA period"),
        "use_filter": BoolParam(label="Use filter"),
        "entry_after": TimeParam(label="Entry after"),
    }

    def __init__(self, ma_period: int = 20, use_filter: bool = True, entry_after: dt.time = dt.time(17, 5)):
        self.ma_period = ma_period
        self.use_filter = use_filter
        self.entry_after = entry_after

    def round_up(self, price: float) -> float:
        return ceil_tick(price, 5.0)

    def entry_strategy(self, frame, positions=()):
        return None
'''

LIB_TICKS = """def ceil_tick(price: float, tick: float) -> float:
    return -(-price // tick) * tick
"""

TWO_CLASSES = """from q_backend.research import ResearchStrategy


class First(ResearchStrategy):
    parameters = {}

    def __init__(self):
        pass

    def entry_strategy(self, frame, positions=()):
        return None


class Second(ResearchStrategy):
    parameters = {}

    def __init__(self):
        pass

    def entry_strategy(self, frame, positions=()):
        return None
"""

UNDECLARED = """from q_backend.research import IntParam, ResearchStrategy


class Undeclared(ResearchStrategy):
    parameters = {"length": IntParam(min=1, max=50)}

    def __init__(self, length: int = 5, extra: int = 3):
        self.length = length

    def entry_strategy(self, frame, positions=()):
        return None
"""

IMPORT_FAILS = """raise RuntimeError("boom at import")
"""

SLEEPS = """import time

from q_backend.research import ResearchStrategy


time.sleep(60)


class Sleeper(ResearchStrategy):
    parameters = {}

    def __init__(self):
        pass

    def entry_strategy(self, frame, positions=()):
        return None
"""

UNPICKLABLE = """from q_backend.research import ResearchStrategy


class Unpicklable(ResearchStrategy):
    parameters = {}

    def __init__(self):
        self.key = lambda value: value

    def entry_strategy(self, frame, positions=()):
        return None
"""


def _write(root: Path, relative: str, text: str) -> None:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


@pytest.fixture
def strategy_root(tmp_path: Path) -> Path:
    root = tmp_path / "strategies"
    _write(root, "late_session_trend.py", LATE_SESSION)
    _write(root, "_lib/ticks.py", LIB_TICKS)
    _write(root, "_lib/__init__.py", "")
    _write(root, "two_classes.py", TWO_CLASSES)
    _write(root, "undeclared.py", UNDECLARED)
    _write(root, "import_fails.py", IMPORT_FAILS)
    _write(root, "sleeps.py", SLEEPS)
    _write(root, "unpicklable.py", UNPICKLABLE)
    _write(root, "notes.txt", "not a strategy\n")
    return root


@pytest.fixture
def bundle_root(tmp_path: Path) -> Path:
    return tmp_path / "bundles"


def test_strategy_ids_are_top_level_modules_not_starting_with_underscore(strategy_root: Path) -> None:
    assert strategy_ids(strategy_root) == [
        "import_fails",
        "late_session_trend",
        "sleeps",
        "two_classes",
        "undeclared",
        "unpicklable",
    ]


def test_strategy_id_cannot_leave_the_directory(strategy_root: Path, tmp_path: Path) -> None:
    _write(tmp_path, "outside.py", LATE_SESSION)

    with pytest.raises(StrategyFileError, match="not a strategy id"):
        build_bundle(strategy_root, "../outside")


def test_missing_directory_is_an_empty_catalog(tmp_path: Path, bundle_root: Path) -> None:
    assert strategy_ids(tmp_path / "absent") == []
    assert list_strategy_files(tmp_path / "absent", bundle_root=bundle_root) == []


def test_listing_reports_valid_files_and_their_own_errors(strategy_root: Path, bundle_root: Path) -> None:
    files = {info.id: info for info in list_strategy_files(strategy_root, bundle_root=bundle_root)}

    valid = files["late_session_trend"]
    assert valid.valid is True
    assert valid.error is None
    assert valid.label == "Late session trend"
    assert valid.class_name == "LateSessionTrendStrategy"
    assert valid.description == "Follow the late-session move."
    assert valid.uses_tick_hooks is False
    specs = {param.name: param for param in valid.parameters}
    assert specs["use_filter"].kind == "bool" and specs["use_filter"].default is True
    assert specs["entry_after"].kind == "time" and specs["entry_after"].default == dt.time(17, 5)

    assert files["two_classes"].valid is False
    assert "2 ResearchStrategy subclasses" in files["two_classes"].error
    assert files["two_classes"].parameters == ()

    assert files["undeclared"].valid is False
    assert "'extra'" in files["undeclared"].error

    assert files["import_fails"].valid is False
    assert "RuntimeError: boom at import" in files["import_fails"].error

    assert files["sleeps"].valid is False
    assert "timed out" in files["sleeps"].error

    assert files["unpicklable"].valid is False
    assert "pickle" in files["unpicklable"].error


def test_changing_a_helper_changes_the_bundle_hash_of_files_that_share_it(strategy_root: Path) -> None:
    before = build_bundle(strategy_root, "late_session_trend").bundle_hash
    unrelated = build_bundle(strategy_root, "two_classes").bundle_hash

    _write(strategy_root, "_lib/ticks.py", LIB_TICKS + "\nTICK_SIZE = 0.5\n")

    assert build_bundle(strategy_root, "late_session_trend").bundle_hash != before
    assert build_bundle(strategy_root, "two_classes").bundle_hash != unrelated


def test_listing_validates_again_after_a_helper_changes(strategy_root: Path, bundle_root: Path) -> None:
    cache: dict[str, object] = {}
    list_strategy_files(strategy_root, bundle_root=bundle_root, cache=cache)
    cached_keys = set(cache)

    _write(strategy_root, "_lib/ticks.py", LIB_TICKS + "\nTICK_SIZE = 0.5\n")
    list_strategy_files(strategy_root, bundle_root=bundle_root, cache=cache)

    assert len(set(cache) - cached_keys) >= 1
    assert cached_keys <= set(cache)


def test_load_strategy_returns_the_class_with_its_helper_import_resolved(
    strategy_root: Path, bundle_root: Path
) -> None:
    bundle = build_bundle(strategy_root, "late_session_trend")
    cls = load_strategy("late_session_trend", root=strategy_root, bundle_root=bundle_root)

    assert issubclass(cls, ResearchStrategy)
    assert cls.__name__ == "LateSessionTrendStrategy"
    assert cls.__module__ == f"{package_name(bundle.bundle_hash)}.late_session_trend"
    assert cls(ma_period=3).round_up(12.0) == 15.0


def test_load_strategy_rejects_an_invalid_file(strategy_root: Path, bundle_root: Path) -> None:
    with pytest.raises(StrategyFileError, match="2 ResearchStrategy subclasses"):
        load_strategy("two_classes", root=strategy_root, bundle_root=bundle_root)


def test_loading_a_second_bundle_does_not_mix_with_the_first(strategy_root: Path, bundle_root: Path) -> None:
    first = load_bundle(build_bundle(strategy_root, "late_session_trend"), bundle_root)
    _write(strategy_root, "_lib/ticks.py", LIB_TICKS.replace("-(-price // tick) * tick", "price"))
    second = load_bundle(build_bundle(strategy_root, "late_session_trend"), bundle_root)

    assert first is not second
    assert first(ma_period=3).round_up(12.0) == 15.0
    assert second(ma_period=3).round_up(12.0) == 12.0


def test_materialize_writes_the_bundle_under_its_hash(strategy_root: Path, bundle_root: Path) -> None:
    bundle = build_bundle(strategy_root, "late_session_trend")

    import_root = materialize(bundle, bundle_root)

    assert import_root == bundle_root / bundle.bundle_hash
    pkg = package_name(bundle.bundle_hash)
    assert (import_root / pkg / "late_session_trend.py").read_text(encoding="utf-8") == LATE_SESSION
    assert (import_root / pkg / "_lib" / "ticks.py").read_text(encoding="utf-8") == LIB_TICKS


def test_two_different_bundles_load_side_by_side_in_one_process(strategy_root: Path, bundle_root: Path) -> None:
    bundle_a = build_bundle(strategy_root, "late_session_trend")
    first_cls = load_bundle(bundle_a, bundle_root)

    _write(strategy_root, "_lib/ticks.py", LIB_TICKS.replace("-(-price // tick) * tick", "price"))
    bundle_b = build_bundle(strategy_root, "late_session_trend")
    second_cls = load_bundle(bundle_b, bundle_root)

    assert first_cls is not second_cls
    assert bundle_a.bundle_hash != bundle_b.bundle_hash

    pkg_a = f"q_strategies_{bundle_a.bundle_hash[:12]}"
    pkg_b = f"q_strategies_{bundle_b.bundle_hash[:12]}"
    assert f"{pkg_a}.late_session_trend" in sys.modules
    assert f"{pkg_b}.late_session_trend" in sys.modules

    assert first_cls(ma_period=3).round_up(12.0) == 15.0
    assert second_cls(ma_period=3).round_up(12.0) == 12.0


@pytest.mark.parametrize("default, required", [("", True), ("=None", False)])
def test_tick_hook_requirement_is_discovered(strategy_root, tmp_path, default, required):
    source = """from q_backend.research import ResearchStrategy

class TickAware(ResearchStrategy):
    parameters = {}

    def entry_strategy(self, frame, *, ticks%s):
        return None
""" % default
    _write(strategy_root, "tick_aware.py", source)
    info = validate_bundle(build_bundle(strategy_root, "tick_aware"), tmp_path / "tick-bundles")
    assert info.valid, info.error
    assert info.uses_tick_hooks
    assert info.requires_tick_data is required
