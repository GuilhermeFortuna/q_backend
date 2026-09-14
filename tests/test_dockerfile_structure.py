"""Dockerfile structure tests for the Research development image (Q-032)."""

from __future__ import annotations

from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
DOCKERFILE = ROOT / "Dockerfile"


def _instructions(text: str) -> list[str]:
    lines: list[str] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        lines.append(line)
    return lines


def test_dockerfile_dependency_layer_precedes_source_and_uses_uv_cache() -> None:
    text = DOCKERFILE.read_text(encoding="utf-8")
    instructions = _instructions(text)

    assert any(line.startswith("ARG Q_RESEARCH_IMAGE_FINGERPRINT") for line in instructions)
    assert any("dev.q.backend.research-fingerprint" in line for line in instructions)

    # Relative order of COPY / RUN sync steps
    copy_meta_idx = next(i for i, line in enumerate(instructions) if line.startswith("COPY pyproject.toml"))
    dep_sync_idxs = [i for i, line in enumerate(instructions) if "uv sync" in line and "--no-install-project" in line]
    assert dep_sync_idxs, "dependency sync must use uv sync --no-install-project"
    dep_sync_idx = dep_sync_idxs[0]
    assert "cache" in text.lower() and "/root/.cache/uv" in text
    assert "--mount=type=cache" in text

    copy_src_idx = next(i for i, line in enumerate(instructions) if line.startswith("COPY src"))
    project_sync_idxs = [
        i
        for i, line in enumerate(instructions)
        if "uv sync" in line and "--no-install-project" not in line and "--frozen" in line
    ]
    assert project_sync_idxs, "project install sync must follow source COPY"
    project_sync_idx = project_sync_idxs[-1]

    assert copy_meta_idx < dep_sync_idx < copy_src_idx < project_sync_idx


def test_dockerfile_declares_syntax_directive() -> None:
    first = DOCKERFILE.read_text(encoding="utf-8").splitlines()[0]
    assert first.startswith("# syntax=docker/dockerfile:"), first
