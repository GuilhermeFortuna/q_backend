import ast
from pathlib import Path
import pytest

from q_contracts.stream import JobProgressPayload, JobTerminalPayload


def scan_status_literals(paths: list[Path]) -> set[str]:
    literals = set()
    for path in paths:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            # 1. Dict key "status"
            if isinstance(node, ast.Dict):
                for k, v in zip(node.keys, node.values):
                    if isinstance(k, ast.Constant) and k.value == "status":
                        if isinstance(v, ast.Constant) and isinstance(v.value, str):
                            literals.add(v.value)
            # 2. Assign to variable or attr named "status"
            elif isinstance(node, ast.Assign):
                for target in node.targets:
                    if (isinstance(target, ast.Name) and target.id == "status") or (
                        isinstance(target, ast.Attribute) and target.attr == "status"
                    ):
                        if isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
                            literals.add(node.value.value)
            # 3. Keyword arg status=...
            elif isinstance(node, ast.keyword) and node.arg == "status":
                if isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
                    literals.add(node.value.value)
            # 4. Compare status == ...
            elif isinstance(node, ast.Compare):
                left_is_status = (isinstance(node.left, ast.Name) and "status" in node.left.id.lower()) or (
                    isinstance(node.left, ast.Attribute) and "status" in node.left.attr.lower()
                )
                if left_is_status:
                    for c in node.comparators:
                        if isinstance(c, ast.Constant) and isinstance(c.value, str):
                            literals.add(c.value)
                        elif isinstance(c, (ast.List, ast.Tuple, ast.Set)):
                            for elt in c.elts:
                                if isinstance(elt, ast.Constant) and isinstance(elt.value, str):
                                    literals.add(elt.value)
    return literals


def test_status_mapping_covers_all_job_manager_literals():
    from q_backend.streaming.jobs import STATUS_TO_STREAM

    repo_root = Path(__file__).parents[2]
    job_files = sorted((repo_root / "src" / "q_backend" / "api").glob("*_jobs.py"))
    assert len(job_files) == 9, f"Expected 9 job manager files, found {len(job_files)}"

    literals = scan_status_literals(job_files)
    assert literals, "Scan found no status literals"

    unmapped = literals - set(STATUS_TO_STREAM.keys())
    assert not unmapped, f"Unmapped status literals found in job managers: {unmapped}"

    valid_stream_statuses = {"queued", "running", "completed", "failed", "cancelled"}
    for raw, stream_status in STATUS_TO_STREAM.items():
        assert stream_status in valid_stream_statuses, f"Invalid stream status '{stream_status}' for raw '{raw}'"


def test_status_mapping_fails_on_unmapped_literal(tmp_path):
    from q_backend.streaming.jobs import STATUS_TO_STREAM

    scratch_file = tmp_path / "scratch_jobs.py"
    scratch_file.write_text('status = "errored"\n', encoding="utf-8")

    literals = scan_status_literals([scratch_file])
    assert "errored" in literals
    assert "errored" not in STATUS_TO_STREAM


def test_namespace_to_kind_mapping():
    from q_backend.streaming.jobs import NAMESPACE_TO_KIND

    expected_kinds = {
        "alpha_research",
        "backtest",
        "discovery_ab",
        "encoder_ablation",
        "neural_training",
        "optimization",
        "storage_ingest",
        "strategy_search",
        "walkforward",
    }
    assert set(NAMESPACE_TO_KIND.values()) == expected_kinds
    assert NAMESPACE_TO_KIND["job"] == "optimization"
