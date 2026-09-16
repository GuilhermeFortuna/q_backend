#!/usr/bin/env bash
# Shared hook bootstrap.
#
# Hooks spawned by GUI git clients (GitKraken, VS Code, JetBrains) inherit a
# minimal PATH that omits per-user install directories. `uv` installs to
# ~/.local/bin by default, so every hook died with "uv: command not found"
# before running a single check — the same class of problem scripts/ci.sh
# already works around for the session bus vars.
ensure_uv_on_path() {
    command -v uv >/dev/null 2>&1 && return 0

    local candidate
    for candidate in "$HOME/.local/bin" "$HOME/.cargo/bin" /usr/local/bin /opt/homebrew/bin; do
        if [[ -x "${candidate}/uv" ]]; then
            export PATH="${candidate}:${PATH}"
            return 0
        fi
    done

    echo "error: 'uv' was not found on PATH and is required by this hook." >&2
    echo "       Looked in: ~/.local/bin ~/.cargo/bin /usr/local/bin /opt/homebrew/bin" >&2
    echo "       Install uv (https://docs.astral.sh/uv/) or add its directory to PATH." >&2
    return 1
}
