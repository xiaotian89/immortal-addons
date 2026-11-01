FROM python:3.11-slim

LABEL org.opencontainers.image.title="immortal-harvester" \
      org.opencontainers.image.description="新源采集器：抓取 169bt 论坛帖子直写 immortal 数据库，支持定时轮询" \
      org.opencontainers.image.version="1.0" \
      org.opencontainers.image.source="https://github.com/xiaotian89/immortal-addons"

# 国内构建用镜像源；构建时可用 --build-arg APT_MIRROR= 覆盖成官方源
ARG APT_MIRROR=mirrors.aliyun.com

RUN if [ -n "$APT_MIRROR" ]; then \
      sed -i "s|deb.debian.org|${APT_MIRROR}|g; s|security.debian.org|${APT_MIRROR}|g" \
        /etc/apt/sources.list.d/debian.sources 2>/dev/null || \
      sed -i "s|deb.debian.org|${APT_MIRROR}|g" /etc/apt/sources.list 2>/dev/null || true; \
    fi \
 && apt-get update \
 && apt-get install -y --no-install-recommends libpq5 tini \
 && rm -rf /var/lib/apt/lists/*

# psycopg2 用国内 PyPI 镜像（清华源在部分 DNS 环境下解析失败，阿里源更稳）
ARG PIP_INDEX=https://mirrors.aliyun.com/pypi/simple/
RUN pip install --no-cache-dir -i "$PIP_INDEX" "psycopg2-binary==2.9.9"

WORKDIR /app

COPY harvester.py /app/harvester.py
COPY anime_cover.py /app/anime_cover.py
COPY run.sh /app/run.sh
COPY entrypoint.sh /app/entrypoint.sh

RUN chmod +x /app/run.sh /app/entrypoint.sh \
 && python -c "import ast; ast.parse(open('/app/harvester.py').read())"

# 日志目录
VOLUME ["/app/logs"]

ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["/app/entrypoint.sh"]
