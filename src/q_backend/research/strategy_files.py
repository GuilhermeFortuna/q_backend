"""ResearchStrategy files in the strategy directory: discovery, bundles, validation and loading.

A strategy file is one top-level ``*.py`` module whose name does not start with ``_``. Helper
modules (``_``-prefixed top-level files and packages) hold shared code and are imported
relatively. The strategy file and every helper form its *bundle*: a content hash identifies the
bundle, and the bundle is materialized under ``<bundle_root>/<hash>/q_strategies/`` so that a
frozen version runs the exact source it was validated from, whatever the live file says now.
"""

from __future__ import annotations

import importlib
import inspect
import multiprocessing
import os
import pickle
import re
import shutil
import sys
import tempfile
from collections.abc import Iterable, MutableMapping
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from types import ModuleType
from typing import Any

from q_backend.research.hooks import detect_hook_call, ticks_mode
from q_backend.research.params import DeclaredParam, ParamError, declared_params
from q_backend.research.strategy import ResearchStrategy
from q_backend.storage.settings import get_settings

PACKAGE = "q_strategies"
DEFAULT_TIMEOUT_S = 10.0
_HOOKS = ("compute_indicators", "entry_strategy", "exit_strategy")
_IGNORED_DIRS = frozenset({"__pycache__"})
_STAGING_PREFIX = ".staging-"


class StrategyFileError(ValueError):
    """A strategy file cannot be found, imported, or loaded as one ResearchStrategy."""


@dataclass(frozen=True)
class BundleFile:
    """One file of a bundle: its path relative to the strategy directory, and its source text."""

    path: str
    content: str


@dataclass(frozen=True)
class StrategyBundle:
    """A strategy file with its helper modules, identified by a SHA-256 over their paths and contents."""

    strategy_id: str
    bundle_hash: str
    files: tuple[BundleFile, ...]


@dataclass(frozen=True)
class StrategyFileInfo:
    """The validation result of one strategy file. Invalid files carry only their id, hash and error."""

    id: str
    label: str
    description: str
    class_name: str | None
    bundle_hash: str
    valid: bool
    error: str | None
    parameters: tuple[DeclaredParam, ...]
    uses_tick_hooks: bool


def strategy_dir() -> Path:
    """The configured strategy directory (``Q_STRATEGY_DIR``), with ``~`` expanded."""
    return Path(get_settings().strategy_dir).expanduser()


def default_bundle_root() -> Path:
    """Where materialized bundles are written (``Q_STRATEGY_BUNDLE_ROOT``)."""
    return Path(get_settings().strategy_bundle_root)


def strategy_ids(root: Path) -> list[str]:
    """Ids of the strategy files in ``root``, sorted. A missing directory has none."""
    if not root.is_dir():
        return []
    return sorted(path.stem for path in root.glob("*.py") if path.is_file() and not path.name.startswith(("_", ".")))


def build_bundle(root: Path, strategy_id: str) -> StrategyBundle:
    """Read the strategy file and every helper module under ``root`` and hash them."""
    strategy_path = root / f"{strategy_id}.py"
    if not strategy_path.is_file():
        raise StrategyFileError(f"no strategy file '{strategy_id}.py' in {root}")

    sources = {strategy_path.name: strategy_path}
    for helper in _helper_paths(root):
        sources[helper.relative_to(root).as_posix()] = helper

    return bundle_from_files(
        strategy_id,
        (BundleFile(path=relative, content=_read(sources[relative])) for relative in sources),
    )


def bundle_from_files(strategy_id: str, files: Iterable[BundleFile]) -> StrategyBundle:
    """A bundle over ``files``, sorted by path, with its hash computed from their paths and contents."""
    ordered = tuple(sorted(files, key=lambda file: file.path))
    digest = sha256()
    for file in ordered:
        encoded = file.content.encode("utf-8")
        digest.update(f"{file.path}\0{len(encoded)}\0".encode("utf-8"))
        digest.update(encoded)
    return StrategyBundle(strategy_id=strategy_id, bundle_hash=digest.hexdigest(), files=ordered)


def materialize(bundle: StrategyBundle, bundle_root: Path) -> Path:
    """Write the bundle to ``<bundle_root>/<hash>/q_strategies/`` and return ``<bundle_root>/<hash>``.

    The directory is content-addressed: a hash that already exists is reused. Files are staged in a
    sibling directory and renamed into place, so a concurrent reader never sees a partial bundle.
    """
    final = bundle_root / bundle.bundle_hash
    if (final / PACKAGE).is_dir():
        return final

    bundle_root.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=f"{_STAGING_PREFIX}{bundle.bundle_hash[:12]}-", dir=bundle_root))
    try:
        package = staging / PACKAGE
        for file in bundle.files:
            target = package / file.path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(file.content, encoding="utf-8")
        if not (package / "__init__.py").exists():
            (package / "__init__.py").write_text("", encoding="utf-8")
        os.rename(staging, final)
    except OSError:
        shutil.rmtree(staging, ignore_errors=True)
        if not (final / PACKAGE).is_dir():
            raise
    return final


def load_bundle(bundle: StrategyBundle, bundle_root: Path) -> type[ResearchStrategy]:
    """Import the bundle and return its one ResearchStrategy class.

    Any earlier ``q_strategies`` modules are evicted first, so two bundles never mix in one process.
    """
    import_root = materialize(bundle, bundle_root)
    try:
        module = _import_module(import_root, bundle.strategy_id)
    except Exception as exc:
        raise StrategyFileError(f"'{bundle.strategy_id}' failed to import: {_describe(exc)}") from exc
    return _strategy_class(module)


def load_strategy(
    strategy_id: str,
    *,
    root: Path | None = None,
    bundle_root: Path | None = None,
) -> type[ResearchStrategy]:
    """Return the ResearchStrategy class of the strategy file as it is on disk now.

    Scripts and Optuna drivers use this, so they run the same code the stack runs. ``root``
    defaults to ``Q_STRATEGY_DIR``.
    """
    bundle = build_bundle(root if root is not None else strategy_dir(), strategy_id)
    return load_bundle(bundle, bundle_root if bundle_root is not None else default_bundle_root())


def validate_bundle(
    bundle: StrategyBundle,
    bundle_root: Path,
    *,
    timeout_s: float = DEFAULT_TIMEOUT_S,
) -> StrategyFileInfo:
    """Check the bundle in a fresh child process, so an import that hangs or crashes cannot hurt the caller.

    The check imports the file, requires exactly one ResearchStrategy subclass, validates its
    parameter declaration, builds it with its defaults, pickles it, and checks every hook signature.
    """
    if not bundle.strategy_id.isidentifier():
        return _invalid(bundle, "file name must be a Python identifier")
    import_root = materialize(bundle, bundle_root)
    outcome = _run_check(import_root, bundle.strategy_id, timeout_s)
    if not outcome["ok"]:
        return _invalid(bundle, outcome["error"])
    return StrategyFileInfo(
        id=bundle.strategy_id,
        label=outcome["label"],
        description=outcome["description"],
        class_name=outcome["class_name"],
        bundle_hash=bundle.bundle_hash,
        valid=True,
        error=None,
        parameters=outcome["parameters"],
        uses_tick_hooks=outcome["uses_tick_hooks"],
    )


def list_strategy_files(
    root: Path,
    *,
    bundle_root: Path,
    cache: MutableMapping[str, StrategyFileInfo] | None = None,
    timeout_s: float = DEFAULT_TIMEOUT_S,
) -> list[StrategyFileInfo]:
    """Validate every strategy file in ``root``. Results are cached by bundle hash when ``cache`` is given."""
    infos: list[StrategyFileInfo] = []
    for strategy_id in strategy_ids(root):
        bundle = build_bundle(root, strategy_id)
        if cache is not None and bundle.bundle_hash in cache:
            infos.append(cache[bundle.bundle_hash])
            continue
        info = validate_bundle(bundle, bundle_root, timeout_s=timeout_s)
        if cache is not None:
            cache[bundle.bundle_hash] = info
        infos.append(info)
    return infos


def _helper_paths(root: Path) -> Iterable[Path]:
    """Helper files: top-level ``_*.py`` modules and every file under a top-level ``_*`` package."""
    for entry in sorted(root.iterdir()):
        if not entry.name.startswith("_") or entry.name in _IGNORED_DIRS:
            continue
        if entry.is_file() and entry.suffix == ".py":
            yield entry
        elif entry.is_dir():
            for path in sorted(entry.rglob("*")):
                if not path.is_file() or path.suffix == ".pyc" or _IGNORED_DIRS & set(path.relative_to(root).parts):
                    continue
                yield path


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _invalid(bundle: StrategyBundle, error: str) -> StrategyFileInfo:
    return StrategyFileInfo(
        id=bundle.strategy_id,
        label=bundle.strategy_id,
        description="",
        class_name=None,
        bundle_hash=bundle.bundle_hash,
        valid=False,
        error=error,
        parameters=(),
        uses_tick_hooks=False,
    )


def _run_check(import_root: Path, strategy_id: str, timeout_s: float) -> dict[str, Any]:
    context = multiprocessing.get_context("spawn")
    receiver, sender = context.Pipe(duplex=False)
    process = context.Process(target=_check_in_child, args=(str(import_root), strategy_id, sender), daemon=True)
    process.start()
    sender.close()
    try:
        if not receiver.poll(timeout_s):
            return {"ok": False, "error": f"validation timed out after {timeout_s:g} seconds"}
        try:
            return receiver.recv()
        except EOFError:
            return {"ok": False, "error": f"validation process exited with code {process.exitcode}"}
    finally:
        if process.is_alive():
            process.kill()
        process.join()
        receiver.close()


def _check_in_child(import_root: str, strategy_id: str, sender: Any) -> None:
    try:
        outcome = {"ok": True, **_check(Path(import_root), strategy_id)}
    except BaseException as exc:  # noqa: BLE001 - every failure, including sys.exit, is reported to the parent
        outcome = {"ok": False, "error": _describe(exc)}
    sender.send(outcome)
    sender.close()


def _check(import_root: Path, strategy_id: str) -> dict[str, Any]:
    module = _import_module(import_root, strategy_id)
    cls = _strategy_class(module)
    if inspect.isabstract(cls):
        raise StrategyFileError(f"{cls.__name__} is abstract; it must implement entry_strategy")

    label = getattr(cls, "label", cls.__name__)
    if not isinstance(label, str):
        raise StrategyFileError(f"{cls.__name__}.label must be a string")

    parameters = declared_params(cls)
    instance = cls(**{param.name: param.default for param in parameters})
    try:
        pickle.dumps(instance)
    except Exception as exc:
        raise StrategyFileError(f"{cls.__name__} instance does not pickle: {exc}") from exc

    uses_tick_hooks = False
    for name in _HOOKS:
        uses_tick_hooks = detect_hook_call(instance, name).ticks != "none" or uses_tick_hooks

    return {
        "label": label,
        "description": _first_paragraph(cls),
        "class_name": cls.__name__,
        "parameters": parameters,
        "uses_tick_hooks": uses_tick_hooks,
    }


def _import_module(import_root: Path, strategy_id: str) -> ModuleType:
    """Import ``q_strategies.<strategy_id>`` from one bundle root, with earlier bundles evicted."""
    for name in list(sys.modules):
        if name == PACKAGE or name.startswith(f"{PACKAGE}."):
            del sys.modules[name]
    importlib.invalidate_caches()
    sys.path.insert(0, str(import_root))
    try:
        return importlib.import_module(f"{PACKAGE}.{strategy_id}")
    finally:
        sys.path.remove(str(import_root))


def _strategy_class(module: ModuleType) -> type[ResearchStrategy]:
    classes = [
        value
        for value in vars(module).values()
        if isinstance(value, type) and issubclass(value, ResearchStrategy) and value.__module__ == module.__name__
    ]
    if not classes:
        raise StrategyFileError("defines no ResearchStrategy subclass")
    if len(classes) > 1:
        names = ", ".join(cls.__name__ for cls in classes)
        raise StrategyFileError(
            f"defines {len(classes)} ResearchStrategy subclasses ({names}); a strategy file must define exactly one"
        )
    return classes[0]


def _first_paragraph(cls: type) -> str:
    """The first paragraph of the class's own docstring, on one line."""
    doc = cls.__dict__.get("__doc__")
    if not doc:
        return ""
    paragraph = inspect.cleandoc(doc).split("\n\n", 1)[0]
    return re.sub(r"\s+", " ", paragraph).strip()


def _describe(exc: BaseException) -> str:
    """The message a user reads: our own errors as written, anything else as ``Type: message``."""
    if isinstance(exc, (StrategyFileError, ParamError)):
        return str(exc)
    return f"{type(exc).__name__}: {exc}" if str(exc) else type(exc).__name__
