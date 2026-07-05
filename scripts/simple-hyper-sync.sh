#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="${PROJECT_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
SERVICE="${SIMPLE_HYPER_SERVICE:-simple-hyper.service}"
SOURCE_URL="${SIMPLE_HYPER_SYNC_URL:-https://codeload.github.com/wukuangchic/Hyper-250525/tar.gz/refs/heads/main}"
STATE_FILE="${SIMPLE_HYPER_SYNC_STATE_FILE:-/var/lib/simple-hyper-sync/main.etag}"
LOCK_FILE="${SIMPLE_HYPER_SYNC_LOCK_FILE:-/run/simple-hyper-sync.lock}"

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
  --exclude '.venv/' \
  --exclude 'logs/' \
  --exclude '.env' \
  --exclude '.simple-hyper-sync.etag' \
  --exclude '.git/' \
  --exclude 'server_batch.json' \
  --exclude 'server_batch.lock' \
  --exclude '.server_batch.json.*.tmp' \
  "$source_dir"/ "$PROJECT_DIR"/

if [ -x "$PROJECT_DIR/.venv/bin/python" ]; then
  "$PROJECT_DIR/.venv/bin/python" -m pip install -r "$PROJECT_DIR/requirements.txt" >/dev/null
fi

if id simplehyper >/dev/null 2>&1; then
  chown simplehyper:simplehyper "$PROJECT_DIR" 2>/dev/null || true
  [ -f "$PROJECT_DIR/server_batch.json" ] && chown simplehyper:simplehyper "$PROJECT_DIR/server_batch.json" 2>/dev/null || true
  [ -f "$PROJECT_DIR/server_batch.lock" ] && chown simplehyper:simplehyper "$PROJECT_DIR/server_batch.lock" 2>/dev/null || true
  [ -d "$PROJECT_DIR/logs" ] && chown simplehyper:simplehyper "$PROJECT_DIR/logs" 2>/dev/null || true
  chmod 775 "$PROJECT_DIR" 2>/dev/null || true
  [ -f "$PROJECT_DIR/server_batch.json" ] && chmod 664 "$PROJECT_DIR/server_batch.json" 2>/dev/null || true
  [ -f "$PROJECT_DIR/server_batch.lock" ] && chmod 664 "$PROJECT_DIR/server_batch.lock" 2>/dev/null || true
  [ -d "$PROJECT_DIR/logs" ] && chmod 775 "$PROJECT_DIR/logs" 2>/dev/null || true
fi

printf '%s\n' "$target_etag" > "$STATE_FILE"
systemctl restart "$SERVICE"
echo "simple-hyper-sync: updated to $target_etag and restarted $SERVICE"
