#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
if [ ! -d .venv ]; then
  python3 -m venv .venv
fi
.venv/bin/pip install -q -r requirements.txt
# 生效口径批次状态落盘，聚合中断/进程重启后仍可核对进度与实际发布时点。
# 设 EFFECTIVE_BATCH_STATE_PATH=:memory: 可关闭。
export EFFECTIVE_BATCH_STATE_PATH="${EFFECTIVE_BATCH_STATE_PATH:-.data/effective_batch_state.json}"
mkdir -p "$(dirname "$EFFECTIVE_BATCH_STATE_PATH")"
exec .venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8012
