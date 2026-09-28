#!/usr/bin/env bash
set -euo pipefail

# GUI-spawned git hooks (GitKraken, etc.) often omit the user session bus vars,
# which makes `systemctl --user` fail and skips ci.slice entirely.
if [[ -z "${XDG_RUNTIME_DIR:-}" && -d "/run/user/$(id -u)" ]]; then
  export XDG_RUNTIME_DIR="/run/user/$(id -u)"
fi
if [[ -z "${DBUS_SESSION_BUS_ADDRESS:-}" && -n "${XDG_RUNTIME_DIR:-}" && -S "${XDG_RUNTIME_DIR}/bus" ]]; then
  export DBUS_SESSION_BUS_ADDRESS="unix:path=${XDG_RUNTIME_DIR}/bus"
fi

# Enter the host user ci.slice when available so local CI yields to interactive work.
# Scope gets Nice=10 + idle ionice so install/clone/build IO is deprioritized too.
# No-ops on hosts/runners without systemd-run or the slice (e.g. GitHub Actions).
if [[ "${CI_RESOURCE_CONTROLLED:-0}" != "1" ]]; then
  if command -v systemd-run >/dev/null 2>&1 &&
     systemctl --user status ci.slice >/dev/null 2>&1; then
    _ci_run=(
      systemd-run --user --scope --quiet --collect --slice=ci.slice --nice=10
      --setenv=CI_RESOURCE_CONTROLLED=1
      --setenv=XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR}"
      --setenv=DBUS_SESSION_BUS_ADDRESS="${DBUS_SESSION_BUS_ADDRESS}"
      # Carry PATH across the re-exec: a GUI-spawned hook may have prepended the
      # directory holding `uv`, and losing it here reintroduces the same failure.
      --setenv=PATH="${PATH}"
    )
    if command -v ionice >/dev/null 2>&1; then
      exec "${_ci_run[@]}" ionice -c 3 "$0" "$@"
    fi
    exec "${_ci_run[@]}" "$0" "$@"
  fi
fi

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

DEV_DATABASE_URL="postgresql+psycopg://q:q@localhost:5434/q"
DEV_REDIS_URL="redis://localhost:6380/0"
HOSTED_DATABASE_URL="postgresql+psycopg://postgres:password@localhost:5434/q_storage"
HOSTED_REDIS_URL="redis://localhost:6380/0"

_ci_hosted_mode() {
  [[ "${GITHUB_ACTIONS:-}" == "true" && "${CI:-}" == "true" &&
     -n "${GITHUB_RUN_ID:-}" && -n "${GITHUB_WORKFLOW:-}" ]]
}

_ci_tcp_open() {
  local host="$1"
  local port="$2"
  (echo > "/dev/tcp/${host}/${port}") >/dev/null 2>&1
}

_ci_parse_url_host_port() {
  local url="$1"
  local scheme="${url%%://*}"
  local rest="${url#*://}"
  if [[ "$rest" == *"@"* ]]; then
    rest="${rest#*@}"
  fi
  local hostport="${rest%%/*}"
  local host="${hostport%%:*}"
  local port="${hostport#*:}"
  if [[ "$host" == "$port" ]]; then
    if [[ "$scheme" == redis* ]]; then
      port=6379
    else
      port=5432
    fi
  fi
  printf '%s %s\n' "$host" "$port"
}

_ci_verify_service_url() {
  local label="$1"
  local url="$2"
  read -r host port < <(_ci_parse_url_host_port "$url")
  if [[ -z "$host" || -z "$port" ]]; then
    echo "ERROR: Could not parse ${label} URL: ${url}" >&2
    return 1
  fi
  if ! _ci_tcp_open "$host" "$port"; then
    echo "ERROR: ${label} is not reachable at ${host}:${port}" >&2
    return 1
  fi
}

CI_COMPOSE_PROJECT=""
CI_TMP_ROOT=""
CI_COMPOSE_FILES=()

_ci_compose() {
  COMPOSE_PROJECT_NAME="${CI_COMPOSE_PROJECT}" docker compose "${CI_COMPOSE_FILES[@]}" "$@"
}

_ci_cleanup_local_services() {
  if [[ -z "${CI_COMPOSE_PROJECT:-}" ]]; then
    return 0
  fi
  echo "==> Tearing down CI Compose project (${CI_COMPOSE_PROJECT})..."
  _ci_compose down -v --remove-orphans >/dev/null 2>&1 || true
  if [[ -n "${CI_TMP_ROOT:-}" && -d "${CI_TMP_ROOT}" ]]; then
    rm -rf "${CI_TMP_ROOT}"
  fi
  CI_COMPOSE_PROJECT=""
}

_ci_start_local_services() {
  if ! command -v docker >/dev/null 2>&1; then
    echo "ERROR: Docker is required for local CI but was not found on PATH." >&2
    return 1
  fi
  if ! docker info >/dev/null 2>&1; then
    echo "ERROR: Docker daemon is not available." >&2
    return 1
  fi

  CI_COMPOSE_PROJECT="q-backend-ci-$$-${RANDOM}"
  CI_COMPOSE_FILES=(-f docker-compose.ci.yml)
  if systemctl status ci-docker.slice >/dev/null 2>&1; then
    CI_COMPOSE_FILES+=(-f docker-compose.ci-slice.yml)
  fi

  # Install cleanup before the first command that can create Compose resources.
  # Signals and unexpected command failures during startup must clean this project.
  trap _ci_cleanup_local_services EXIT
  trap 'exit 130' INT
  trap 'exit 143' TERM
  CI_TMP_ROOT="$(mktemp -d "${TMPDIR:-/tmp}/q-backend-ci.XXXXXX")"

  export Q_DATA_LAKE_ROOT="${CI_TMP_ROOT}/lake"
  export Q_MARKET_DATA_ROOT="${CI_TMP_ROOT}/market"
  export Q_TICK_CACHE_DIR="${CI_TMP_ROOT}/tick_cache"
  export Q_RUNTIME_CONFIG_PATH="${CI_TMP_ROOT}/runtime_config.json"
  mkdir -p "${Q_DATA_LAKE_ROOT}" "${Q_MARKET_DATA_ROOT}" "${Q_TICK_CACHE_DIR}"

  export Q_CI_ISOLATED=1
  unset Q_DATABASE_URL Q_REDIS_URL

  echo "==> Starting disposable CI services (Compose project ${CI_COMPOSE_PROJECT})..."
  if ! _ci_compose up -d --wait postgres redis; then
    echo "ERROR: CI Postgres/Redis failed to become healthy." >&2
    _ci_cleanup_local_services
    return 1
  fi

  local pg_port redis_port
  pg_port="$(_ci_compose port postgres 5432 | awk -F: '{print $NF}')"
  redis_port="$(_ci_compose port redis 6379 | awk -F: '{print $NF}')"
  if [[ -z "$pg_port" || -z "$redis_port" ]]; then
    echo "ERROR: Could not resolve published CI service ports." >&2
    _ci_cleanup_local_services
    return 1
  fi

  export Q_DATABASE_URL="postgresql+psycopg://postgres:password@127.0.0.1:${pg_port}/q_storage"
  export Q_REDIS_URL="redis://127.0.0.1:${redis_port}/0"

  if [[ "${Q_CI_PREFLIGHT_ONLY:-0}" != "1" ]]; then
    if ! _ci_verify_service_url "Postgres" "${Q_DATABASE_URL}"; then
      _ci_cleanup_local_services
      return 1
    fi
    if ! _ci_verify_service_url "Redis" "${Q_REDIS_URL}"; then
      _ci_cleanup_local_services
      return 1
    fi
  fi

  trap _ci_cleanup_local_services EXIT INT TERM
  echo "==> CI database URL: ${Q_DATABASE_URL}"
  echo "==> CI Redis URL: ${Q_REDIS_URL}"
  echo "==> CI services ready (Postgres ${pg_port}, Redis ${redis_port})."
}

_ci_preflight_hosted() {
  if [[ -z "${Q_DATABASE_URL:-}" || -z "${Q_REDIS_URL:-}" ]]; then
    echo "ERROR: Hosted CI requires explicit Q_DATABASE_URL and Q_REDIS_URL." >&2
    return 1
  fi
  if [[ "${Q_DATABASE_URL}" != "${HOSTED_DATABASE_URL}" ||
        "${Q_REDIS_URL}" != "${HOSTED_REDIS_URL}" ]]; then
    echo "ERROR: Hosted CI must use its job-scoped Postgres/Redis service URLs." >&2
    return 1
  fi
  export Q_CI_ISOLATED=1
  if ! _ci_verify_service_url "Postgres" "${Q_DATABASE_URL}"; then
    return 1
  fi
  if ! _ci_verify_service_url "Redis" "${Q_REDIS_URL}"; then
    return 1
  fi
  echo "==> Hosted CI service endpoints verified."
}

_ci_preflight_local() {
  # Ambient CI=true or development URLs must not redirect local runs to shared services.
  if [[ -n "${Q_DATABASE_URL:-}" || -n "${Q_REDIS_URL:-}" ]]; then
    if [[ "${Q_DATABASE_URL:-}" == "$DEV_DATABASE_URL" || "${Q_REDIS_URL:-}" == "$DEV_REDIS_URL" ]]; then
      echo "ERROR: Refusing to reuse development Postgres/Redis URLs for local CI." >&2
      return 1
    fi
    echo "ERROR: Local CI ignores ambient Q_DATABASE_URL/Q_REDIS_URL; unset them or use hosted CI." >&2
    return 1
  fi
  _ci_start_local_services
}

run_ci_pipeline() {
  echo "=========================================="
  echo " Starting q_backend CI Pipeline"
  echo "=========================================="

  if _ci_hosted_mode; then
    _ci_preflight_hosted
  else
    _ci_preflight_local
  fi

  if [[ "${Q_CI_PREFLIGHT_ONLY:-0}" == "1" ]]; then
    echo "==> Q_CI_PREFLIGHT_ONLY set; skipping validation stages."
    return 0
  fi

  echo "==> Checking vendored contracts (make contracts-check)..."
  make contracts-check

  echo "==> Applying database migrations (alembic upgrade head)..."
  uv run alembic upgrade head

  echo "==> Running linter (ruff check .)..."
  uv run ruff check .

  echo "==> Checking code formatting (black --check .)..."
  uv run black --check .

  CPUS="$(nproc 2>/dev/null || echo 2)"
  DEFAULT_WORKERS=$(( CPUS / 2 ))
  (( DEFAULT_WORKERS > 12 )) && DEFAULT_WORKERS=12
  (( DEFAULT_WORKERS < 1 )) && DEFAULT_WORKERS=1
  PYTEST_WORKERS="${PYTEST_WORKERS:-$DEFAULT_WORKERS}"
  for var in OMP_NUM_THREADS OPENBLAS_NUM_THREADS MKL_NUM_THREADS NUMEXPR_NUM_THREADS NUMBA_NUM_THREADS; do
    export "$var=${!var:-1}"
  done
  NICE=()
  if ! _ci_hosted_mode && command -v nice >/dev/null 2>&1; then
    NICE=(nice -n 10)
    command -v ionice >/dev/null 2>&1 && NICE=(ionice -c 3 "${NICE[@]}")
  fi

  echo "==> Running unit tests (pytest, ${PYTEST_WORKERS} workers)..."
  "${NICE[@]}" uv run pytest -n "$PYTEST_WORKERS" --dist loadfile -m "not integration"

  echo "==> Running integration tests (pytest, serial)..."
  "${NICE[@]}" uv run pytest -m integration

  echo "=========================================="
  echo " All q_backend CI checks passed successfully! "
  echo "=========================================="
}

if [[ "${BASH_SOURCE[0]}" == "${0}" ]]; then
  run_ci_pipeline "$@"
fi
