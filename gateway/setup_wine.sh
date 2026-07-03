#!/usr/bin/env bash
#
# setup_wine.sh — idempotent, re-runnable setup for the MT5 remote data gateway
# under Wine (WO183/WO186). Prepares a dedicated 64-bit Wine prefix containing:
#   * a Windows Python (full installer), and
#   * the pinned `MetaTrader5` pip package,
# then installs the MetaTrader 5 terminal into the same prefix and prints the launch
# commands.
#
# GUARDRAILS (see q_backend/docs/mt5-wine-gateway.md):
#   * This script NEVER stores, prompts for, or logs broker credentials. Terminal login
#     is done ONCE, by hand, in the terminal GUI; it then lives inside the Wine prefix.
#   * Nothing here auto-updates: the Wine version is recorded and change-checked, the
#     Windows Python and the MetaTrader5 pip version are pinned. Upgrading is a
#     deliberate, documented operator action (bump the pins below and re-run).
#
# Re-running is safe: each step is skipped when its artifact already exists.
#
# Usage:
#   ./setup_wine.sh              # run all steps
#   ./setup_wine.sh --help
#
# Overridable via environment:
#   MT5_GATEWAY_PREFIX   Wine prefix dir (default: ~/.local/share/mt5-gateway-prefix)
#   MT5_DOWNLOAD_DIR     where installers are cached (default: <prefix>/.gateway-downloads)

set -euo pipefail

# --------------------------------------------------------------------------------------
# Pins — bump deliberately, then re-run this script. See the doc for the bump checklist.
# --------------------------------------------------------------------------------------
# Windows Python installed inside the prefix. 3.11.x has well-tested Wine behavior and
# a matching MetaTrader5 cp311 win_amd64 wheel. To bump: pick a version from
# https://www.python.org/downloads/windows/ that also has a MetaTrader5 wheel (below).
readonly PYTHON_VERSION="3.11.9"
# MetaTrader5 pip version. To bump: check https://pypi.org/project/MetaTrader5/#history
# and confirm a win_amd64 wheel exists for PYTHON_VERSION's cpXY tag, then update both.
readonly MT5_PIP_VERSION="5.0.5735"
# Windows install target for Python, inside the prefix (drive_c/Python311).
readonly PYTHON_WIN_DIR='C:\Python311'
readonly PYTHON_REL_DIR="drive_c/Python311"

# Official download URLs.
readonly PYTHON_URL="https://www.python.org/ftp/python/${PYTHON_VERSION}/python-${PYTHON_VERSION}-amd64.exe"
# Generic MetaQuotes MT5 installer. Swap for your broker's installer if you prefer; the
# broker is selected/logged-in inside the terminal GUI regardless.
readonly MT5_SETUP_URL="https://download.mql5.com/cdn/web/metaquotes.software.corp/mt5/mt5setup.exe"

# --------------------------------------------------------------------------------------
# Resolved config.
# --------------------------------------------------------------------------------------
PREFIX="${MT5_GATEWAY_PREFIX:-${HOME}/.local/share/mt5-gateway-prefix}"
DOWNLOAD_DIR="${MT5_DOWNLOAD_DIR:-${PREFIX}/.gateway-downloads}"
WINE_MARKER="${PREFIX}/.gateway-setup-marker"
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" >/dev/null 2>&1 && pwd -P)"

readonly PREFIX DOWNLOAD_DIR WINE_MARKER SCRIPT_DIR

# Wine looks these up; export once.
export WINEPREFIX="${PREFIX}"
export WINEARCH="win64"
# Keep Wine quiet unless the operator is debugging.
export WINEDEBUG="${WINEDEBUG:--all}"

# --------------------------------------------------------------------------------------
# Small helpers.
# --------------------------------------------------------------------------------------
log()  { printf '\033[1;34m[setup]\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[warn]\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[1;31m[error]\033[0m %s\n' "$*" >&2; exit 1; }

need_cmd() {
    command -v "$1" >/dev/null 2>&1 || die "required command not found: $1 (install it and re-run)"
}

sha256_of() {
    sha256sum "$1" | awk '{print $1}'
}

download() {
    # download <url> <dest>; skips when a non-empty file already exists.
    local url="$1" dest="$2"
    if [[ -s "${dest}" ]]; then
        log "already downloaded: $(basename -- "${dest}")"
        return 0
    fi
    log "downloading $(basename -- "${dest}") ..."
    if command -v curl >/dev/null 2>&1; then
        curl -fSL --retry 3 -o "${dest}.part" "${url}"
    else
        wget -O "${dest}.part" "${url}"
    fi
    mv -f "${dest}.part" "${dest}"
    log "  sha256($(basename -- "${dest}")) = $(sha256_of "${dest}")"
    log "  ^ record this and verify against the official checksum before trusting it."
}

if [[ "${1:-}" == "--help" || "${1:-}" == "-h" ]]; then
    sed -n '2,40p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
    exit 0
fi

# --------------------------------------------------------------------------------------
# 0. Preconditions.
# --------------------------------------------------------------------------------------
need_cmd wine
need_cmd sha256sum
need_cmd awk
if ! command -v curl >/dev/null 2>&1 && ! command -v wget >/dev/null 2>&1; then
    die "need either curl or wget to download installers"
fi

WINE_VERSION="$(wine --version 2>/dev/null || echo 'unknown')"
readonly WINE_VERSION
log "system wine: ${WINE_VERSION}"

# --------------------------------------------------------------------------------------
# 1. Wine prefix — create (pinned) and change-check the Wine version (risk (b) mitigation).
# --------------------------------------------------------------------------------------
if [[ -f "${WINE_MARKER}" ]]; then
    recorded_wine="$(grep -E '^wine_version=' "${WINE_MARKER}" | head -n1 | cut -d= -f2- || true)"
    if [[ -n "${recorded_wine}" && "${recorded_wine}" != "${WINE_VERSION}" ]]; then
        warn "================================================================"
        warn "Wine version CHANGED since this prefix was created:"
        warn "  recorded: ${recorded_wine}"
        warn "  current:  ${WINE_VERSION}"
        warn "The prefix is pinned on purpose. A different Wine may break the MT5"
        warn "terminal or the MetaTrader5 module. If this was intentional, delete"
        warn "the prefix (${PREFIX}) and re-run, or update the marker knowingly."
        warn "================================================================"
    fi
else
    log "creating 64-bit Wine prefix at ${PREFIX} ..."
    mkdir -p "${PREFIX}"
    # wineboot initializes the prefix; harmless if it already exists.
    wineboot --init >/dev/null 2>&1 || true
    mkdir -p "${DOWNLOAD_DIR}"
    {
        printf 'wine_version=%s\n' "${WINE_VERSION}"
        printf 'created_utc=%s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
        printf 'python_version=%s\n' "${PYTHON_VERSION}"
        printf 'metatrader5_pip=%s\n' "${MT5_PIP_VERSION}"
    } >"${WINE_MARKER}"
    log "recorded pins in ${WINE_MARKER}"
fi
mkdir -p "${DOWNLOAD_DIR}"

# --------------------------------------------------------------------------------------
# 2. Windows Python inside the prefix.
# --------------------------------------------------------------------------------------
PYTHON_EXE_REL="${PREFIX}/${PYTHON_REL_DIR}/python.exe"
readonly PYTHON_EXE_REL
if [[ -f "${PYTHON_EXE_REL}" ]]; then
    log "Windows Python already installed (${PYTHON_REL_DIR})."
else
    python_installer="${DOWNLOAD_DIR}/python-${PYTHON_VERSION}-amd64.exe"
    download "${PYTHON_URL}" "${python_installer}"
    log "installing Windows Python ${PYTHON_VERSION} into ${PYTHON_WIN_DIR} (silent) ..."
    wine "${python_installer}" /quiet \
        InstallAllUsers=1 \
        PrependPath=0 \
        Include_launcher=0 \
        Include_test=0 \
        Include_pip=1 \
        "TargetDir=${PYTHON_WIN_DIR}"
    [[ -f "${PYTHON_EXE_REL}" ]] || die "Python install did not produce ${PYTHON_EXE_REL}"
    log "Windows Python installed."
fi

# --------------------------------------------------------------------------------------
# 3. Pinned MetaTrader5 pip package inside the Wine Python.
#    numpy is pulled in as a MetaTrader5 dependency; nothing else is needed by the gateway.
# --------------------------------------------------------------------------------------
installed_mt5="$(wine "${PYTHON_WIN_DIR}\\python.exe" -m pip show MetaTrader5 2>/dev/null \
    | awk -F': ' '/^Version:/ {print $2}' | tr -d '\r' || true)"
if [[ "${installed_mt5}" == "${MT5_PIP_VERSION}" ]]; then
    log "MetaTrader5==${MT5_PIP_VERSION} already installed in the Wine Python."
else
    log "installing MetaTrader5==${MT5_PIP_VERSION} into the Wine Python ..."
    wine "${PYTHON_WIN_DIR}\\python.exe" -m pip install --no-input --disable-pip-version-check \
        "MetaTrader5==${MT5_PIP_VERSION}"
    log "MetaTrader5 installed."
fi

# --------------------------------------------------------------------------------------
# 4. MetaTrader 5 terminal.
# --------------------------------------------------------------------------------------
# Accept broker-branded installs too (e.g. "Genial Investimentos MetaTrader 5") —
# any terminal64.exe under Program Files counts as installed.
TERMINAL_EXE_REL="$(find "${PREFIX}/drive_c/Program Files" -maxdepth 2 -name terminal64.exe 2>/dev/null | head -n1)"
if [[ -z "${TERMINAL_EXE_REL}" ]]; then
    TERMINAL_EXE_REL="${PREFIX}/drive_c/Program Files/MetaTrader 5/terminal64.exe"
fi
readonly TERMINAL_EXE_REL
if [[ -f "${TERMINAL_EXE_REL}" ]]; then
    log "MetaTrader 5 terminal already installed (${TERMINAL_EXE_REL})."
else
    mt5_installer="${DOWNLOAD_DIR}/mt5setup.exe"
    download "${MT5_SETUP_URL}" "${mt5_installer}"
    log "launching MT5 terminal installer (may open a GUI; follow it once) ..."
    warn "If the installer is interactive, complete it, then re-run this script to continue."
    wine "${mt5_installer}" /auto || \
        warn "MT5 installer returned non-zero; if the terminal is installed you can ignore this."
fi

# --------------------------------------------------------------------------------------
# 5. Summary + next steps.
# --------------------------------------------------------------------------------------
gateway_script="${SCRIPT_DIR}/mt5_gateway.py"
cat <<EOF

$(log 'setup complete')

Pins recorded in: ${WINE_MARKER}
  wine (system) : ${WINE_VERSION}   (change-checked on every run)
  python (win)  : ${PYTHON_VERSION}
  MetaTrader5   : ${MT5_PIP_VERSION}

MANUAL, ONE-TIME (credentials never touch this script):
  1. Start the terminal and log in to your broker account in its GUI:
       WINEPREFIX="${PREFIX}" wine "${TERMINAL_EXE_REL}"
     Tools > Options > check "Enable algorithmic/automated trading" is not required for
     read-only data, but the terminal MUST stay logged in and running.

RUN THE GATEWAY (terminal must be running + logged in first):
  WINEPREFIX="${PREFIX}" \\
    wine "${PYTHON_WIN_DIR}\\python.exe" \\
    "\$(WINEPREFIX="${PREFIX}" winepath -w "${gateway_script}")" \\
    --host 127.0.0.1 --port 18812
  # add --token <secret> (or export MT5_GATEWAY_TOKEN) for non-localhost binds.

VERIFY:
  curl -s http://127.0.0.1:18812/v1/health

AUTOSTART (systemd user units): see gateway/systemd/ and
  q_backend/docs/mt5-wine-gateway.md
EOF
