#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="${PROJECT_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
SERVICE="${SIMPLE_HYPER_SERVICE:-simple-hyper.service}"
SOURCE_URL="${SIMPLE_HYPER_SYNC_URL:-https://codeload.github.com/wukuangchic/Hyper-250525/tar.gz/refs/heads/main}"
STATE_FILE="${SIMPLE_HYPER_SYNC_STATE_FILE:-/var/lib/simple-hyper-sync/main.etag}"
LOCK_FILE="${SIMPLE_HYPER_SYNC_LOCK_FILE:-/run/simple-hyper-sync.lock}"
RUNTIME_DIR="${SIMPLE_HYPER_STATE_DIR:-/var/lib/simple-hyper}"

mkdir -p "$(dirname "$STATE_FILE")"
exec 9>"$LOCK_FILE"
if ! flock -n 9; then
  echo "simple-hyper-sync: another run is already active"
  exit 0
fi

current_etag=""
if [ -f "$STATE_FILE" ]; then
  current_etag="$(tr -d '\r\n' < "$STATE_FILE")"
fi

headers="$(curl -fsSI --connect-timeout 15 --max-time 60 "$SOURCE_URL")"
target_etag="$(printf '%s\n' "$headers" | awk 'BEGIN{IGNORECASE=1} /^etag:/ {sub(/\r$/, "", $2); print $2; exit}')"

if [ -z "$target_etag" ]; then
  echo "simple-hyper-sync: unable to read GitHub tarball ETag" >&2
  exit 1
fi

if [ "$current_etag" = "$target_etag" ]; then
  echo "simple-hyper-sync: already up to date at $target_etag"
  exit 0
fi

tmp_root="$(mktemp -d)"
cleanup() {
  rm -rf "$tmp_root"
}
trap cleanup EXIT

curl -fsSL --connect-timeout 15 --max-time 120 "$SOURCE_URL" -o "$tmp_root/repo.tar.gz"
tar -xzf "$tmp_root/repo.tar.gz" -C "$tmp_root"

source_dir="$(find "$tmp_root" -mindepth 1 -maxdepth 1 -type d | head -n1)"
if [ -z "$source_dir" ] || [ ! -d "$source_dir" ]; then
  echo "simple-hyper-sync: failed to unpack GitHub tarball" >&2
  exit 1
fi

rsync -a --delete \
  --no-owner \
  --no-group \
  --exclude '.venv/' \
  --exclude 'logs/' \
  --exclude '.env' \
  --exclude 'simple-hyper.env' \
  --exclude '.simple-hyper-sync.etag' \
  --exclude '.git/' \
  --exclude 'server_batch.json' \
  --exclude 'server_batch.lock' \
  --exclude '.server_batch.json.*.tmp' \
  --exclude 'command_history.json' \
  --exclude '.command_history.json.tmp' \
  "$source_dir"/ "$PROJECT_DIR"/

if [ -x "$PROJECT_DIR/.venv/bin/python" ]; then
  if id simplehyper >/dev/null 2>&1; then
    chown -R simplehyper:simplehyper "$PROJECT_DIR/.venv"
    runuser -u simplehyper -- "$PROJECT_DIR/.venv/bin/python" -m pip install -r "$PROJECT_DIR/requirements.txt" >/dev/null
  else
    echo "simple-hyper-sync: simplehyper user is required for dependency installation" >&2
    exit 1
  fi
fi

if id simplehyper >/dev/null 2>&1; then
  install -d -o simplehyper -g simplehyper -m 0750 "$RUNTIME_DIR" "$RUNTIME_DIR/logs"
  for name in server_batch.json server_batch.lock command_history.json; do
    if [ -e "$PROJECT_DIR/$name" ] && [ ! -e "$RUNTIME_DIR/$name" ]; then
      mv "$PROJECT_DIR/$name" "$RUNTIME_DIR/$name"
    fi
  done
  if [ -d "$PROJECT_DIR/logs" ]; then
    rsync -a "$PROJECT_DIR/logs"/ "$RUNTIME_DIR/logs"/
    rm -rf "$PROJECT_DIR/logs"
  fi
  chown -R simplehyper:simplehyper "$RUNTIME_DIR"
  find "$RUNTIME_DIR" -type d -exec chmod 0750 {} +
  find "$RUNTIME_DIR" -type f -exec chmod 0640 {} +
fi

chown -R root:root "$PROJECT_DIR"
chmod -R u=rwX,go=rX "$PROJECT_DIR"
for env_file in "$PROJECT_DIR/.env" "$PROJECT_DIR/simple-hyper.env"; do
  if [ -f "$env_file" ] && id simplehyper >/dev/null 2>&1; then
    chown root:simplehyper "$env_file"
    chmod 0640 "$env_file"
  fi
done
systemctl restart "$SERVICE"
printf '%s\n' "$target_etag" > "$STATE_FILE"
echo "simple-hyper-sync: updated to $target_etag and restarted $SERVICE"
