#!/bin/zsh

set -u

SCRIPT_DIR="${0:A:h}"
cd "$SCRIPT_DIR" || exit 1

clear
echo "========================================"
echo "  Hyperliquid 交易与资金费历史同步到飞书"
echo "========================================"
echo "开始时间：$(date '+%Y-%m-%d %H:%M:%S')"
echo

python3 "$SCRIPT_DIR/sync_history_to_feishu.py"
STATUS=$?

echo
echo "结束时间：$(date '+%Y-%m-%d %H:%M:%S')"
if (( STATUS == 0 )); then
  echo "同步完成。"
else
  echo "同步失败，退出码：$STATUS"
fi
echo
read "REPLY?按回车键关闭窗口..."
exit "$STATUS"
