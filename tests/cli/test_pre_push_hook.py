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
    hooks = repo / ".githooks"
    hooks.mkdir()
    for name in ("pre-push", "_common.sh"):
        shutil.copyfile(REPO_ROOT / ".githooks" / name, hooks / name)
    scripts = repo / "scripts"
    scripts.mkdir()
    ci = scripts / "ci.sh"
    ci.write_text(
        '#!/usr/bin/env bash\nset -euo pipefail\ngit -C "$HOOK_FOREIGN" cat-file -t "$HOOK_REVISION" > "$HOOK_RESULT"\n'
    )
    ci.chmod(0o755)
    git("add", ".githooks", "scripts", cwd=repo)
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
    local_revision = git("rev-parse", "HEAD", cwd=worktree)

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
        ["bash", str(hooks / "pre-push"), "origin", "https://example.invalid/repo.git"],
        cwd=worktree,
        env=env,
        input=f"refs/heads/fixture {local_revision} refs/heads/fixture {'0' * 40}\n",
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert result_file.read_text().strip() == "commit"


@pytest.mark.parametrize("ci_exit_code", [0, 7], ids=["success", "failure"])
def test_pre_push_runs_ci_for_pushed_revision_and_removes_temporary_worktree(tmp_path: Path, ci_exit_code: int) -> None:
    names = subprocess.check_output(["git", "rev-parse", "--local-env-vars"], cwd=REPO_ROOT, text=True).splitlines()
    env = {name: value for name, value in os.environ.items() if name not in names}

    def git(*args: str, cwd: Path) -> str:
        return subprocess.check_output(["git", *args], cwd=cwd, env=env, text=True).strip()

    repo = tmp_path / "repo"
    repo.mkdir()
    git("init", "--quiet", "--initial-branch=current", cwd=repo)
    for key, value in (("user.name", "Test"), ("user.email", "test@example.invalid"), ("commit.gpgsign", "false")):
        git("config", key, value, cwd=repo)

    hooks = repo / ".githooks"
    hooks.mkdir()
    for name in ("pre-push", "_common.sh"):
        shutil.copyfile(REPO_ROOT / ".githooks" / name, hooks / name)
    scripts = repo / "scripts"
    scripts.mkdir()
    result_file = tmp_path / "ci-revisions"
    ci = scripts / "ci.sh"
    ci.write_text(
        "#!/usr/bin/env bash\n"
        "set -euo pipefail\n"
        'git rev-parse HEAD >> "$HOOK_RESULT"\n'
        '[[ "$UV_PROJECT_ENVIRONMENT" == "$PWD/.venv" ]] || exit 43\n'
        '[[ "$(cat revision.txt)" == pushed ]] || exit 42\n'
        '[[ "$PWD" == "$Q_CI_TMPDIR"/q-backend-pre-push.*/checkout ]] || exit 44\n'
        'exit "$HOOK_CI_EXIT"\n'
    )
    ci.chmod(0o755)
    (repo / "revision.txt").write_text("current\n")
    git("add", ".githooks", "scripts", "revision.txt", cwd=repo)
    git("commit", "--quiet", "-m", "current checkout", cwd=repo)
    current_revision = git("rev-parse", "HEAD", cwd=repo)

    git("switch", "--quiet", "-c", "development", cwd=repo)
    (repo / "revision.txt").write_text("pushed\n")
    git("commit", "--quiet", "-am", "pushed revision", cwd=repo)
    pushed_revision = git("rev-parse", "HEAD", cwd=repo)
    git("switch", "--quiet", "current", cwd=repo)
    assert current_revision != pushed_revision

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    uv = bin_dir / "uv"
    uv.write_text("#!/usr/bin/env bash\nexit 0\n")
    uv.chmod(0o755)
    env.update(
        GIT_DIR=".git",
        HOOK_RESULT=str(result_file),
        HOOK_CI_EXIT=str(ci_exit_code),
        UV_PROJECT_ENVIRONMENT=str(tmp_path / "wrong-venv"),
        Q_CI_TMPDIR=str(tmp_path),
        PATH=f"{bin_dir}:{env['PATH']}",
    )
    pushed_ref = f"refs/heads/development {pushed_revision} refs/heads/development {'0' * 40}\n"
    pushed_tag = f"refs/tags/v-test {pushed_revision} refs/tags/v-test {'0' * 40}\n"
    result = subprocess.run(
        ["bash", str(hooks / "pre-push"), "origin", "https://example.invalid/repo.git"],
        cwd=repo,
        env=env,
        input=pushed_ref + pushed_tag,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == ci_exit_code, result.stdout + result.stderr
    assert result_file.read_text().splitlines() == [pushed_revision]
    assert [path.resolve() for path in tmp_path.glob("q-backend-pre-push.*")] == []
    worktrees = git("worktree", "list", "--porcelain", cwd=repo)
    assert [line.removeprefix("worktree ") for line in worktrees.splitlines() if line.startswith("worktree ")] == [
        str(repo.resolve())
    ]
