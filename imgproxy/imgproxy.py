#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
immortal 图片缓存代理 v5（2026-10-28）
============================================
路径格式:  http://<NAS>:8002/<域名>/<路径>

v5 解决 v4 实测的三个真问题（日志 767 次 502 / 364 次 200）:

  A. 域名级熔断（最大收益）
     死站/图被删的图床（ymawv.la 131万张、hxmmdd.com 11万张）每张都要
     TCP+TLS+直连超时+代理超时，实测单张 4~10 秒。v4 只按 URL 拉黑，
     同一图床的另一张图仍然从头试一遍。
     v5 按「域名」统计窗口内失败：网络类失败 N 次或 404 类失败 M 次，且
     窗口内成功率 ≤ OK_RATIO_MAX 时把整个域名标记为死亡，后续请求毫秒级
     返回。活站偶发几张死图不会被误杀（实测 djhdhs 11 成功 6 慢，不能熔断）。
     后台线程定期用一条样本 URL 探活，恢复了自动放行（不需要重启）。

  B. 负缓存（负结果也告诉浏览器别再来问）
     v4 所有失败都回 `Cache-Control: no-store`，浏览器每次刷新/翻页都重新
     请求一遍死链。v5 对死链回 404 + 短 max-age，对网络类失败回 502 +
     短 max-age，浏览器自己会压住重复请求。

  C. 上游连接复用（省握手）
     每张图都新开 TCP+TLS（实测 0.3~0.9 秒握手，跨境 RST 更久）。v5 按
     (域名, 端口, 路由) 维护 keep-alive 连接池，同图床的连续图片复用连接。

  G. 总时限 + 慢速滴流保护
     有些图床会以 1~2KB/s 磨洋工，read timeout 被不断重置，单张拖到 90 秒
     （实测 djhdhs 有图要 98 秒）。v5 给每次回源加硬 deadline，并在起跑
     MIN_KBPS_GRACE 秒后检查平均速率，低于 MIN_KBPS 立即放弃。

  另加:
  D. /__stats 监控端点 + 每 10 分钟日志汇总（能看到 HIT/MISS/熔断域名排行）
  E. 熔断状态落盘 /cache/.state.json，重启不丢（v4 重启后要重新踩一遍坑）
  F. 并发闸改为带超时的阻塞获取，避免高峰期误回 503

保留 v4: 直连优先 + 失败转代理 + 域名路由记忆 + 单飞 + 拒收 HTML +
        分级黑名单 + 缓存 key 恒为 https://（老缓存继续命中）。

环境变量:
  CACHE_DIR / PORT / CONNECT_TIMEOUT / DIRECT_CONNECT_TIMEOUT / READ_TIMEOUT
  TOTAL_BUDGET / BLACKLIST_TTL / BLACKLIST_TTL_404 / CONCURRENCY
  CACHE_TTL / MAX_CACHE_MB / CLEAN_INTERVAL / UPSTREAM_PROXY / DOMAIN_MEMO_TTL
  REFERER / NEG_TTL / DEAD_MIN_FAILS / DEAD_TTL / DEAD_404_MIN_FAILS
  DEAD_404_TTL / DEAD_DOMAINS / CONCURRENCY_WAIT / POOL_SIZE / POOL_IDLE
"""

import hashlib
import json
import os
import re
import socket
import ssl
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

CACHE_DIR = os.environ.get("CACHE_DIR", "/cache")
PORT = int(os.environ.get("PORT", "8002"))
CONNECT_TIMEOUT = float(os.environ.get("CONNECT_TIMEOUT", "6"))
DIRECT_CONNECT_TIMEOUT = float(os.environ.get("DIRECT_CONNECT_TIMEOUT", "3"))
READ_TIMEOUT = float(os.environ.get("READ_TIMEOUT", "45"))
TOTAL_BUDGET = float(os.environ.get("TOTAL_BUDGET", "45"))
BLACKLIST_TTL = int(os.environ.get("BLACKLIST_TTL", "300"))
BLACKLIST_TTL_404 = int(os.environ.get("BLACKLIST_TTL_404", "3600"))
CONCURRENCY = int(os.environ.get("CONCURRENCY", "48"))
CONCURRENCY_WAIT = float(os.environ.get("CONCURRENCY_WAIT", "25"))
CACHE_TTL = float(os.environ.get("CACHE_TTL", str(180 * 24 * 3600)))
MAX_CACHE_BYTES = int(os.environ.get("MAX_CACHE_MB", "2048")) * 1024 * 1024
CLEAN_INTERVAL = float(os.environ.get("CLEAN_INTERVAL", str(24 * 3600)))
MAX_BODY = 50 * 1024 * 1024
REFERER = os.environ.get("REFERER", "https://sehuatang.net/")
# 防盗链按「图片域名自己」做白名单的站越来越多：hxmmdd 2026-10 起 Referer 不对
# 直接回 404（不是 403），整站会被误判成死站。默认用 self，个别站用 REFERER_MAP 覆盖。
# 格式：REFERER_MAP="host1=https://a/,host2=-"（- 表示不带 Referer）
REFERER_MAP = {}
for _kv in os.environ.get("REFERER_MAP", "").split(","):
    if "=" in _kv:
        _k, _v = _kv.split("=", 1)
        _k, _v = _k.strip().lower(), _v.strip()
        if _k:
            REFERER_MAP[_k] = "" if _v == "-" else _v


def referer_for(domain):
    """该域名回源该用哪个 Referer。默认「自己的域名」，MAP 里配过就用配置。"""
    r = REFERER_MAP.get(domain.lower())
    if r is not None:
        return r
    return "https://%s/" % domain
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")

# 负缓存：告诉浏览器失败结果保留多久（秒）
NEG_TTL = int(os.environ.get("NEG_TTL", "600"))
# 域名熔断
DEAD_MIN_FAILS = int(os.environ.get("DEAD_MIN_FAILS", "4"))
DEAD_TTL = int(os.environ.get("DEAD_TTL", "1800"))
DEAD_404_MIN_FAILS = int(os.environ.get("DEAD_404_MIN_FAILS", "6"))
DEAD_404_TTL = int(os.environ.get("DEAD_404_TTL", "21600"))
FAIL_WINDOW = float(os.environ.get("FAIL_WINDOW", "600"))
# 窗口内成功率低于该值才允许熔断（0.1 = 成功率不到 10% 才算整站死）
OK_RATIO_MAX = float(os.environ.get("OK_RATIO_MAX", "0.1"))
# 慢速滴流保护：起跑 GRACE 秒后若平均速率低于 MIN_KBPS，直接放弃
MIN_KBPS = float(os.environ.get("MIN_KBPS", "4"))
MIN_KBPS_GRACE = float(os.environ.get("MIN_KBPS_GRACE", "6"))
# 响应头超时：正常图床毫秒级返回头，卡住超过这个时间基本是坏链
HEADER_TIMEOUT = float(os.environ.get("HEADER_TIMEOUT", "8"))
# 单次 recv 等待上限：卡顿期间也要能周期性重算速率，否则会一直阻塞到总预算
READ_SLICE = float(os.environ.get("READ_SLICE", "4"))
DEAD_SEED = [d.strip().lower() for d in os.environ.get("DEAD_DOMAINS", "").split(",")
             if d.strip()]
# 以 "." 开头的是后缀熔断（匹配整族子域），其余按完整域名匹配
DEAD_SUFFIX = tuple(s for s in DEAD_SEED if s.startswith("."))
# 连接池
POOL_SIZE = int(os.environ.get("POOL_SIZE", "6"))
POOL_IDLE = float(os.environ.get("POOL_IDLE", "45"))

# 回源引擎：curl（HTTP/2 + 与浏览器一致的 TLS 指纹，绕开 Cloudflare 按指纹限速）
# 或 socket（纯 Python，无外部依赖）。auto = 有 curl 就用 curl。
FETCH_ENGINE = os.environ.get("FETCH_ENGINE", "auto").lower()
CURL_BIN = os.environ.get("CURL_BIN", "/usr/bin/curl")


def _curl_version():
    try:
        p = subprocess.run([CURL_BIN, "--version"], stdout=subprocess.PIPE,
                           stderr=subprocess.DEVNULL, timeout=10)
        if p.returncode == 0 and p.stdout:
            return p.stdout.decode("utf-8", "replace").splitlines()[0].strip()
    except Exception:
        pass
    return ""


_CURL_VER = "" if FETCH_ENGINE == "socket" else _curl_version()
USE_CURL = bool(_CURL_VER)
ENGINE_NAME = "curl" if USE_CURL else "socket"

_raw_proxy = os.environ.get("UPSTREAM_PROXY", "192.168.2.70:7892").strip()
PROXY_HOST, PROXY_PORT = "", 0
if _raw_proxy:
    _hp = _raw_proxy.replace("http://", "").rstrip("/")
    if ":" in _hp:
        PROXY_HOST, _p = _hp.rsplit(":", 1)
        try:
            PROXY_PORT = int(_p)
        except ValueError:
            PROXY_HOST = ""
    else:
        PROXY_HOST, PROXY_PORT = _hp, 7892

# 按域名指定代理端口：PROXY_MAP="jp.netcdn.space:7893,foo:7892"
# 为什么要分流：机场(7892) 与自建(7893) 对不同图床的连通性不一样 —— 实测
# jp.netcdn.space 走 7892 回 503、走 7893 回 200；img.m-team.cc 反过来只有 7892 通。
# 一个全局 UPSTREAM_PROXY 无法同时满足，必须按域名分。
PROXY_MAP = {}
for _kv in os.environ.get("PROXY_MAP", "").split(","):
    _kv = _kv.strip()
    if ":" in _kv:
        _k, _p = _kv.rsplit(":", 1)
        try:
            PROXY_MAP[_k.strip().lower()] = int(_p)
        except ValueError:
            pass


def proxy_for(domain):
    """该域名回源走哪个代理 (host, port)；没配代理返回 None。"""
    if not PROXY_HOST:
        return None
    return (PROXY_HOST, PROXY_MAP.get(domain.lower(), PROXY_PORT))


# 直连必然被 RST/超时的域名：直接走代理优先，省掉 3 秒 DIRECT_CONNECT_TIMEOUT
# （DMM 系 / mgstage / ldkms 全部直连不通，每次都先撞 3 秒再转代理，一页几十张就是几分钟）
PROXY_FIRST = set(d.strip().lower()
                  for d in os.environ.get("PROXY_FIRST_DOMAINS", "").split(",")
                  if d.strip())

_Path_RE = re.compile(r"^/([^/]+)/(.+)$")
_MIME = {
    ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
    ".gif": "image/gif", ".webp": "image/webp", ".bmp": "image/bmp",
    ".avif": "image/avif",
}

_sem = threading.BoundedSemaphore(CONCURRENCY)

# 单 URL 黑名单: key -> (过期时间戳, 回给浏览器的状态码)
_bad = {}
_bad_lock = threading.Lock()

# 单飞：key -> Event
_flight = {}
_flight_lock = threading.Lock()

# 域名健康记忆（路由用）+ 熔断（v5）
_dom_stat = {}
_dom_lock = threading.Lock()
DOMAIN_MEMO_TTL = float(os.environ.get("DOMAIN_MEMO_TTL", "600"))

# 统计
_stats = {"req": 0, "hit": 0, "hit_coalesced": 0, "miss": 0, "dead": 0,
          "fail": 0, "bytes": 0, "start": time.time()}
_stats_lock = threading.Lock()

STATE_FILE = os.path.join(CACHE_DIR, ".state.json")

# 上游 keep-alive 连接池: (domain, port, route) -> [(socket, 放入时间戳)]
_pool = {}
_pool_lock = threading.Lock()


def stat(name, n=1):
    with _stats_lock:
        _stats[name] = _stats.get(name, 0) + n


class ConnectTimeout(TimeoutError):
    """连接/TLS 阶段超时（黑洞图床）"""


class ProxyRefused(Exception):
    """代理拒绝 CONNECT"""


# ---------------- 域名熔断 ----------------
_DIRTY_SAMPLE_RE = re.compile(r"[\s()\\]|%0[ad]|%20")


def _clean_sample(url):
    """样本 URL 是否是干净的图片路径。

    论坛页面混进来的脏数据（被截断的 `/n(3`、带换行的扫描器 payload）必然
    404，拿它当「域名代表样本」会让整站永远救不回来（2026-10-29 事故）。
    """
    if not url or len(url) > 400:
        return False
    return not _DIRTY_SAMPLE_RE.search(url)


def _dom(domain):
    return _dom_stat.setdefault(domain, {
        "net": 0, "gone": 0, "oks": 0, "win_start": 0.0,
        "last_fail": 0.0, "ok_ts": 0.0, "dead_until": 0.0,
        "reason": "", "sample": "", "last_probe": 0.0,
    })


def _reset_window(st, now):
    """失败计数按窗口滚动统计，避免几天前的失败累积到现在。"""
    if now - st.get("win_start", 0.0) > FAIL_WINDOW:
        st["net"] = 0
        st["gone"] = 0
        st["oks"] = 0
        st["win_start"] = now


def domain_dead(domain):
    """域名是否处于熔断期。返回剩余秒数（0 = 正常）。"""
    d = domain.lower()
    # 后缀熔断：DEAD_DOMAINS 里以 "." 开头的项匹配整族子域。
    # 例：.imagehaha.com 一次覆盖 img69/img166/img202/img401/s10... （实测这些
    # 子域 443 全不通，逐个列要写十几个还漏新子域）
    if DEAD_SUFFIX and d.endswith(DEAD_SUFFIX):
        return float(DEAD_404_TTL)
    with _dom_lock:
        st = _dom_stat.get(d)
        if not st:
            return 0.0
        left = st["dead_until"] - time.time()
        return left if left > 0 else 0.0


def domain_note(domain, kind, sample=None):
    """kind: ok / net / gone。返回 True 表示本次调用触发了熔断。

    只有「几乎全是失败」的域名才熔断：活站偶发几张慢图/死图不能整站判死，
    否则会把正常图床误杀（实测 tu.djhdhs.us 11 张成功 6 张慢也会被误熔断）。
    """
    tripped = False
    with _dom_lock:
        st = _dom(domain)
        now = time.time()
        _reset_window(st, now)
        if sample and _clean_sample(sample):
            st["sample"] = sample
        if kind == "ok":
            st["net"] = 0
            st["gone"] = 0
            st["oks"] += 1
            st["dead_until"] = 0.0
            st["reason"] = ""
            st["ok_ts"] = now
            return False
        st["last_fail"] = now
        if kind == "net":
            st["net"] += 1
        else:
            st["gone"] += 1
        total = st["net"] + st["gone"] + st["oks"]
        ok_ratio = (st["oks"] / float(total)) if total else 0.0
        if st["dead_until"] <= now and ok_ratio <= OK_RATIO_MAX:
            if st["net"] >= DEAD_MIN_FAILS:
                st["dead_until"] = now + DEAD_TTL
                st["reason"] = "network"
                tripped = True
            elif st["gone"] >= DEAD_404_MIN_FAILS:
                st["dead_until"] = now + DEAD_404_TTL
                st["reason"] = "gone/404"
                tripped = True
    if tripped:
        save_state()
        with _dom_lock:
            st = _dom_stat.get(domain, {})
            print("[%s] DOMAIN-DEAD %s reason=%s until=%s sample=%s"
                  % (time.strftime("%d/%b/%Y %H:%M:%S"), domain, st.get("reason"),
                     time.strftime("%H:%M:%S", time.localtime(st.get("dead_until", 0))),
                     (st.get("sample") or "")[:110]),
                  file=sys.stderr, flush=True)
    return tripped


def save_state():
    """熔断状态落盘，重启不丢。失败不影响服务。"""
    try:
        with _dom_lock:
            snap = {d: {k: st[k] for k in ("dead_until", "reason", "sample", "net", "gone")}
                    for d, st in _dom_stat.items() if st["dead_until"] > time.time()}
        tmp = STATE_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump({"saved": time.time(), "domains": snap}, f)
        os.replace(tmp, STATE_FILE)
    except Exception as e:  # 状态文件坏了也不该影响出图
        print("[state] save failed: %s" % e, file=sys.stderr, flush=True)


def load_state():
    try:
        with open(STATE_FILE) as f:
            data = json.load(f)
    except (OSError, ValueError):
        return 0
    n = 0
    now = time.time()
    with _dom_lock:
        for d, v in (data.get("domains") or {}).items():
            if float(v.get("dead_until", 0)) > now:
                st = _dom(d)
                st["dead_until"] = float(v["dead_until"])
                st["reason"] = str(v.get("reason", ""))
                st["sample"] = str(v.get("sample", ""))
                n += 1
    if n:
        print("[state] restored %d dead domains" % n, flush=True)
    return n


# ---------------- 连接池 ----------------
def pool_take(key):
    if POOL_SIZE <= 0:
        return None
    now = time.time()
    with _pool_lock:
        lst = _pool.get(key)
        if not lst:
            return None
        while lst:
            sock, ts = lst.pop()
            if now - ts > POOL_IDLE:
                try:
                    sock.close()
                except Exception:
                    pass
                continue
            return sock
    return None


def pool_give(key, sock):
    if POOL_SIZE <= 0:
        try:
            sock.close()
        except Exception:
            pass
        return
    with _pool_lock:
        lst = _pool.setdefault(key, [])
        if len(lst) >= POOL_SIZE:
            try:
                sock.close()
            except Exception:
                pass
            return
        lst.append((sock, time.time()))


def pool_stats():
    with _pool_lock:
        return {"keys": len(_pool), "sockets": sum(len(v) for v in _pool.values())}


# ---------------- 缓存与黑名单 ----------------
def bad_get(key):
    with _bad_lock:
        v = _bad.get(key)
        if not v:
            return None
        if v[0] <= time.time():
            _bad.pop(key, None)
            return None
        return v[1]


def mark_bad(key, ttl, code=502):
    with _bad_lock:
        if len(_bad) > 20000:
            now = time.time()
            for k in [k for k, v in _bad.items() if v[0] <= now]:
                _bad.pop(k, None)
        _bad[key] = (time.time() + ttl, code)


def cache_path(url):
    h = hashlib.sha1(url.encode("utf-8")).hexdigest()
    return os.path.join(CACHE_DIR, h[:2], h[2:])


# ---------------- 回源 ----------------
def _open_tunnel(host, port, proxy_port=None):
    """经 HTTP 代理建 CONNECT 隧道，返回裸 socket。"""
    _pp = proxy_port or PROXY_PORT
    s = socket.create_connection((PROXY_HOST, _pp), timeout=CONNECT_TIMEOUT)
    s.settimeout(CONNECT_TIMEOUT)
    try:
        s.sendall(("CONNECT %s:%d HTTP/1.1\r\nHost: %s:%d\r\n\r\n"
                   % (host, port, host, port)).encode("latin-1"))
        buf = b""
        while b"\r\n\r\n" not in buf:
            d = s.recv(4096)
            if not d:
                raise EOFError("proxy closed during CONNECT")
            buf += d
            if len(buf) > 65536:
                raise ValueError("proxy CONNECT reply too large")
        line = buf.split(b"\r\n")[0]
        m = re.match(rb"HTTP/\d\.\d (\d{3})", line)
        if not m or m.group(1) != b"200":
            raise ProxyRefused(line.decode("latin-1", "replace")[:80])
        return s
    except BaseException:
        try:
            s.close()
        except Exception:
            pass
        raise


def _read_body(sock, headers, prefix=b"", deadline=None):
    t0 = time.time()
    if deadline is None:
        deadline = t0 + READ_TIMEOUT
    got = [len(prefix)]

    def _remain():
        elapsed = time.time() - t0
        # 慢速滴流保护：有些图床会 1~2KB/s 磨洋工，把整页拖到几十秒
        if MIN_KBPS > 0 and elapsed > MIN_KBPS_GRACE:
            kbps = (got[0] / 1024.0) / elapsed
            if kbps < MIN_KBPS:
                raise socket.timeout("too slow %.1fKB/%.1fs" % (got[0] / 1024.0, elapsed))
        left = deadline - time.time()
        if left <= 0:
            raise socket.timeout("read budget exceeded")
        return min(READ_TIMEOUT, left, READ_SLICE)

    def _recv(n):
        # 分片超时只是「这段没数据」，不等于连接坏了：重算速率后继续等
        while True:
            sock.settimeout(_remain())
            try:
                return sock.recv(n)
            except socket.timeout:
                continue

    def recv_until_marker(marker):
        nonlocal prefix
        while marker not in prefix:
            d = _recv(4096)
            if not d:
                raise EOFError("connection closed in body")
            prefix += d
            got[0] += len(d)
            if len(prefix) > 1024 * 1024:
                raise ValueError("chunk/trailer too large")
        idx = prefix.index(marker)
        out = prefix[:idx]
        prefix = prefix[idx + len(marker):]
        return out

    def recv_exact(n):
        nonlocal prefix
        while len(prefix) < n:
            d = _recv(65536)
            if not d:
                raise EOFError("connection closed mid-body")
            prefix += d
            got[0] += len(d)
        out = prefix[:n]
        prefix = prefix[n:]
        return out

    te = headers.get(b"transfer-encoding", b"")
    if te.lower() == b"chunked":
        out = bytearray()
        while True:
            line = recv_until_marker(b"\r\n")
            try:
                size = int(line.split(b";")[0].strip(), 16)
            except ValueError:
                raise ValueError("bad chunk size")
            if size == 0:
                while True:
                    t = recv_until_marker(b"\r\n")
                    if t == b"":
                        break
                break
            out += recv_exact(size)
            recv_exact(2)
            if len(out) > MAX_BODY:
                raise ValueError("body too large")
        return bytes(out), prefix
    cl = headers.get(b"content-length")
    if cl:
        n = int(cl)
        if n > MAX_BODY:
            raise ValueError("body too large")
        body = recv_exact(n)
        return body, prefix
    out = bytearray(prefix)
    prefix = b""
    while True:
        d = sock.recv(65536)
        if not d:
            break
        out += d
        if len(out) > MAX_BODY:
            raise ValueError("body too large")
    return bytes(out), b""


def _open_conn(key, domain, port, scheme, route):
    """返回 (socket, 是否复用)。复用失败时调用方会重建。"""
    sock = pool_take(key)
    if sock is not None:
        try:
            sock.settimeout(READ_TIMEOUT)
            return sock, True
        except OSError:
            try:
                sock.close()
            except Exception:
                pass
    ct = DIRECT_CONNECT_TIMEOUT if route == "direct" else CONNECT_TIMEOUT
    try:
        if route == "proxy":
            _px = proxy_for(domain)
            sock = _open_tunnel(domain, port, _px[1] if _px else None)
        else:
            sock = socket.create_connection((domain, port), timeout=ct)
            try:
                sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            except OSError:
                pass
    except socket.timeout:
        raise ConnectTimeout("connect timeout %s:%d via %s" % (domain, port, route))
    try:
        sock.settimeout(ct)
        if scheme == "https":
            ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
            try:
                sock = ctx.wrap_socket(sock, server_hostname=domain)
            except socket.timeout:
                raise ConnectTimeout("tls timeout %s via %s" % (domain, route))
    except ConnectTimeout:
        try:
            sock.close()
        except Exception:
            pass
        raise
    except BaseException:
        try:
            sock.close()
        except Exception:
            pass
        raise
    return sock, False


def fetch_http(domain, path, scheme, route, deadline=None):
    """单条路由回源（带连接复用）。返回 (status, headers, body)。"""
    port = 443 if scheme == "https" else 80
    # 池 key 必须带代理端口：否则 7892 与 7893 建立的隧道会互相复用，请求串到错的出口
    _pp = (proxy_for(domain) or ("", 0))[1] if route == "proxy" else 0
    pkey = (domain, port, route, _pp)
    last = None
    for attempt in (0, 1):
        sock, reused = _open_conn(pkey, domain, port, scheme, route)
        keep = False
        try:
            sock.settimeout(READ_TIMEOUT)
            req = ("GET /%s HTTP/1.1\r\nHost: %s\r\nUser-Agent: %s\r\nReferer: %s\r\n"
                   "Accept: image/*,*/*;q=0.8\r\nAccept-Encoding: identity\r\n"
                   "Connection: keep-alive\r\n\r\n"
                   % (path, domain, UA, referer_for(domain))).encode("latin-1")
            sock.sendall(req)
            head = b""
            hdr_deadline = time.time() + HEADER_TIMEOUT
            while b"\r\n\r\n" not in head:
                left = hdr_deadline - time.time()
                if left <= 0:
                    raise socket.timeout("header timeout %.0fs" % HEADER_TIMEOUT)
                if deadline is not None:
                    left = min(left, deadline - time.time())
                    if left <= 0:
                        raise socket.timeout("header budget exceeded")
                sock.settimeout(min(READ_TIMEOUT, left, READ_SLICE))
                try:
                    d = sock.recv(4096)
                except socket.timeout:
                    continue                    # 分片超时，header 总时限没到就继续等
                if not d:
                    break
                head += d
                if len(head) > 65536:
                    raise ValueError("response header too large")
            if b"\r\n\r\n" not in head:
                raise EOFError("no complete response header")
            raw_head, _, body_prefix = head.partition(b"\r\n\r\n")
            lines = raw_head.split(b"\r\n")
            m = re.match(rb"HTTP/\d\.\d (\d{3})", lines[0])
            if not m:
                raise ValueError("bad status line")
            status = int(m.group(1))
            headers = {}
            for ln in lines[1:]:
                k, _, v = ln.partition(b":")
                headers[k.strip().lower()] = v.strip()
            conn_hdr = headers.get(b"connection", b"").lower()
            keep = (b"close" not in conn_hdr)
            if status != 200:
                # 非 200 通常也带 body，没读完就复用会把残留字节当成下一个响应头，
                # 所以这类连接一律丢弃（失败请求本来就少，不值得为它解 body）。
                return status, headers, b""
            body, leftover = _read_body(sock, headers, body_prefix, deadline)
            if keep and not leftover:
                pool_give(pkey, sock)
                sock = None
            return status, headers, body
        except (ConnectTimeout, ProxyRefused):
            raise
        except (socket.timeout, EOFError, ValueError, OSError, ssl.SSLError) as e:
            last = e
            if reused and attempt == 0:
                continue                 # 复用的连接被上游关了 -> 建新连接重试一次
            raise
        finally:
            if sock is not None:
                try:
                    sock.close()
                except Exception:
                    pass
    raise last if last else RuntimeError("fetch failed")


def route_order(domain):
    """按域名健康记忆决定路由顺序，省 VPS 流量。

    只有「基本都失败」的域名才优先走代理；偶发失败不影响 —— 否则一次失败会让
    整个域名 10 分钟都绕 VPS，1MB+ 大图直接被拖死（实测直连 5s，绕代理 20s+）。
    """
    if not PROXY_HOST:
        return ["direct"]
    # 直连必然失败的域名：别浪费 3 秒直连超时，直接代理优先
    if domain.lower() in PROXY_FIRST:
        return ["proxy", "direct"]
    now = time.time()
    with _dom_lock:
        st = _dom_stat.get(domain)
        if not st:
            return ["direct", "proxy"]
        total = st["net"] + st["gone"] + st["oks"]
        ok_ratio = (st["oks"] / float(total)) if total else 1.0
        recent_fail = (now - st["last_fail"]) < DOMAIN_MEMO_TTL
    if recent_fail and ok_ratio < 0.5:
        return ["proxy", "direct"]
    return ["direct", "proxy"]


def _mark_route(domain, route, ok):
    with _dom_lock:
        st = _dom(domain)
        if ok:
            st["ok_ts"] = time.time()
        else:
            st["last_fail"] = max(st["last_fail"], time.time())


def _unlink(path):
    try:
        os.remove(path)
    except OSError:
        pass


def _head_bytes(path, n=512):
    try:
        with open(path, "rb") as f:
            return f.read(n)
    except OSError:
        return b""


def _curl_fetch(domain, rest, scheme, route, out_path, deadline):
    """用 curl 回源，body 直接写进 out_path；返回上游状态码。

    为什么要用 curl：tu.djhdhs.us 这类 Cloudflare 站点按 TLS 指纹限速。同一个
    1.1MB 图片，Python 的 ssl 客户端实测要 9.5~75 秒，curl 只要 1.8~2.6 秒。
    """
    url = "%s://%s/%s" % (scheme, domain, rest)
    remain = max(3.0, deadline - time.time())
    ct = DIRECT_CONNECT_TIMEOUT if route == "direct" else CONNECT_TIMEOUT
    args = [CURL_BIN, "-sS", "-k", "--http2", "--globoff",
            "-o", out_path,
            # 跟随 301/302：i.imgur(7947 张)、gyutto(75 张) 都靠这个才能出图。
            # 之前不带 -L，302 直接被当失败丢弃，整站看起来「图全没了」。
            "--location", "--max-redirs", "3",
            "--connect-timeout", "%.0f" % ct,
            "--max-time", "%.0f" % remain,
            "--speed-limit", "%d" % int(max(1.0, MIN_KBPS) * 1024),
            "--speed-time", "%.0f" % max(2.0, MIN_KBPS_GRACE),
            "-H", "User-Agent: " + UA,
            "-H", "Accept: image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
            "-H", "Accept-Encoding: identity",
            "-w", "%{http_code}"]
    _ref = referer_for(domain)
    if _ref:
        args += ["-H", "Referer: " + _ref]
    if route == "proxy":
        _px = proxy_for(domain)
        if _px:
            args += ["--proxy", "http://%s:%d" % _px]
    args.append(url)
    try:
        p = subprocess.run(args, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                           stderr=subprocess.PIPE, timeout=remain + 5)
    except subprocess.TimeoutExpired:
        raise socket.timeout("curl 超时 total budget")
    if p.returncode != 0:
        raise OSError("curl rc=%d %s" % (p.returncode,
                      p.stderr.decode("utf-8", "replace").strip()[:120]))
    try:
        return int(p.stdout.decode("ascii", "replace").strip()[:3])
    except ValueError:
        raise OSError("curl 状态码异常 %r" % p.stdout[:40])


def fetch_to_file(domain, path, out_path):
    """回源并把 body 落到 out_path。返回 (status, route, scheme, size)。

    引擎按 FETCH_ENGINE 选 curl / socket；两者都做 直连→代理 × https→http 兜底，
    且共用同一个总预算 deadline。
    """
    deadline = time.time() + TOTAL_BUDGET
    routes = route_order(domain)
    last = None
    for route in routes:
        route_ok = False
        for scheme in ("https", "http"):
            if deadline - time.time() <= 1:
                break
            _unlink(out_path)
            try:
                if USE_CURL:
                    status = _curl_fetch(domain, path, scheme, route, out_path, deadline)
                else:
                    status, _h, body = fetch_http(domain, path, scheme, route, deadline)
                    if status == 200:
                        os.makedirs(os.path.dirname(out_path), exist_ok=True)
                        with open(out_path, "wb") as f:
                            f.write(body)
            except ConnectTimeout as e:
                last = e
                break                      # 本路由黑洞，换下一条路由
            except ProxyRefused as e:
                last = e
                break                      # 代理不通，换下一条路由
            except ssl.SSLError as e:
                last = e
                continue                   # 证书类错误 -> 降级 http
            except (socket.timeout, EOFError, ValueError, OSError) as e:
                last = e
                continue                   # 数据阶段异常 -> 预算内降级重试
            route_ok = True
            if status == 200 or status in (404, 410):
                _mark_route(domain, route, True)
                size = 0
                if status == 200:
                    try:
                        size = os.path.getsize(out_path)
                    except OSError:
                        size = 0
                return status, route, scheme, size
            last = RuntimeError("upstream status %d" % status)
            if 300 <= status < 400:
                # 3xx 没被跟随成功（socket 引擎无 -L）：换路由/降级重试，
                # 不能像 404 那样一句 break 钉死
                continue
            break                          # 上游明确非 200，不换条路由重试
        _mark_route(domain, route, route_ok)
    raise last if last else RuntimeError("no upstream route available")


# ---------------- 单飞 ----------------
def flight_enter(key):
    with _flight_lock:
        ev = _flight.get(key)
        if ev is None:
            ev = threading.Event()
            _flight[key] = ev
            return True, ev
        return False, ev


def flight_leave(key, ev):
    with _flight_lock:
        _flight.pop(key, None)
    ev.set()


# ---------------- 定期清理 ----------------
def cleanup_once():
    now = time.time()
    removed = 0
    total = 0
    all_files = []
    for root, _dirs, files in os.walk(CACHE_DIR):
        for f in files:
            if f.startswith("."):
                continue
            p = os.path.join(root, f)
            try:
                st = os.stat(p)
            except OSError:
                continue
            total += st.st_size
            if now - st.st_mtime > CACHE_TTL:
                try:
                    os.remove(p)
                    removed += 1
                    total -= st.st_size
                except OSError:
                    pass
            else:
                all_files.append((st.st_mtime, p, st.st_size))
    if total > MAX_CACHE_BYTES:
        all_files.sort()
        while total > MAX_CACHE_BYTES and all_files:
            _mt, p, sz = all_files.pop(0)
            try:
                os.remove(p)
                removed += 1
                total -= sz
            except OSError:
                pass
    return removed, total


def cleaner_loop():
    while True:
        time.sleep(CLEAN_INTERVAL)
        try:
            n, total = cleanup_once()
            print("[cleanup] removed=%d cache_size=%.1fMB"
                  % (n, total / 1048576.0), flush=True)
        except Exception as e:
            print("[cleanup] error: %s" % e, flush=True)


# ---------------- 探活（熔断域名自动复活） ----------------
def probe_domain(domain, sample):
    """探活：先试干净样本（顺带补一张缓存），再兜底探域名根路径。

    判定标准是「服务器还有没有回 HTTP 响应」，不是「样本图是不是 200」：
    403/404 同样说明站点活着。旧逻辑拿必然 404 的脏样本去探活，域名一旦
    被误熔断就永远复活不了（2026-10-29 tu.djhdhs.us 事故）。
    """
    tmp = os.path.join(CACHE_DIR, ".probe.tmp")
    if sample and _clean_sample(sample):
        m = _Path_RE.match("/" + sample.split("://", 1)[-1])
        if m:
            host, rest = m.group(1), m.group(2)
            _unlink(tmp)
            try:
                status, _route, _scheme, size = fetch_to_file(host, rest, tmp)
            except Exception:
                status, size = 0, 0
            if (status == 200 and size > 0
                    and not _head_bytes(tmp).lstrip().lower().startswith(
                        (b"<!doctype", b"<html"))):
                cp = cache_path("https://%s/%s" % (host, rest))
                try:
                    os.makedirs(os.path.dirname(cp), exist_ok=True)
                    os.replace(tmp, cp)
                    return True
                except OSError:
                    _unlink(tmp)
                    return True
            _unlink(tmp)
            # 样本本身是干净的、却拿不到图 -> 站点确实没这张图，保持熔断。
            # 不能退到根路径兜底，否则「站活着但图全删」的域名每 15 分钟被放行一次
            return False
    # 兜底：样本缺失或本身是脏 URL，只能按「域名是否还有响应」判断
    _unlink(tmp)
    try:
        status, _route, _scheme, _size = fetch_to_file(domain, "", tmp)
    except Exception:
        _unlink(tmp)
        return False
    _unlink(tmp)
    return bool(status and status > 0)

def rehab_loop():
    while True:
        time.sleep(300)
        try:
            now = time.time()
            with _dom_lock:
                cands = [(d, st["sample"]) for d, st in _dom_stat.items()
                         if st["dead_until"] > now and st["sample"]
                         and now - st["last_probe"] > 900]
                cands = cands[:5]
                for d, _s in cands:
                    _dom(d)["last_probe"] = now
            for d, sample in cands:
                ok = probe_domain(d, sample)
                with _dom_lock:
                    st = _dom(d)
                    if ok:
                        st["dead_until"] = 0.0
                        st["reason"] = ""
                        st["net"] = 0
                        st["gone"] = 0
                    else:
                        # 网络类熔断可能只是抖动，续 30 分钟再看；整站 404 的续 6 小时
                        st["dead_until"] = time.time() + (
                            DEAD_TTL if st["reason"] == "network" else min(DEAD_404_TTL, 21600))
                # 恢复和续期都要落盘：只在恢复时写会导致 .state.json 里的
                # dead_until 永远过期，重启后恢复不出任何熔断状态
                save_state()
                print("[rehab] %s -> %s" % (d, "恢复" if ok else "仍不可用"),
                      file=sys.stderr, flush=True)
        except Exception as e:
            print("[rehab] error: %s" % e, file=sys.stderr, flush=True)


def summary_loop():
    while True:
        time.sleep(600)
        try:
            up = time.time() - _stats["start"]
            with _stats_lock:
                s = dict(_stats)
            total = s["hit"] + s["hit_coalesced"] + s["miss"] + s["dead"] + s["fail"]
            rate = (100.0 * (s["hit"] + s["hit_coalesced"]) / total) if total else 0.0
            now = time.time()
            with _dom_lock:
                dead = [(d, int(st["dead_until"] - now), st["reason"])
                        for d, st in _dom_stat.items() if st["dead_until"] > now]
                failrank = sorted(((d, st["net"] + st["gone"]) for d, st in _dom_stat.items()),
                                  key=lambda x: -x[1])[:5]
            print("[summary] 运行%.1fh 请求=%d HIT=%d(%.1f%%) MISS=%d 死链=%d 失败=%d "
                  "流量=%.1fMB 连接池=%s" % (up / 3600.0, total, s["hit"] + s["hit_coalesced"],
                                          rate, s["miss"], s["dead"], s["fail"],
                                          s["bytes"] / 1048576.0, pool_stats()), flush=True)
            print("[summary] 熔断中=%s ; 失败榜=%s"
                  % ([(d, "%ds" % t, r) for d, t, r in dead[:8]], failrank), flush=True)
        except Exception as e:
            print("[summary] error: %s" % e, flush=True)


# ---------------- HTTP 服务 ----------------
class Handler(BaseHTTPRequestHandler):
    server_version = "imgproxy/5.0"
    protocol_version = "HTTP/1.1"

    def do_GET(self):
        try:
            self._handle()
        except (BrokenPipeError, ConnectionResetError):
            self.close_connection = True
        except Exception as e:
            try:
                self._err(500, "internal error: %s" % e)
            except Exception:
                self.close_connection = True

    def _handle(self):
        stat("req")
        if self.path.startswith("/__stats"):
            self._send_stats()
            return
        m = _Path_RE.match(self.path)
        if not m:
            self._err(400, "path must be /<domain>/<path>")
            return
        domain, rest = m.group(1), m.group(2)
        key = "https://%s/%s" % (domain, rest)
        cp = cache_path(key)

        if self._cache_ok(cp):
            stat("hit")
            self._send_file(cp, "HIT")
            return

        code = bad_get(key)
        if code:
            stat("dead")
            self._err(code, "cached failure", cache_ttl=NEG_TTL, x_cache="DEAD-URL")
            return

        left = domain_dead(domain)
        if left > 0:
            stat("dead")
            self._err(404, "domain %s unavailable (%ds)" % (domain, int(left)),
                      cache_ttl=NEG_TTL, x_cache="DEAD-DOMAIN")
            return

        leader, ev = flight_enter(key)
        if not leader:
            ev.wait(TOTAL_BUDGET + 5)
            if self._cache_ok(cp):
                stat("hit_coalesced")
                self._send_file(cp, "HIT-COALESCED")
            else:
                stat("fail")
                code = bad_get(key) or 502
                self._err(code, "upstream failed (shared flight)",
                          cache_ttl=NEG_TTL, x_cache="FAIL")
            return
        try:
            self._fetch_and_store(domain, rest, key, cp)
        finally:
            flight_leave(key, ev)

    @staticmethod
    def _cache_ok(cp):
        try:
            return os.path.getsize(cp) > 0
        except OSError:
            return False

    def _fetch_and_store(self, domain, rest, key, cp):
        if not _sem.acquire(timeout=CONCURRENCY_WAIT):
            stat("fail")
            self._err(503, "busy, retry later", cache_ttl=30, x_cache="BUSY")
            return
        tmp = cp + ".tmp%d" % threading.get_ident()
        try:
            try:
                os.makedirs(os.path.dirname(cp), exist_ok=True)   # curl -o 要求目录先存在
                status, route, scheme, size = fetch_to_file(domain, rest, tmp)
            except Exception as exc:
                _unlink(tmp)
                mark_bad(key, BLACKLIST_TTL, 502)
                domain_note(domain, "net", key)
                stat("fail")
                self._note("FETCH-FAIL", key, str(exc))
                self._err(502, "upstream fetch failed: %s" % exc,
                          cache_ttl=NEG_TTL, x_cache="FAIL")
                return

            if status != 200:
                _unlink(tmp)
                # 403/451 = 防盗链 / CF 挑战 / 法律封锁：站点是活的，按网络类处理
                # （30 分钟短熔断 + 短负缓存），不能当「图已被删」判死 6 小时
                if status in (404, 410):
                    ttl = BLACKLIST_TTL_404
                    resp = 404
                    kind = "gone"
                else:
                    ttl = BLACKLIST_TTL
                    resp = 502
                    kind = "net"
                mark_bad(key, ttl, resp)
                domain_note(domain, kind, key)
                stat("dead")
                self._note("UPSTREAM-%d" % status, key, "via %s/%s" % (route, scheme))
                self._err(resp, "upstream status %d" % status,
                          cache_ttl=NEG_TTL, x_cache="FAIL")
                return

            if size <= 0:
                _unlink(tmp)
                mark_bad(key, BLACKLIST_TTL, 502)
                domain_note(domain, "net", key)
                stat("fail")
                self._note("EMPTY", key, "via %s/%s" % (route, scheme))
                self._err(502, "upstream returned empty body",
                          cache_ttl=NEG_TTL, x_cache="FAIL")
                return

            low = _head_bytes(tmp).lstrip().lower()
            if low.startswith(b"<!doctype") or low.startswith(b"<html"):
                _unlink(tmp)
                mark_bad(key, BLACKLIST_TTL, 502)
                domain_note(domain, "net", key)
                stat("fail")
                self._note("HTML-INSTEAD-OF-IMAGE", key, "via %s/%s" % (route, scheme))
                self._err(502, "upstream returned html",
                          cache_ttl=NEG_TTL, x_cache="FAIL")
                return

            try:
                os.makedirs(os.path.dirname(cp), exist_ok=True)
                os.replace(tmp, cp)      # curl 已把 body 写进 tmp，原子改名即可
            except Exception as e:
                _unlink(tmp)
                self._note("CACHE-WRITE-FAIL", key, e)
                self._err(500, "cache write failed")
                return
            domain_note(domain, "ok")
            stat("miss")
            stat("bytes", size)
            self._note("MISS", key, "via %s/%s/%s %.0fKB"
                       % (ENGINE_NAME, route, scheme, size / 1024.0))
            self._send_file(cp, "MISS")
        finally:
            _unlink(tmp)
            _sem.release()

    def _send_file(self, path, cache_state):
        ctype = _MIME.get(os.path.splitext(path)[1].lower(), "application/octet-stream")
        try:
            size = os.path.getsize(path)
        except OSError:
            self._err(404, "cache file missing")
            return
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(size))
        self.send_header("Cache-Control", "public, max-age=31536000, immutable")
        self.send_header("X-Cache", cache_state)
        self.end_headers()
        try:
            self.wfile.flush()
            with open(path, "rb") as f:
                while True:
                    chunk = f.read(262144)
                    if not chunk:
                        break
                    self.wfile.write(chunk)
        except (BrokenPipeError, ConnectionResetError):
            self.close_connection = True

    def _err(self, code, msg, cache_ttl=None, x_cache=None):
        body = msg.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        if cache_ttl:
            # 负缓存：让浏览器自己压住重复请求，别再回来问
            self.send_header("Cache-Control", "public, max-age=%d" % int(cache_ttl))
        else:
            self.send_header("Cache-Control", "no-store")
        if x_cache:
            self.send_header("X-Cache", x_cache)
        self.end_headers()
        try:
            self.wfile.write(body)
        except Exception:
            self.close_connection = True

    def _send_stats(self):
        now = time.time()
        with _stats_lock:
            s = dict(_stats)
        up = now - s["start"]
        total = s["hit"] + s["hit_coalesced"] + s["miss"] + s["dead"] + s["fail"]
        with _dom_lock:
            dead = [{"domain": d, "left_s": int(st["dead_until"] - now),
                     "reason": st["reason"], "sample": st["sample"][:120]}
                    for d, st in _dom_stat.items() if st["dead_until"] > now]
            failrank = sorted(
                ((d, st["net"], st["gone"]) for d, st in _dom_stat.items()
                 if st["net"] + st["gone"] > 0),
                key=lambda x: -(x[1] + x[2]))[:10]
        payload = {
            "uptime_h": round(up / 3600.0, 2),
            "requests": total,
            "hit": s["hit"], "hit_coalesced": s["hit_coalesced"], "miss": s["miss"],
            "dead": s["dead"], "fail": s["fail"],
            "hit_rate_pct": round(100.0 * (s["hit"] + s["hit_coalesced"]) / total, 1) if total else 0.0,
            "cache_bytes_mb": round(s["bytes"] / 1048576.0, 1),
            "pool": pool_stats(),
            "dead_domains": dead[:20],
            "fail_rank": [{"domain": d, "net": n, "gone": g} for d, n, g in failrank],
            "config": {
                "concurrency": CONCURRENCY, "neg_ttl": NEG_TTL,
                "dead_min_fails": DEAD_MIN_FAILS, "dead_ttl": DEAD_TTL,
                "dead_404_min_fails": DEAD_404_MIN_FAILS, "dead_404_ttl": DEAD_404_TTL,
                "ok_ratio_max": OK_RATIO_MAX, "min_kbps": MIN_KBPS,
                "header_timeout": HEADER_TIMEOUT,
                "pool_size": POOL_SIZE, "read_timeout": READ_TIMEOUT,
                "total_budget": TOTAL_BUDGET,
                "proxy": ("%s:%d" % (PROXY_HOST, PROXY_PORT)) if PROXY_HOST else "off",
                "proxy_map": PROXY_MAP or {},
                "proxy_first": sorted(PROXY_FIRST),
                "engine": ENGINE_NAME,
                "curl": _CURL_VER[:80],
            },
        }
        body = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(body)
        except Exception:
            self.close_connection = True

    @staticmethod
    def _note(tag, key, detail):
        print("[%s] %s %s %s" % (time.strftime("%d/%b/%Y %H:%M:%S"), tag, key, detail or ""),
              file=sys.stderr, flush=True)

    def log_message(self, fmt, *args):
        sys.stderr.write("[%s] %s\n" % (self.log_date_time_string(), fmt % args))


def main():
    os.makedirs(CACHE_DIR, exist_ok=True)
    restored = load_state()
    seeded = 0
    for d in DEAD_SEED:
        if d.startswith("."):
            continue              # 后缀规则在 domain_dead 里实时生效，不占状态表
        with _dom_lock:
            if d in _dom_stat:
                continue          # 已有状态（含探活救回来的）不再强行判死
            st = _dom(d)
            st["dead_until"] = time.time() + DEAD_404_TTL
            st["reason"] = "seed"
            seeded += 1
    if seeded:
        save_state()
    print("[state] restored=%d seeded=%d" % (restored, seeded), flush=True)
    for fn in (cleaner_loop, rehab_loop, summary_loop):
        threading.Thread(target=fn, daemon=True).start()
    srv = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    srv.daemon_threads = True
    print("imgproxy v5.5 listening on :%d 回源引擎=%s (%s) cache=%s connect=%ss(direct %ss) "
          "read=%ss budget=%ss blacklist=%ss/%ss ttl=%dd max=%.0fMB concurrency=%d proxy=%s "
          "domain_memo=%ss neg_ttl=%ss dead=%d/%ds %d/%ds ok_ratio<=%.2f "
          "min_kbps=%.1f(grace %.0fs) header_timeout=%.0fs pool=%d"
          % (PORT, ENGINE_NAME, _CURL_VER or "-", CACHE_DIR, CONNECT_TIMEOUT,
             DIRECT_CONNECT_TIMEOUT, READ_TIMEOUT,
             TOTAL_BUDGET, BLACKLIST_TTL, BLACKLIST_TTL_404, CACHE_TTL / 86400.0,
             MAX_CACHE_BYTES / 1048576.0, CONCURRENCY,
             ("%s:%d" % (PROXY_HOST, PROXY_PORT)) if PROXY_HOST else "off",
             DOMAIN_MEMO_TTL, NEG_TTL, DEAD_MIN_FAILS, DEAD_TTL,
             DEAD_404_MIN_FAILS, DEAD_404_TTL, OK_RATIO_MAX,
             MIN_KBPS, MIN_KBPS_GRACE, HEADER_TIMEOUT, POOL_SIZE),
          flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()
