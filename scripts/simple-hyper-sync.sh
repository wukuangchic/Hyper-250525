#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="${PROJECT_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
REMOTE="${SIMPLE_HYPER_SYNC_REMOTE:-origin}"
BRANCH="${SIMPLE_HYPER_SYNC_BRANCH:-main}"
SERVICE="${SIMPLE_HYPER_SERVICE:-simple-hyper.service}"
LOCK_FILE="${SIMPLE_HYPER_SYNC_LOCK_FILE:-/run/simple-hyper-sync.lock}"

exec 9>"$LOCK_FILE"
if ! flock -n 9; then
  echo "simple-hyper-sync: another run is already active"
  exit 0
fi

cd "$PROJECT_DIR"

if ! git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  echo "simple-hyper-sync: $PROJECT_DIR is not a git repository" >&2
  exit 1
fi

current_rev="$(git rev-parse HEAD)"
git fetch --prune "$REMOTE" "$BRANCH"
target_rev="$(git rev-parse "$REMOTE/$BRANCH")"

if [ "$current_rev" = "$target_rev" ]; then
  echo "simple-hyper-sync: already up to date at $target_rev"
  exit 0
fi

alias_backup=""
if [ -f coin_aliases.csv ]; then
  alias_backup="$(mktemp)"
  cp coin_aliases.csv "$alias_backup"
fi

git checkout -B "$BRANCH" "$REMOTE/$BRANCH" >/dev/null
git reset --hard "$REMOTE/$BRANCH" >/dev/null

if [ -n "$alias_backup" ] && [ -f "$alias_backup" ]; then
  cp "$alias_backup" coin_aliases.csv
  rm -f "$alias_backup"
fi

if [ -x .venv/bin/python ]; then
  .venv/bin/python -m pip install -r requirements.txt >/dev/null
fi

systemctl restart "$SERVICE"
echo "simple-hyper-sync: updated to $target_rev and restarted $SERVICE"
