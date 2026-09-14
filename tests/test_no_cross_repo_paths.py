import re
import subprocess
from pathlib import Path

SIBLING_REPOS = r"(?:q_backend|q_contracts|q_core|q_frontend|q_terminal)"
CROSS_REPO_PATTERN = re.compile(rf"(?:\.\./)+{SIBLING_REPOS}\b")
MD_LINK_PATTERN = re.compile(
    rf"(?:\[[^\]]*\]\((?:\.\./)+{SIBLING_REPOS}\b|<(?:\.\./)+{SIBLING_REPOS}\b|(?:href|src)=[\"'](?:\.\./)+{SIBLING_REPOS}\b)"
)
INLINE_CODE_PATTERN = re.compile(r"`[^`]*`")


def get_tracked_files(repo_root: Path) -> list[str]:
    result = subprocess.run(
        ["git", "ls-files"],
        cwd=repo_root,
        capture_output=True,
        text=True,
        check=True,
    )
    return [line for line in result.stdout.splitlines() if line.strip()]


def find_cross_repo_violations(repo_root: Path) -> list[str]:
    files = get_tracked_files(repo_root)
    violations: list[str] = []
    for file_rel in files:
        file_path = repo_root / file_rel
        if not file_path.is_file():
            continue
        try:
            content = file_path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue

        is_markdown = file_rel.lower().endswith(".md")
        lines = content.splitlines()
        in_fenced_block = False

        for i, line in enumerate(lines, start=1):
            if is_markdown:
                if line.strip().startswith("```"):
                    in_fenced_block = not in_fenced_block
                    continue
                if in_fenced_block:
                    continue

                stripped = INLINE_CODE_PATTERN.sub("", line)
                if MD_LINK_PATTERN.search(stripped) or (CROSS_REPO_PATTERN.search(stripped) and "`" not in line):
                    violations.append(f"{file_rel}:{i}")
            else:
                if CROSS_REPO_PATTERN.search(line):
                    violations.append(f"{file_rel}:{i}")
    return violations


def test_no_cross_repo_relative_paths() -> None:
    repo_root = Path(__file__).resolve().parent.parent
    violations = find_cross_repo_violations(repo_root)
    assert not violations, "Forbidden cross-repository relative paths found:\n" + "\n".join(violations)
