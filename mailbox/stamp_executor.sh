#!/usr/bin/env bash
# stamp_executor.sh — 盖戳入口（薄壳）。核心逻辑在 devtools/stamp_core.py。
#
# 用法（本脚本与 config.ini 同目录的部署形态下，零参数）:
#   bash stamp_executor.sh           # 盖戳（幂等）
#   bash stamp_executor.sh --check   # 只检查
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
PY="${PYTHON:-python}"

exec "$PY" "$REPO_ROOT/devtools/stamp_core.py" --mailbox "$SCRIPT_DIR" "$@"
