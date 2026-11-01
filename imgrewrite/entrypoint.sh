#!/bin/sh
# 图片地址改写：把库里远程图片 URL 指向本地图片代理
# 所有配置走环境变量，SQL 模板已内置镜像，不依赖任何挂载文件
set -u

IMG_PROXY_BASE="${IMG_PROXY_BASE:?请设置 IMG_PROXY_BASE，例如 http://192.168.1.10:8002}"
DB_HOST="${DB_HOST:-postgres}"
DB_PORT="${DB_PORT:-5432}"
DB_USER="${DB_USER:-immortal}"
DB_NAME="${DB_NAME:-immortal_db}"
INTERVAL="${REWRITE_INTERVAL:-300}"

# 把模板里的占位符换成真实地址
sed "s#http://IMGPROXY_HOST:8002#${IMG_PROXY_BASE}#g" /app/rewrite.tpl.sql > /tmp/rewrite.sql

echo "[imgrewrite] 目标地址=${IMG_PROXY_BASE} 间隔=${INTERVAL}秒"

# 等数据库可用（最多 60 秒），避免容器启动顺序问题
i=0
while [ $i -lt 12 ]; do
    if pg_isready -h "$DB_HOST" -p "$DB_PORT" -U "$DB_USER" -q 2>/dev/null; then
        break
    fi
    i=$((i + 1))
    echo "[imgrewrite] 等待数据库就绪 ${i}/12"
    sleep 5
done

while true; do
    echo "[imgrewrite] $(date '+%Y-%m-%d %H:%M:%S') 执行改写"
    psql -h "$DB_HOST" -p "$DB_PORT" -U "$DB_USER" -d "$DB_NAME" \
         -v ON_ERROR_STOP=1 -q -f /tmp/rewrite.sql && \
        echo "[imgrewrite] 完成" || echo "[imgrewrite] 失败，稍后重试"
    sleep "$INTERVAL"
done
