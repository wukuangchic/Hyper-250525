#!/bin/bash

set -u

APP_DIR="/root/hyper-feishu-history-sync"
cd "$APP_DIR" || exit 1

echo "[$(date '+%F %T %Z')] sync started"
/usr/bin/python3 "$APP_DIR/sync_history_to_feishu.py"
STATUS=$?
echo "[$(date '+%F %T %Z')] sync finished status=$STATUS"
exit "$STATUS"
