FROM python:3.11-slim

LABEL org.opencontainers.image.title="immortal-imgproxy" \
      org.opencontainers.image.description="图片缓存代理：域名级熔断、负缓存、按域名分代理端口、后缀熔断，适配修仙/immortal 图床链路" \
      org.opencontainers.image.version="5.5" \
      org.opencontainers.image.source="https://github.com/xiaotian89/immortal-addons"

# 只用到标准库，不装任何 pip 包；curl 是静态编译二进制，随镜像一起打包
RUN mkdir -p /app /cache

COPY curl /app/curl
COPY imgproxy.py /app/imgproxy.py
COPY rewrite.sql /app/rewrite.sql
COPY rewrite_full.sql /app/rewrite_full.sql

RUN chmod +x /app/curl && python -c "import ast,sys; ast.parse(open('/app/imgproxy.py').read())"

WORKDIR /app

# 缓存目录（建议挂出来，否则重建容器缓存全丢）
VOLUME ["/cache"]

# 8002 只监听本机/容器内，用 host 网络时这个 EXPOSE 仅作声明
EXPOSE 8002

# 健康检查：/__stats 返回 JSON
HEALTHCHECK --interval=60s --timeout=10s --start-period=10s --retries=3 \
  CMD python -c "import urllib.request,json,sys; \
d=json.load(urllib.request.urlopen('http://127.0.0.1:8002/__stats',timeout=8)); \
sys.exit(0 if 'hits' in d or 'hit' in d else 1)" || exit 1

CMD ["python", "/app/imgproxy.py"]
