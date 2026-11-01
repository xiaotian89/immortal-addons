#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
给 immortal 的 BT动漫区 补封面：标题 → 作品名 → bangumi 搜索 → 封面

用法：
  DRY_RUN=1 python3 anime_cover.py   # 只打印
  DRY_RUN=0 python3 anime_cover.py   # 写库
"""
import os
import re
import sys
import json
import time
import subprocess
import urllib.parse
import difflib

DRY = os.environ.get("DRY_RUN", "1") == "1"
SECTION = os.environ.get("SECTION", "BT动漫区")
# API 与图片走不同出口：api.bgm.tv 走机场(7892)正常，lain.bgm.tv 只有自建(7893)拿得到
API_PROXY = os.environ.get("API_PROXY", "http://192.168.2.70:7892")
IMG_PROXY = os.environ.get("IMG_PROXY", "http://192.168.2.70:7893")
IMG_BASE = os.environ.get("IMG_PROXY_BASE", "http://192.168.2.70:8002").rstrip("/")
SIM_MIN = float(os.environ.get("SIM_MIN", "0.40"))

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0 Safari/537.36")

DB = dict(host=os.environ.get("DB_HOST", "127.0.0.1"),
          port=int(os.environ.get("DB_PORT", "25432")),
          user=os.environ.get("DB_USER", "immortal"),
          password=os.environ.get("DB_PASSWORD", ""),
          dbname=os.environ.get("DB_NAME", "immortal_db"))

# 已知死图床：补封面时顺手摘掉，避免页面上一堆死链
DEAD_HOSTS = ("imagehaha.com", "imageshimage.com", "qpic.ws",
              "tu.ymawv.la", "tu.w4kmzkm.com", "upload.cc", "s7tu.com",
              "tu6.3wk.cc", "snapgram.co", "djsndtupian.com", "33img.com",
              "z4a.net", "ultraimg.com")


def psql(sql):
    r = subprocess.run(["docker", "exec", "immortal-pg", "psql", "-U", "immortal",
                        "-d", "immortal_db", "-t", "-A", "-F", "|", "-c", sql],
                       capture_output=True, text=True)
    if r.returncode != 0:
        print("[psql]", r.stderr.strip()[:200], file=sys.stderr)
    return [x for x in r.stdout.strip().splitlines() if x]


def clean_title(t):
    s = t or ""
    s = re.sub(r"^\s*\(18禁アニメ\)\s*", "", s)
    # 整段删掉 [...] / 【...】 / (無修正...) —— 里面装的是厂牌和标签，
    # 留在关键词里会把 [Collaboration Works] 这类拆成 "Works 作品名"
    s = re.sub(r"\[[^\]]*\]|【[^】]*】", " ", s)
    s = re.sub(r"\(無修正[^)]*\)|（無修正[^）]*）", " ", s)
    s = re.sub(r"《[^》]*》|「[^」]*」", " ", s)
    s = re.sub(r"全\s*\d*\s*[话話集]", " ", s)
    s = re.sub(r"^\s*(19|20)\d{2}\s*年\s*\d{1,2}\s*月\s*合集\s*$", "", s)
    s = re.sub(r"[\(\)（）〜~～]", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def core(s):
    """比对用的核心名：去掉 THE ANIMATION 这类通用后缀和副标题"""
    s = re.sub(r"THE\s+ANIMATION.*$", "", s or "", flags=re.I)
    s = re.sub(r"ANIMATION.*$", "", s, flags=re.I)
    s = re.sub(r"[～~].*$", "", s)
    return s.replace(" ", "").strip()


def kw_variants(kw):
    """搜索关键词的逐级回退：完整名 → 去后缀 → 去感叹号后半段 → 首个词"""
    out = [kw]
    for v in (core(kw),
              re.sub(r"[!！].*$", "", kw).strip(),
              re.sub(r"-[^-]*$", "", kw).strip(),
              re.sub(r"\s.*$", "", kw).strip()):
        if v and v != kw and v not in out and len(v) >= 3:
            out.append(v)
    return out


def name_score(kw, name):
    """关键词与候选名的匹配分。互相包含直接满分，其余用编辑距离，
    但只比对 core()，否则两个不同作品都带 "THE ANIMATION" 会算出 0.9 的假高分"""
    a, b = core(kw), core(name)
    if not a or not b:
        return 0.0
    if a in b or b in a:
        return 1.0
    return difflib.SequenceMatcher(None, a, b).ratio()



def bgm_search(keyword):
    url = ("https://api.bgm.tv/search/subject/" + urllib.parse.quote(keyword) +
           "?type=2&responseGroup=large&max_results=6")
    p = subprocess.run(["curl", "-sS", "--max-time", "25", "-H", "User-Agent: " + UA,
                        "-x", API_PROXY, url], capture_output=True, text=True)
    try:
        return (json.loads(p.stdout).get("list") or [])
    except Exception:
        return []


def sim(a, b):
    return difflib.SequenceMatcher(None, a or "", b or "").ratio()


def pick_cover(keyword, items):
    """挑相似度最高的结果，返回 (封面url, 匹配名, 相似度)"""
    best = (None, None, 0.0)
    for it in items:
        names = [it.get("name"), it.get("name_cn")]
        for n in names:
            if not n:
                continue
            s = name_score(keyword, n)
            if s > best[2]:
                imgs = it.get("images") or {}
                cover = imgs.get("large") or imgs.get("common") or imgs.get("medium")
                if cover and cover.startswith("//"):
                    cover = "https:" + cover
                best = (cover, n, s)
    return best


def to_proxy(url):
    if not url:
        return None
    u = url if url.startswith("http") else "https:" + url
    m = re.match(r"^https?://([^/]+)/(.+)$", u)
    if not m:
        return None
    return "%s/%s/%s" % (IMG_BASE, m.group(1), m.group(2))


def img_ok(url, t=25):
    # 必须带 -L：lain.bgm.tv 的 http 会重定向，没有 -L 拿到的是 302 不是图
    p = subprocess.run(["curl", "-s", "-k", "-L", "--max-time", str(t), "-o", "/dev/null",
                        "-w", "%{http_code}", "-x", IMG_PROXY, "-H", "User-Agent: " + UA, url],
                       capture_output=True, text=True)
    return p.stdout.strip() == "200"


def main():
    rows = psql("select id||'|'||coalesce(match_number,'-')||'|'||title||'|'||"
                "coalesce(array_to_string(preview_images,','),'') "
                "from article where website='169bt' and section='%s' order by id desc" % SECTION)
    print("[cfg] 板块=%s 记录=%d 模式=%s 相似度门槛=%.2f" % (
        SECTION, len(rows), "只读" if DRY else "写库", SIM_MIN), file=sys.stderr)

    done = []
    for row in rows:
        parts = row.split("|")
        if len(parts) < 4:
            continue
        rid, num, title, old_imgs = parts[0], parts[1], parts[2], parts[3]
        kw = clean_title(title)
        if not kw or re.search(r"合集", kw):
            print("  [skip] %s 无法提炼作品名" % title[:40], file=sys.stderr)
            continue
        cover = matched = None
        score = 0.0
        for variant in kw_variants(kw):
            cover, matched, score = pick_cover(variant, bgm_search(variant))
            if cover and score >= SIM_MIN:
                break
        if not cover or score < SIM_MIN:
            print("  [miss] %-28s 搜「%s」→ 最佳 %s (%.2f)" % (
                title[:26], kw[:16], (matched or "无")[:22], score), file=sys.stderr)
            continue
        if not img_ok(cover):
            print("  [bad ] %-28s 封面下载失败" % title[:26], file=sys.stderr)
            continue

        proxy_cover = to_proxy(cover)
        # 保留非死图床的旧图
        keep = []
        for u in [x for x in old_imgs.split(",") if x]:
            host = re.sub(r"^http://192\.168\.2\.70:8002/([^/]+)/.*$", r"\1", u)
            if not any(d in host for d in DEAD_HOSTS):
                keep.append(u)
        new_imgs = [proxy_cover] + [k for k in keep if k != proxy_cover]

        print("  [ok  ] %-26s → %-22s (%.2f) 图 %d 张" % (
            title[:26], matched[:22], score, len(new_imgs)), file=sys.stderr)
        done.append((rid, new_imgs, title, matched, score))
        time.sleep(0.4)

    print("\n[result] 成功 %d / %d" % (len(done), len(rows)), file=sys.stderr)

    if DRY:
        print(json.dumps([{"id": d[0], "images": d[1], "matched": d[3]} for d in done],
                         ensure_ascii=False, indent=1))
        return

    try:
        import psycopg2
        from psycopg2.extras import execute_batch
    except ImportError:
        print("[fatal] 缺 psycopg2", file=sys.stderr)
        sys.exit(2)
    conn = psycopg2.connect(**DB)
    try:
        with conn, conn.cursor() as cur:
            execute_batch(cur,
                          "UPDATE article SET preview_images = %s::text[] WHERE id = %s",
                          [(imgs, int(rid)) for rid, imgs, _t, _m, _s in done],
                          page_size=20)
        print("[db] 已更新 %d 条封面" % len(done), file=sys.stderr)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
