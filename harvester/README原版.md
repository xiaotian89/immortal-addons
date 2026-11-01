# 169bt 采集器（for immortal / 修仙）

把 **169bt（SEX169 论坛）** 的博彩/BT 资源采集进 **immortal（修仙）** 的数据库，填补 X1080X 停止更新后的新片缺口。

## 功能

- 采集指定板块的帖子：标题、番号、发布日期、文件大小、磁力链接、预览图
- 自动提取番号（如 `ABF-290`、`FC2-4630764`）供修仙匹配影片
- 预览图自动改写为修仙的图片代理地址，可直接显示
- 按帖子 ID 去重；重复运行只补齐缺失字段（磁力/图片/大小），不覆盖已有数据
- 无第三方 SDK 依赖，纯标准库 + PostgreSQL 驱动

## 部署

### 1. 准备

需要一个能运行的 **immortal（修仙）**，以及它的 PostgreSQL 连接信息（端口以你实际映射为准）。

宿主机需要 Python 3.8+ 和 `psycopg2`：

```bash
python3 -m pip install --break-system-packages psycopg2-binary
```

### 2. 放置文件

```bash
mkdir -p /vol1/1000/tools/169bt-harvester
# 把 harvester.py 和 run.sh 放进该目录
chmod +x run.sh
```

### 3. 改配置

编辑 `run.sh` 顶部的环境变量，只改这几项：

| 变量 | 说明 |
|---|---|
| `FETCH_PROXY` | 访问论坛用的代理，没有就留空 |
| `IMG_PROXY_BASE` | 修仙图片代理地址，通常是 `http://<NAS_IP>:8002` |
| `BOARDS` | 要采集的板块，格式 `fid:名称, fid:名称` |
| `DB_HOST` / `DB_PORT` / `DB_USER` / `DB_PASSWORD` / `DB_NAME` | 修仙数据库连接 |

### 4. 试跑

```bash
bash run.sh
tail -20 harvester.log
```

看到 `[db] 新增 N 条` 就成功了。

### 5. 定时运行

```bash
(crontab -l 2>/dev/null; echo "30 22 * * * /vol1/1000/tools/169bt-harvester/run.sh") | crontab -
```

## 板块 ID

| fid | 板块 |
|---|---|
| 45 | BT有码区 |
| 137 | BT无码区 |
| 93 | BT国产区 |
| 160 | BT合集专区 |
| 138 | BT欧美区 |
| 133 | BT动漫区 |
| 211 | 有码原创 |
| 212 | 无码原创 |
| 210 | 国产原创 |
| 192 | 4K UHD |

站点改版后 ID 可能变化，打开对应板块页看 `fid=` 参数即可。

## 参数

| 变量 | 默认 | 说明 |
|---|---|---|
| `PAGES` | 1 | 每个板块抓几页 |
| `MAX_POSTS` | 15 | 每个板块最多处理几帖 |
| `DELAY` | 1.0 | 每帖间隔秒数 |
| `DRY_RUN` | 1 | 1 = 只打印不写库，0 = 写库 |
| `KEEP_GIF` | 0 | 1 = 保留 gif 图片（默认过滤论坛装饰图） |

## 限制

- **磁力覆盖率约 70~90%**：论坛有一小部分帖子用网盘或附件形式发布，没有磁力
- **需要代理**：部分网络环境无法直连该站
- **板块 ID 会变**：站点改版后需要手动更新 `BOARDS`
- **仅供个人媒体库整理使用**
