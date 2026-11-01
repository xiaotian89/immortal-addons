FROM postgres:16-alpine

LABEL org.opencontainers.image.title="immortal-addons-imgrewrite" \
      org.opencontainers.image.description="图片地址改写：把库里远程图片 URL 指向本地图片代理，SQL 模板已内置于镜像" \
      org.opencontainers.image.version="1.0" \
      org.opencontainers.image.source="https://github.com/xiaotian89/immortal-addons"

# SQL 模板打进镜像，运行时不依赖宿主机文件
COPY rewrite.sql /app/rewrite.tpl.sql
COPY rewrite_full.sql /app/rewrite_full.sql
COPY entrypoint.sh /app/entrypoint.sh

RUN chmod +x /app/entrypoint.sh

CMD ["/app/entrypoint.sh"]
