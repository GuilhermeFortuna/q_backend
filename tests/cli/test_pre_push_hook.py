"""Push-hook Git context must not leak into the CI pipeline's foreign checkouts."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize("linked_worktree", [False, True])
def test_pre_push_ci_can_address_a_foreign_repository(tmp_path: Path, linked_worktree: bool) -> None:
    names = subprocess.check_output(["git", "rev-parse", "--local-env-vars"], cwd=REPO_ROOT, text=True).splitlines()
    env = {name: value for name, value in os.environ.items() if name not in names}

    def git(*args: str, cwd: Path) -> str:
        return subprocess.check_output(["git", *args], cwd=cwd, env=env, text=True).strip()

    repo = tmp_path / "repo"
    repo.mkdir()
    git("init", "--quiet", cwd=repo)
    git(
        "-c",
        "user.name=Test",
        "-c",
        "user.email=test@example.invalid",
        "-c",
        "commit.gpgsign=false",
        "commit",
        "--quiet",
        "--allow-empty",
        "-m",
        "fixture",
        cwd=repo,
    )
    worktree = repo
    if linked_worktree:
        worktree = tmp_path / "worktree"
        git("worktree", "add", "--quiet", "--detach", str(worktree), cwd=repo)

    foreign = tmp_path / "foreign"
    foreign.mkdir()
    git("init", "--quiet", cwd=foreign)
    git(
        "-c",
        "user.name=Test",
        "-c",
        "user.email=test@example.invalid",
        "-c",
        "commit.gpgsign=false",
        "commit",
        "--quiet",
        "--allow-empty",
        "-m",
        "foreign fixture",
        cwd=foreign,
    )
    foreign_revision = git("rev-parse", "HEAD", cwd=foreign)
    hooks = worktree / ".githooks"
    hooks.mkdir()
    for name in ("pre-push", "_common.sh"):
        shutil.copyfile(REPO_ROOT / ".githooks" / name, hooks / name)
    scripts = worktree / "scripts"
    scripts.mkdir()
    ci = scripts / "ci.sh"
    ci.write_text(
        '#!/usr/bin/env bash\nset -euo pipefail\ngit -C "$HOOK_FOREIGN" cat-file -t "$HOOK_REVISION" > "$HOOK_RESULT"\n'
    )
    ci.chmod(0o755)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    uv = bin_dir / "uv"
    uv.write_text("#!/usr/bin/env bash\nexit 0\n")
    uv.chmod(0o755)
    result_file = tmp_path / "result"
    env.update(
        GIT_DIR=git("rev-parse", "--absolute-git-dir", cwd=worktree) if linked_worktree else ".git",
        HOOK_FOREIGN=str(foreign),
        HOOK_REVISION=foreign_revision,
        HOOK_RESULT=str(result_file),
        PATH=f"{bin_dir}:{env['PATH']}",
    )
    result = subprocess.run(
        ["bash", str(hooks / "pre-push")], cwd=worktree, env=env, capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert result_file.read_text().strip() == "commit"
