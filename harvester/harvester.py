#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
169bt(SEX169) → immortal 采集器
只依赖 Python 标准库。DRY_RUN=1（默认）只打印，不写库。

环境变量：
  SITE_BASE      站点地址，默认 https://169bt.com
  FETCH_PROXY    抓取用 HTTP 代理，留空=直连，默认 http://192.168.2.70:7892
  IMG_PROXY_BASE 图片改写前缀，默认 http://192.168.2.70:8002
  BOARDS         板块列表，格式 fid:名称,fid:名称
  PAGES          每个板块抓几页（默认1）
  MAX_POSTS      每个板块最多处理几帖（0=不限，默认15）
  DELAY          每帖间隔秒数（默认1.0）
  DRY_RUN        1=只打印不写库（默认1）
"""
import os
import re
import sys
import json
import time
import urllib.request
import urllib.error

CFG = {
    "base": os.environ.get("SITE_BASE", "https://169bt.com").rstrip("/"),
    "proxy": os.environ.get("FETCH_PROXY", "http://192.168.2.70:7892").strip(),
    "img_base": os.environ.get("IMG_PROXY_BASE", "http://192.168.2.70:8002").rstrip("/"),
    "boards": os.environ.get("BOARDS", "45:BT有码区,137:BT无码区"),
    "pages": int(os.environ.get("PAGES", "1")),
    "limit": int(os.environ.get("MAX_POSTS", "15")),
    "delay": float(os.environ.get("DELAY", "1.0")),
    # 默认保留 gif：国产原创/破解区的预览图本身就是 gif，一律过滤会整板块缺图。
    # 真正的装饰图（签名档、分割线）靠「跨帖重复 ≥3 次就剔除」来清。
    "keep_gif": os.environ.get("KEEP_GIF", "1") == "1",
    "dry": os.environ.get("DRY_RUN", "1") == "1",
}

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")

_opener = None


def opener():
    global _opener
    if _opener is None:
        handlers = []
        if CFG["proxy"]:
            handlers.append(urllib.request.ProxyHandler(
                {"http": CFG["proxy"], "https": CFG["proxy"]}))
        _opener = urllib.request.build_opener(*handlers)
    return _opener


def fetch(url, timeout=25, retries=2):
    """抓页面，返回 str。失败返回空串。"""
    last = ""
    for attempt in range(retries + 1):
        req = urllib.request.Request(url, headers={
            "User-Agent": UA,
            "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
            "Accept-Encoding": "identity",
            "Accept-Language": "zh-CN,zh;q=0.9,ja;q=0.8",
        })
        try:
            with opener().open(req, timeout=timeout) as r:
                raw = r.read()
            for enc in ("utf-8", "gbk", "big5"):
                try:
                    return raw.decode(enc)
                except UnicodeDecodeError:
                    continue
            return raw.decode("utf-8", "replace")
        except Exception as exc:
            last = str(exc)
            if attempt < retries:
                time.sleep(2 + attempt * 2)
    print("  [warn] 抓取失败 %s -> %s" % (url, last), file=sys.stderr)
    return ""


def parse_board_list(html):
    """板块页 → 帖子列表 [{tid,title,author,date}]"""
    out = []
    parts = re.split(r'id="normalthread_(\d+)"', html)
    for i in range(1, len(parts) - 1, 2):
        tid, seg = parts[i], parts[i + 1][:4000]
        m = re.search(r'class="s xst"[^>]*>\s*([^<]{3,200}?)\s*<', seg)
        if not m:
            continue
        title = unescape(m.group(1))
        au = re.search(r'<cite>\s*<a[^>]*>([^<]{1,30})</a>', seg)
        dt = (re.search(r'<span title="([^"]{6,25})"', seg)
              or re.search(r'(\d{4}-\d{1,2}-\d{1,2}(?:\s+\d{1,2}:\d{2})?)', seg))
        out.append({
            "tid": tid,
            "title": title,
            "author": au.group(1).strip() if au else "",
            "date": dt.group(1).strip() if dt else "",
        })
    return out


def unescape(s):
    import html as _h
    return _h.unescape(re.sub(r"\s+", " ", s)).strip()


HASH_RE = re.compile(
    r"(?:驗證全碼|验证全码|驗證碼|验证码|驗證編號|验证编号|驗證編號|特征全码|特征全碼|特征碼|特征码"
    r"|特徵全碼|特徵碼|哈希验证|哈希驗證|哈希|HASH碼|HASH码|Hash|HASH)"
    r"[】\]】:：=︰\s]{0,5}([a-fA-F0-9]{40})")
# 兜底：正文里独立出现的 40 位十六进制串，基本只可能是 info hash
BARE_HASH_RE = re.compile(r"(?<![0-9a-zA-Z])([a-fA-F0-9]{40})(?![0-9a-zA-Z])")
MAGNET_RE = re.compile(r"magnet:\?xt=urn:btih:([a-zA-Z0-9]{32,40})")
SIZE_RE = re.compile(r"(?:影片大小|影片容量|文件大小|檔案大小|容量|大小)[】\]】:：=︰\s]{0,5}([\d.]+\s*(?:GB|GiB|MB|MiB|G|M))", re.I)
POST_DATE_RE = re.compile(r'<em id="authorposton\d*"[^>]*>.*?<span title="(\d{4}-\d{1,2}-\d{1,2})[\d :]*"', re.S)
POST_DATE_RE2 = re.compile(r'(?:發表於|发表于)\s*(\d{4}-\d{1,2}-\d{1,2})')
NUMBER_RES = [
    re.compile(r"\b(FC2)[-_ ]?(?:PPV[-_ ]?)?(\d{5,8})\b", re.I),
    # 论坛会给标题加楼号（477GRMO-365 = 楼号477 + 番号GRMO-365），先剥掉前置数字
    re.compile(r"^\d{2,4}([A-Z]{2,6})[-_ ]?(\d{2,6})\b", re.I),
    re.compile(r"(?<![A-Za-z0-9])([A-Z]{2,6})-?(\d{2,6})\b", re.I),
]


def extract_post_date(html):
    for rx in (POST_DATE_RE, POST_DATE_RE2):
        m = rx.search(html)
        if m:
            return m.group(1)
    return None


def extract_magnets(html, extra=""):
    """磁力 + 各种写法的验证码/哈希拼装，去重保序"""
    found = []
    for m in MAGNET_RE.findall(html):
        found.append(m.lower())
    for m in HASH_RE.findall(html):
        found.append(m.lower())
    # 兜底：正文里裸的 40 位 hex（不同板块字段名不统一：驗證編號 / HASH碼 / 特征全码…）
    for m in BARE_HASH_RE.findall(extra or ""):
        found.append(m.lower())
    seen, out = set(), []
    for h in found:
        if h not in seen and len(h) in (32, 40):
            seen.add(h)
            out.append("magnet:?xt=urn:btih:" + h)
    return out


def extract_number(title, body):
    hay = (title or "") + " " + (body or "")[:600]
    for rx in NUMBER_RES:
        for m in rx.finditer(hay):
            # 前面紧挨着数字说明是论坛楼号/流水号拼进标题的，跳过
            if m.start() > 0 and hay[m.start() - 1].isdigit():
                continue
            pre = m.group(1).upper()
            dig = m.group(2)
            if pre == "FC2":
                return "FC2-%s" % dig.lstrip("0") if dig.startswith("0") else "FC2-%s" % dig
            return "%s-%s" % (pre, dig)
    return None


UNIT = {"GB": 1024, "G": 1024, "GiB": 1024, "MB": 1, "M": 1, "MiB": 1}


def extract_size_mb(body):
    for m in SIZE_RE.finditer(body or ""):
        m2 = re.match(r"([\d.]+)\s*([A-Za-z]+)", m.group(1))
        if not m2:
            continue
        try:
            v = float(m2.group(1))
        except ValueError:
            continue
        return int(v * UNIT.get(m2.group(2).upper(), 1))
    return 0


def img_to_proxy(url):
    """把外链图片改写成图片代理地址（与库里现有格式一致）"""
    if not url:
        return None
    u = url.strip()
    if u.startswith("//"):
        u = "https:" + u
    if not u.startswith("http"):
        return None
    m = re.match(r"^https?://([^/]+)/(.+)$", u)
    if not m:
        return None
    return "%s/%s/%s" % (CFG["img_base"], m.group(1), m.group(2))


def parse_thread(html):
    t = re.search(r"<title>\s*([^<]{2,200}?)\s*</title>", html)
    title = unescape(t.group(1)) if t else ""
    title = re.sub(r"\s*[『「][^』」]{0,30}[』」]\s*-\s*SEX169.*$", "", title).strip()

    body = ""
    raw_body = ""
    m = re.search(r'<td class="t_f"[^>]*>(.*?)</td>', html, re.S)
    if m:
        raw_body = m.group(1)
        body = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", raw_body)).strip()

    # 图片只从正文区取：整页正则会把页头装饰图、表情、签名档全抓进来
    imgs = []
    scope = raw_body or ""
    for u in re.findall(r'(?:file|zoomfile|src)="([^"]+\.(?:jpg|jpeg|png|webp|gif))"', scope, re.I):
        if any(k in u for k in ("static/image", "smiley", "/common/", "online_member", "avatar")):
            continue
        # 论坛签名档/分割线几乎都是 gif，默认过滤（可用 KEEP_GIF=1 关掉）
        if not CFG["keep_gif"] and u.lower().split("?")[0].endswith(".gif"):
            continue
        p = img_to_proxy(u)
        if p and p not in imgs:
            imgs.append(p)

    return {
        "title": title,
        "body": body,
        "magnet": extract_magnets(html, raw_body),
        "images": imgs[:12],
        "size_mb": extract_size_mb(body),
        "date": extract_post_date(html),
    }


def parse_boards():
    out = []
    for item in CFG["boards"].split(","):
        item = item.strip()
        if not item or ":" not in item:
            continue
        fid, name = item.split(":", 1)
        out.append((fid.strip(), name.strip()))
    return out


def db_connect():
    """连 PostgreSQL；没装驱动时给出明确提示"""
    try:
        import psycopg2
    except ImportError:
        print("[fatal] 缺少 psycopg2：pip install psycopg2-binary", file=sys.stderr)
        sys.exit(2)
    return psycopg2.connect(
        host=os.environ.get("DB_HOST", "127.0.0.1"),
        port=int(os.environ.get("DB_PORT", "25432")),
        user=os.environ.get("DB_USER", "immortal"),
        password=os.environ.get("DB_PASSWORD", ""),
        dbname=os.environ.get("DB_NAME", "immortal_db"),
        connect_timeout=10,
    )


INSERT_SQL = """
INSERT INTO article
    (tid, title, match_number, publish_date, magnet, edk,
     preview_images, detail_url, size, section, category, website)
VALUES
    (%(tid)s, %(title)s, %(match_number)s, %(publish_date)s, %(magnet)s, %(edk)s,
     %(preview_images)s, %(detail_url)s, %(size)s, %(section)s, %(category)s, %(website)s)
ON CONFLICT (tid, website) DO UPDATE
SET magnet = CASE WHEN COALESCE(array_length(EXCLUDED.magnet, 1), 0) > 0
                  THEN EXCLUDED.magnet ELSE article.magnet END,
    preview_images = CASE WHEN COALESCE(array_length(EXCLUDED.preview_images, 1), 0) > 0
                          THEN EXCLUDED.preview_images ELSE article.preview_images END,
    size = CASE WHEN COALESCE(article.size, 0) = 0 THEN EXCLUDED.size ELSE article.size END,
    match_number = CASE
        WHEN article.match_number IS NULL OR article.match_number = '' THEN EXCLUDED.match_number
        WHEN article.match_number ~ '^[0-9]' THEN COALESCE(EXCLUDED.match_number, article.match_number)
        ELSE article.match_number END,
    publish_date = COALESCE(article.publish_date, EXCLUDED.publish_date)
"""


def write_db(results):
    conn = db_connect()
    added = updated = 0
    try:
        with conn, conn.cursor() as cur:
            tids = []
            for r in results:
                try:
                    r["tid"] = int(r["tid"])
                    tids.append(r["tid"])
                except (TypeError, ValueError):
                    r["tid"] = None
            existing = set()
            if tids:
                cur.execute("SELECT tid FROM article WHERE website = %s AND tid = ANY(%s)",
                            ("169bt", tids))
                existing = {row[0] for row in cur.fetchall()}
            for r in results:
                if r.get("tid") is None:
                    continue
                params = dict(r)
                params["edk"] = params.get("edk") or []
                params["magnet"] = params.get("magnet") or []
                params["preview_images"] = params.get("preview_images") or []
                params["size"] = int(params.get("size") or 0)
                cur.execute(INSERT_SQL, params)
                if r["tid"] in existing:
                    updated += 1
                else:
                    added += 1
    finally:
        conn.close()
    print("[db] 新增 %d 条，补齐/更新 %d 条" % (added, updated), file=sys.stderr)
    return added, updated


def main():
    boards = parse_boards()
    print("[cfg] 站点=%s 代理=%s 板块=%d 每板页数=%d 每帖上限=%s 模式=%s" % (
        CFG["base"], CFG["proxy"] or "直连", len(boards), CFG["pages"],
        CFG["limit"] or "不限", "只读" if CFG["dry"] else "写库"), file=sys.stderr)

    results = []
    for fid, name in boards:
        for page in range(1, CFG["pages"] + 1):
            url = "%s/forum.php?mod=forumdisplay&fid=%s&page=%d" % (CFG["base"], fid, page)
            html = fetch(url)
            if not html:
                continue
            posts = parse_board_list(html)
            real = [p for p in posts if p["title"] and not any(
                k in p["title"] for k in ("官方担保", "捐助会员", "永久地址", "严禁上传", "求色花堂"))]
            print("[board] %s(fid=%s) 第%d页: %d 帖（过滤广告后 %d）" % (
                name, fid, page, len(posts), len(real)), file=sys.stderr)
            if CFG["limit"]:
                real = real[:CFG["limit"]]
            for p in real:
                durl = "%s/forum.php?mod=viewthread&tid=%s" % (CFG["base"], p["tid"])
                th = fetch(durl, timeout=30)
                if not th:
                    continue
                info = parse_thread(th)
                rec = {
                    "website": "169bt",
                    "tid": p["tid"],
                    "section": name,
                    "title": (info["title"] or p["title"])[:250],
                    "match_number": extract_number(info["title"] or p["title"], info["body"]),
                    "publish_date": info.get("date") or (p["date"][:10] if p["date"][:4].isdigit() else None),
                    "magnet": info["magnet"],
                    "preview_images": info["images"],
                    "detail_url": durl,
                    "size": info["size_mb"],
                    "category": "",
                }
                results.append(rec)
                print("[post] %-8s 磁力×%d 图×%d %5sMB  %s" % (
                    rec["match_number"] or "-", len(rec["magnet"]), len(rec["preview_images"]),
                    rec["size"], rec["title"][:52]), file=sys.stderr)
                time.sleep(CFG["delay"])

    # 跨帖重复图（3 帖以上都出现）= 签名档/分割线，剔除
    from collections import Counter
    cnt = Counter()
    for r in results:
        for u in r["preview_images"]:
            cnt[u] += 1
    dup = {u for u, c in cnt.items() if c >= 3}
    if dup:
        for r in results:
            r["preview_images"] = [u for u in r["preview_images"] if u not in dup]
        print("[filter] 剔除跨帖重复装饰图 %d 张" % len(dup), file=sys.stderr)

    if CFG["dry"]:
        print(json.dumps(results, ensure_ascii=False, indent=2))
    else:
        write_db(results)
    print("\n[summary] 共解析 %d 帖，其中带磁力 %d 帖（%.0f%%）" % (
        len(results), sum(1 for r in results if r["magnet"]),
        100.0 * sum(1 for r in results if r["magnet"]) / max(1, len(results))), file=sys.stderr)


if __name__ == "__main__":
    main()
