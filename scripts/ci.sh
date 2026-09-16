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

# Determine repository root
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

echo "=========================================="
echo " Starting q_backend CI Pipeline"
echo "=========================================="

# 1. Ensure required services are accessible (PostgreSQL & Redis)
if [ -z "${CI:-}" ]; then
  echo "==> Checking local Postgres (5434) and Redis (6380)..."
  SERVICES_READY=true
  if ! (echo > /dev/tcp/localhost/5434) >/dev/null 2>&1; then
    SERVICES_READY=false
  fi
  if ! (echo > /dev/tcp/localhost/6380) >/dev/null 2>&1; then
    SERVICES_READY=false
  fi

  if [ "$SERVICES_READY" = false ]; then
    UNITS_INSTALLED=false
    if command -v systemctl >/dev/null 2>&1; then
      if systemctl --user cat q-postgres.service >/dev/null 2>&1 && systemctl --user cat q-redis.service >/dev/null 2>&1; then
        UNITS_INSTALLED=true
      fi
    fi

    if [ "$UNITS_INSTALLED" = true ]; then
      echo "Services not running. Starting systemd user units (q-postgres, q-redis)..."
      systemctl --user start q-postgres.service q-redis.service
      echo "Waiting for PostgreSQL to be ready..."
      for i in {1..30}; do
        if (echo > /dev/tcp/localhost/5434) >/dev/null 2>&1; then
          echo "PostgreSQL is ready."
          break
        fi
        sleep 1
      done
    else
      echo "Services not running. Starting Docker services (postgres, redis)..."
      # Prefer ci-docker.slice for CI containers when the host slice exists;
      # fall back to plain compose so machines without the slice still work.
      if systemctl status ci-docker.slice >/dev/null 2>&1; then
        docker compose -f docker-compose.yml -f docker-compose.ci.yml up -d postgres redis
      else
        docker compose up -d postgres redis
      fi
      echo "Waiting for PostgreSQL to be ready..."
      for i in {1..30}; do
        if docker compose exec -T postgres pg_isready -U postgres >/dev/null 2>&1; then
          echo "PostgreSQL is ready."
          break
        fi
        sleep 1
      done
    fi
  else
    echo "Local services are reachable."
  fi
fi

# 2. Vendored contract drift
# Runs here rather than only in the GitHub workflow, so that a pre-push hook
# catches drift instead of leaving it for CI to find. Needs to reach the
# contracts repository; point CONTRACTS_REPO at a local clone when offline.
echo "==> Checking vendored contracts (make contracts-check)..."
make contracts-check

# 3. Database Migrations
echo "==> Applying database migrations (alembic upgrade head)..."
uv run alembic upgrade head

# 4. Linting
echo "==> Running linter (ruff check .)..."
uv run ruff check .

# 5. Code Formatting Check
echo "==> Checking code formatting (black --check .)..."
uv run black --check .

# 6. Automated Tests
# Unit tests run in parallel under pytest-xdist; integration tests share the live
# Postgres/Redis (they flushdb/flushall), so they run serially afterwards.
# Default to half the logical CPUs (capped at 12) so the desktop stays responsive;
# override with PYTEST_WORKERS=N. Numeric libraries get one thread per worker —
# parallelism comes from the xdist processes, not BLAS/OpenMP/numba threads.
CPUS="$(nproc 2>/dev/null || echo 2)"
DEFAULT_WORKERS=$(( CPUS / 2 ))
(( DEFAULT_WORKERS > 12 )) && DEFAULT_WORKERS=12
(( DEFAULT_WORKERS < 1 )) && DEFAULT_WORKERS=1
PYTEST_WORKERS="${PYTEST_WORKERS:-$DEFAULT_WORKERS}"
for var in OMP_NUM_THREADS OPENBLAS_NUM_THREADS MKL_NUM_THREADS NUMEXPR_NUM_THREADS NUMBA_NUM_THREADS; do
  export "$var=${!var:-1}"
done
# Lower CPU/IO priority locally so tests yield to interactive work.
NICE=()
if [ -z "${CI:-}" ] && command -v nice >/dev/null 2>&1; then
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
