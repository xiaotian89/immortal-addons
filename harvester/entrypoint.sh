#!/bin/sh
# 容器入口：按 INTERVAL 秒循环跑采集，或 RUN_ONCE=1 只跑一次
set -u

INTERVAL="${INTERVAL:-1800}"
LOG_DIR="${LOG_DIR:-/app/logs}"
mkdir -p "$LOG_DIR"

echo "[entrypoint] 启动采集器 间隔=${INTERVAL}秒 单次=${RUN_ONCE:-0}"

if [ "${RUN_ONCE:-0}" = "1" ]; then
    /app/run.sh
    exit $?
fi

while true; do
    /app/run.sh
    echo "[entrypoint] 本轮结束，等待 ${INTERVAL} 秒"
    sleep "$INTERVAL"
done
