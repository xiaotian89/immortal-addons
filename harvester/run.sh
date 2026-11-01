#!/bin/sh
# 169bt 采集器启动脚本
# 所有配置都走环境变量（容器内用 compose 的 environment 覆盖），不改 harvester.py

export SITE_BASE="${SITE_BASE:-https://169bt.com}"
export FETCH_PROXY="${FETCH_PROXY:-}"              # 留空 = 直连；例 http://代理地址:7890
export IMG_PROXY_BASE="${IMG_PROXY_BASE:-}"        # 图片改写前缀；留空 = 不改写图片地址

# 板块：fid:名称，逗号分隔
#   45=BT有码区  137=BT无码区  93=BT国产区  160=BT合集专区  192=4K UHD
#   138=BT欧美区 133=BT动漫区  211=有码原创  212=无码原创  210=国产原创
export BOARDS="${BOARDS:-45:BT有码区,137:BT无码区,93:BT国产区,160:BT合集专区,192:4K UHD,138:BT欧美区,133:BT动漫区,211:有码原创,212:无码原创,210:国产原创}"

export PAGES="${PAGES:-1}"
export MAX_POSTS="${MAX_POSTS:-24}"
export DELAY="${DELAY:-0.6}"
export DRY_RUN="${DRY_RUN:-0}"                     # 0=写库 1=只打印

export DB_HOST="${DB_HOST:-postgres}"
export DB_PORT="${DB_PORT:-5432}"
export DB_USER="${DB_USER:-immortal}"
export DB_PASSWORD="${DB_PASSWORD:-}"
export DB_NAME="${DB_NAME:-immortal_db}"

LOG_DIR="${LOG_DIR:-/app/logs}"
mkdir -p "$LOG_DIR"
LOG_FILE="$LOG_DIR/harvester.log"

cd "$(dirname "$0")" || exit 1
echo "===== $(date '+%Y-%m-%d %H:%M:%S') 开始 =====" >> "$LOG_FILE"
python3 -u harvester.py >> "$LOG_FILE" 2>&1
RC=$?
echo "===== $(date '+%Y-%m-%d %H:%M:%S') 结束（退出码 $RC） =====" >> "$LOG_FILE"

# 日志超过 2MB 就截断，避免无限增长
if [ -f "$LOG_FILE" ] && [ "$(stat -c%s "$LOG_FILE" 2>/dev/null || echo 0)" -gt 2097152 ]; then
    tail -c 1048576 "$LOG_FILE" > "$LOG_FILE.tmp" && mv "$LOG_FILE.tmp" "$LOG_FILE"
fi

exit $RC
