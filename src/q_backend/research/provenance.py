"""Provenance helpers for research scripts and figures."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from typing import Any


def find_git_repo(path: Path) -> tuple[Path | None, str | None, bool | None]:
    try:
        proc = subprocess.run(
            ["git", "-C", str(path), "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            check=False,
        )
        if proc.returncode != 0 or not proc.stdout.strip():
            return None, None, None
        git_root = Path(proc.stdout.strip()).resolve()

        rev_proc = subprocess.run(
            ["git", "-C", str(git_root), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            check=False,
        )
        rev = rev_proc.stdout.strip() if rev_proc.returncode == 0 and rev_proc.stdout.strip() else None

        status_proc = subprocess.run(
            ["git", "-C", str(git_root), "status", "--porcelain"],
            capture_output=True,
            text=True,
            check=False,
        )
        dirty = bool(status_proc.stdout.strip()) if status_proc.returncode == 0 else None
        return git_root, rev, dirty
    except Exception:  # noqa: BLE001
        return None, None, None


def get_script_path() -> Path | None:
    try:
        main_mod = sys.modules.get("__main__")
        main_file = getattr(main_mod, "__file__", None)
        if main_file:
            p = Path(main_file)
            if p.name != "pytest" and not p.name.startswith("pytest-") and p.is_file():
                return p.resolve()
        if sys.argv and sys.argv[0]:
            p = Path(sys.argv[0])
            if p.name != "pytest" and not p.name.startswith("pytest-") and p.is_file():
                return p.resolve()
    except Exception:  # noqa: BLE001, S110 - script path resolution is best-effort and degrades to None
        pass
    return None


def collect_figure_provenance() -> dict[str, Any]:
    """Collect provenance metadata (script, git revision, dirty flag) safely."""
    script_str: str | None = None
    git_rev: str | None = None
    git_dirty: bool | None = None

    try:
        script_path = get_script_path()
        if script_path is not None:
            git_root, rev, dirty = find_git_repo(script_path.parent)
            if git_root is not None:
                try:
                    script_str = str(script_path.relative_to(git_root))
                except ValueError:
                    script_str = str(script_path)
                git_rev = rev
                git_dirty = dirty
            else:
                script_str = str(script_path)
        else:
            git_root, rev, dirty = find_git_repo(Path.cwd())
            git_rev = rev
            git_dirty = dirty
    except Exception:  # noqa: BLE001, S110
        pass

    return {
        "script": script_str,
        "git_revision": git_rev,
        "git_dirty": git_dirty,
    }
