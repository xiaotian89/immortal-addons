# immortal 外挂组件（不改动原镜像）

原版 `envyafish/immortal` 镜像只提供站点本体 + PostgreSQL。本仓库把**两个可选增强组件**单独打包成容器，挂到同一套数据库/缓存上，**不修改、不重建原镜像**。

| 组件 | 作用 | 端口 |
|---|---|---|
| `imgproxy` | 图片缓存代理 + 域名级熔断 | 8002 |
| `harvester` | 新源（169bt 论坛）采集器，直写 article 表 | 无（定时任务） |

---

## 零、部署方式：一个 compose，三个容器

**给别人用就拷这一个文件：`docker-compose.addons.yml`**

一次起三个容器：

| 容器 | 作用 |
|---|---|
| `imgproxy` | 图片缓存代理 |
| `harvester` | 新源采集器 |
| `imgrewrite` | 图片地址改写 |

```bash
git clone https://github.com/xiaotian89/immortal-addons.git
cd immortal-addons

cp .env.example .env
vi .env                      # 改 DB_PASSWORD、IMG_PROXY_BASE、UPSTREAM_PROXY

docker compose -f docker-compose.addons.yml up -d
```

起来后：

```bash
curl http://<你的IP>:8002/__stats     # 图片代理状态
docker logs -f immortal-harvester     # 采集器日志
docker logs -f immortal-imgrewrite    # 改写日志
```

### 其它文件

| 文件 | 容器数 | 用途 |
|---|---|---|
| `docker-compose.addons.yml` | **3** | **推荐，一条命令全装** |
| `docker-compose.imgproxy.yml` | 1 | 只装图片代理（单独排查用） |
| `docker-compose.harvester.yml` | 1 | 只装采集器（单独排查用） |
| `docker-compose.imgrewrite.yml` | 1 | 只装改写（单独排查用） |
| `docker-compose.yml` | 3 | 老版本，内容和 addons 相同 |

> **`DB_HOST` 怎么填**：和原版 immortal 跑在同一个 compose 网络里就写服务名 `postgres`；分开部署写宿主机真实 IP（注意不能写 `127.0.0.1`，那是容器自己）。

> **容器名冲突**：默认名是 `immortal-imgproxy` / `immortal-harvester` / `immortal-imgrewrite`。机器上已有同名容器时，在 `.env` 里改 `IMGPROXY_NAME` / `HARVESTER_NAME` / `IMGREWRITE_NAME`。

---

## 零·附、`.env` 关键项

```ini
DB_HOST=postgres           # 和原版同网络就写服务名，分开部署写真实 IP
DB_PORT=5432
DB_PASSWORD=改成你的密码
IMG_PROXY_BASE=http://192.168.1.10:8002   # 浏览器访问用的地址，必须是真实 IP
UPSTREAM_PROXY=            # 图床要翻就填 http 代理，全都能直连就留空
```

> `IMG_PROXY_BASE` 千万别写 `127.0.0.1` 或容器名——这个地址会被写进数据库，由**浏览器的电脑/手机**去访问。

> **镜像地址**：`ghcr.io/xiaotian89/immortal-addons-imgproxy` 与 `ghcr.io/xiaotian89/immortal-addons-harvester`，**公开可拉，无需登录**。
> 镜像由 GitHub Actions 在每次代码变更后自动构建，见 `.github/workflows/build.yml`。

---

## 一、图片代理 imgproxy

把各图床的图片缓存到本地磁盘，并对已经死掉的图床做熔断，避免每次翻页都等超时。

**能力**

- 缓存命中直接返回，冷启动回源后落盘（默认缓存 180 天，上限 2GB）
- **域名级熔断**：某图床连续失败达到阈值就整站秒回 404，不再逐张试错
- **后缀熔断**：`DEAD_DOMAINS` 里以 `.` 开头（如 `.imagehaha.com`）匹配整族子域
- **负缓存**：失败的 URL 让浏览器短期别再来问
- **Referer 自适应**：不同图床防盗链规则不同，默认用图片自己的域名做 Referer
- **按域名分代理端口**：机场/自建节点对同一图床连通性不同，可分别指定
- 连接池、单飞（同 URL 并发只回源一次）、总时限兜底
- `/__stats` 输出命中率、熔断域名、失败榜

**运行**

```bash
docker run -d --name immortal-imgproxy \
  --network host \
  -v /你的路径/imgcache:/cache \
  -e PORT=8002 \
  -e UPSTREAM_PROXY=          `# 可选：http 代理地址，留空=直连` \
  ghcr.io/xiaotian89/immortal-addons-imgproxy:5.5
```

> 用 `--network host` 是为了绕开 Docker bridge 的 PMTU 问题，TLS 握手能从 3~4 秒降到 0.5 秒。若你的环境必须用 bridge，去掉 `--network host` 改 `-p 8002:8002`，速度会慢一些。

**接入站点**：把数据库里的图片地址改写成 `http://<代理地址>:8002/<域名>/<路径>`。仓库内 `rewrite.sql` 是增量改写脚本，`rewrite_full.sql` 用于首次全量。

验证：`curl http://127.0.0.1:8002/__stats`

---

## 二、新源采集器 harvester

论坛类站点（169bt）的帖子没有 API，靠页面解析入库。直接写 immortal 的 `article` 表，**不改动原站代码**。

**能力**

- 多板块批量抓取，去重靠 `(website, tid)`
- 代理可配（论坛偶有封锁）
- 图片地址可自动改写成图片代理前缀
- `DRY_RUN=1` 只打印不写库，方便先验证

**运行**

```bash
docker run -d --name immortal-harvester \
  -e DB_HOST=你的PG地址 \
  -e DB_PORT=5432 \
  -e DB_USER=immortal \
  -e DB_PASSWORD=你的密码 \
  -e DB_NAME=immortal_db \
  -e FETCH_PROXY=http://代理地址:7890 \
  -e IMG_PROXY_BASE=http://你的图片代理:8002 \
  -e INTERVAL=1800 \
  -v /你的路径/logs:/app/logs \
  ghcr.io/xiaotian89/immortal-addons-harvester:1.0
```

**主要环境变量**

| 变量 | 默认 | 说明 |
|---|---|---|
| `INTERVAL` | 1800 | 两轮采集间隔（秒） |
| `RUN_ONCE` | 0 | 设 1 则只跑一次就退出，配合外部 cron |
| `BOARDS` | 10 个板块 | `fid:名称` 逗号分隔 |
| `PAGES` | 1 | 每板块抓几页 |
| `MAX_POSTS` | 24 | 单轮最多入库条数 |
| `DELAY` | 0.6 | 请求间隔（秒），太激进会被封 |
| `DRY_RUN` | 0 | 1 = 只打印不写库 |
| `FETCH_PROXY` | 空 | 抓取用 HTTP 代理，留空直连 |
| `IMG_PROXY_BASE` | 空 | 图片地址改写前缀，留空不改写 |

验证：`docker logs -f immortal-harvester`，日志同时在 `/app/logs/harvester.log`

---

## 三、和原版一起跑

仓库里的 `docker-compose.yml` **只含这两个外挂组件**，前提是你已经有一套 immortal 在跑。

如果你的 immortal 还没部署，把原版的 `immortal` + `postgres` 两个 service 直接并进同一个 compose 文件即可——原版镜像和它的环境变量一行都不用改，只是多了两个 service。关键点：

- `harvester` 的 `DB_HOST` 指向原版的 PG 服务名（默认 `postgres`）
- `imgrewrite` 的 `DB_HOST` 同上
- `imgproxy` 用 `network_mode: host`，所以**不要**给它写 `ports:`

---

## 四、自己构建

不想依赖作者的镜像，就自己构建：

```bash
# 图片代理（几十秒）
cd imgproxy && docker build -t 你的名字/immortal-imgproxy:5.5 .

# 采集器（国内网络默认走阿里源；国外用官方源去掉 ARG 即可）
cd ../harvester && docker build -t 你的名字/immortal-harvester:1.0 .
```

然后把 `.env` 里的 `IMGPROXY_IMAGE` / `HARVESTER_IMAGE` 改成自己的镜像名。

构建采集器时如果 apt 或 pip 很慢，可指定源：

```bash
docker build --build-arg APT_MIRROR=deb.debian.org \
             --build-arg PIP_INDEX=https://pypi.org/simple/ \
             -t 你的名字/immortal-harvester:1.0 .
```

---

## 五、限制

- 只支持 PostgreSQL（原版用的就是 PG）
- harvester 依赖 169bt 的页面结构，站方改版需要更新解析逻辑
- imgproxy 的熔断名单是实测出来的，换环境后应重新验证：某域名整批 404 时，先用同参数手测单张图，再决定是否判死
- 图片代理默认不缓存 HTML 错误页（防止把错误页存成 .jpg）

---

## 许可

自用工具，无担保。图片代理规则与图床实测结论基于特定网络环境，换环境需重新验证。
