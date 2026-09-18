#!/usr/bin/env bash
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

DEST_DIR="${HOME}/.config/systemd/user"
QUADLET_DEST="${HOME}/.config/containers/systemd"
NO_SYNC=false
UNINSTALL=false

while [[ $# -gt 0 ]]; do
  case "$1" in
    --dest)
      DEST_DIR="$2"
      shift 2
      ;;
    --quadlet-dest)
      QUADLET_DEST="$2"
      shift 2
      ;;
    --no-sync)
      NO_SYNC=true
      shift
      ;;
    --uninstall)
      UNINSTALL=true
      shift
      ;;
    -h|--help)
      echo "Usage: $0 [--dest DIR] [--quadlet-dest DIR] [--no-sync] [--uninstall]"
      exit 0
      ;;
    *)
      echo "Unknown argument: $1" >&2
      exit 1
      ;;
  esac
done

PG_PORT="${Q_UNITS_PG_PORT:-5434}"
REDIS_PORT="${Q_UNITS_REDIS_PORT:-6380}"

check_port() {
  local port="$1"
  (echo > /dev/tcp/127.0.0.1/"$port") >/dev/null 2>&1
}

is_unit_active() {
  local unit="$1"
  if command -v systemctl >/dev/null 2>&1; then
    systemctl --user is-active --quiet "$unit" 2>/dev/null
  else
    return 1
  fi
}

if [ "$UNINSTALL" = true ]; then
  echo "==> Uninstalling Q backend systemd units and quadlets..."
  rm -f "$DEST_DIR/q-migrate.service"
  rm -f "$DEST_DIR/q-api.service"
  rm -f "$DEST_DIR/q-outbox-relay.service"
  rm -f "$DEST_DIR/q-market-publisher.service"
  rm -f "$DEST_DIR/q-research-worker.service"
  rm -f "$DEST_DIR/q-execution-worker.service"
  rm -f "$DEST_DIR/q-backend.target"
  rm -f "$QUADLET_DEST/q-postgres.container"
  rm -f "$QUADLET_DEST/q-postgres-data.volume"
  rm -f "$QUADLET_DEST/q-redis.container"

  if command -v systemctl >/dev/null 2>&1 && [ "$DEST_DIR" = "$HOME/.config/systemd/user" ]; then
    systemctl --user daemon-reload 2>/dev/null || true
  fi
  echo "==> Uninstall complete."
  exit 0
fi

# Guard against port conflicts with non-unit processes (e.g. docker compose)
if check_port "$PG_PORT" && ! is_unit_active "q-postgres.service"; then
  echo "Error: PostgreSQL port $PG_PORT is held by an existing process." >&2
  echo "Stop existing containers with: docker compose stop postgres redis" >&2
  exit 1
fi
if check_port "$REDIS_PORT" && ! is_unit_active "q-redis.service"; then
  echo "Error: Redis port $REDIS_PORT is held by an existing process." >&2
  echo "Stop existing containers with: docker compose stop postgres redis" >&2
  exit 1
fi

if [ "$NO_SYNC" = false ]; then
  echo "==> Syncing dependencies (uv sync --frozen --no-dev)..."
  (cd "$REPO_ROOT" && uv sync --frozen --no-dev)
fi

mkdir -p "$DEST_DIR" "$QUADLET_DEST"

install_file_if_changed() {
  local src="$1"
  local dst="$2"
  if [ -f "$dst" ] && cmp -s "$src" "$dst"; then
    return 0
  fi
  cp "$src" "$dst"
}

render_and_install_if_changed() {
  local template="$1"
  local dst="$2"
  local tmp
  tmp=$(mktemp)
  sed "s|@Q_BACKEND_DIR@|$REPO_ROOT|g" "$template" > "$tmp"
  if [ -f "$dst" ] && cmp -s "$tmp" "$dst"; then
    rm -f "$tmp"
    return 0
  fi
  mv "$tmp" "$dst"
}

echo "==> Installing user units into $DEST_DIR..."
for t in "$REPO_ROOT"/deploy/systemd/user/*.service.in; do
  unit_name=$(basename "$t" .in)
  render_and_install_if_changed "$t" "$DEST_DIR/$unit_name"
done

install_file_if_changed "$REPO_ROOT/deploy/systemd/user/q-backend.target" "$DEST_DIR/q-backend.target"

echo "==> Installing quadlets into $QUADLET_DEST..."
for q in "$REPO_ROOT"/deploy/systemd/quadlet/*; do
  q_name=$(basename "$q")
  install_file_if_changed "$q" "$QUADLET_DEST/$q_name"
done

CONFIG_DIR="${HOME}/.config/q"
mkdir -p "$CONFIG_DIR"
if [ ! -f "$CONFIG_DIR/backend.env" ]; then
  cp "$REPO_ROOT/deploy/systemd/backend.env.example" "$CONFIG_DIR/backend.env"
fi
if [ ! -f "$CONFIG_DIR/postgres.env" ]; then
  cp "$REPO_ROOT/deploy/systemd/postgres.env.example" "$CONFIG_DIR/postgres.env"
fi

if command -v systemctl >/dev/null 2>&1 && [ "$DEST_DIR" = "$HOME/.config/systemd/user" ]; then
  systemctl --user daemon-reload 2>/dev/null || true
fi

echo "==> Q backend systemd units and quadlets installed successfully."
