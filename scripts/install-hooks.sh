#!/usr/bin/env bash
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

echo "==> Configuring Git hooks path to .githooks..."
git config core.hooksPath .githooks
chmod +x .githooks/*
echo "Git hooks configured successfully (.githooks)."
