"""B 站（bilibili）视频解析模块。

与 ``parser.py``（抖音）保持同一套对外契约：``parse_share_url()`` 返回结构一致，
便于前端复用同一张结果卡片。

设计依据全部来自实测（2026-09），不是照抄文档：

* 元信息走官方 ``x/web-interface/view``，**无需登录、无需 WBI 签名**即可拿到
  标题 / 简介 / 封面 / UP 主 / 时长 / 分P列表。
* 播放地址走 ``x/player/playurl``，返回两种形态，**代码两种都支持**：

  1. ``fnval=1`` → ``durl``，一个**完整的 MP4（自带音轨）**，无需合并，直接下载。
     实测 51 分钟 / 655MB 的视频仍是单段。
  2. ``fnval=4048`` → ``dash``，音视频**分成两条流**。1080P60 / 4K / HDR 等高阶
     档位**只在 DASH 下提供**，必须用 ffmpeg 无损合并（``-c copy``）才能得到成品。
     合并发生在服务端（见 ``build_dash_file``），用户仍是点一次下载一个文件。

  策略：同一档位优先用 MP4（少一次合并、更快）；DASH 只用来**补齐 MP4 拿不到的档位**。
  这样普通场景不付合并成本，高画质需求也能满足。
* 画质逐档请求：``support_formats`` / ``accept_quality`` 会列出「理论上支持」的档位
  （含当前账号拿不到的 1080P），照它列清单会让用户点了 1080P 却下载到 720P。
  所以这里逐档真实请求，**只保留接口真的返回了的档位**，拿不到的不列出来。
* ⚠️ 登录 ≠ 解锁高画质：实测**非大会员账号登录后仍封顶 720P**，1080P / 1080P60 /
  4K / HDR 全部需要大会员。前端文案据此写得比较克制，避免用户白期待。
  用 ``debug_qualities()`` 可以一次性看清「凭据是否生效 / 是否大会员 / 视频真实上限」。
* 需要更高画质时有两种取凭据的方式：

  1. **扫码登录**（推荐，体验好）：前端调 ``qr_generate()`` 拿二维码，
     用户用哔哩哔哩 App 扫码，``qr_poll()`` 成功后把凭据交给调用方保存到
     该访客自己的会话里（见 ``sessions.py``），后续解析按会话带上。
     登录态由「哪些档位接口真的返回了」自动体现，无需为登录态另写分支。
  2. **服务端统一账号**：设置环境变量 ``BILI_COOKIE`` 为浏览器登录后的
     Cookie（``SESSDATA`` 等）。优先级低于会话级凭据（见 ``resolve_cookie``）。

接口错误码（实测）：
* ``view``：``62002`` 稿件不可见 / 已失效；``-404`` 不存在
* ``playurl``：``-404`` 付费或需要登录（``rights.pay == 1``）
* ``qrcode/poll``：``86101`` 未扫码 / ``86090`` 已扫待确认 / ``86038`` 已失效 / ``0`` 成功

风控（HTTP 412）——本模块最容易踩的坑，单独说明：

B 站 ``api.bilibili.com`` 前面有一层风控网关。它不看你的业务参数，只看**这个请求
像不像真实浏览器发出来的**。命中即直接返回 ``HTTP 412 Precondition Failed``，
连 JSON 都不给。实测触发条件按影响从大到小排：

1. **请求不带任何 Cookie**（``buvid3`` 缺失）。这是最致命的一条——同一台机器、
   同一个接口，不带 buvid3 必 412，带上就正常返回。而未登录用户天然没有 Cookie，
   所以「裸请求」是这个项目线上最常被拦的形态。
2. **请求头残缺**。只有 ``UA + Referer`` 的请求会被判为脚本；真实 Chrome 首屏
   还会带 ``Origin`` / ``Accept`` / ``sec-ch-ua`` / ``sec-fetch-*``。Referer
   精确到视频页比只给站点首页更宽松。
3. **零间隔连发**。一次解析要发近十个请求（view + 逐档 playurl + dash），
   机房 IP 上这种突发很容易被限。
4. **IP 本身已被标记**（机房 IP 段被批量风控）。这条改代码解决不了，
   只能在服务器上配 ``BILI_COOKIE`` 抬升信誉度，或换 IP。

对应地，本模块做了四层防护，全部对调用方透明：

* ``fingerprint()``：启动后按需抓一套设备指纹（首页 ``Set-Cookie`` 拿
  ``buvid3`` / ``b_nut``，``x/frontend/finger/spi`` 补 ``buvid4``），缓存复用，
  **所有**接口请求都带上——包括未登录用户；
* ``_browser_headers()``：把请求头补齐成真实 Chrome 的样子；
* ``_throttle()``：给接口请求加全局最小间隔，把突发摊平；
* ``_api_get()``：命中 412 时自动换一套指纹退避重试，仍失败才回报用户。
"""

from __future__ import annotations

import asyncio
import os
import re
import secrets
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

import httpx

# 桌面端 UA：B 站接口对移动端 UA 的返回略有差异，桌面端最稳定
PC_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

#: 可选：登录后的 Cookie。设了才能解锁 1080P / 4K。
BILI_COOKIE = os.environ.get("BILI_COOKIE", "").strip()

_API = "https://api.bilibili.com"
_VIEW_API = f"{_API}/x/web-interface/view"
_PLAYURL_API = f"{_API}/x/player/playurl"

# ---------------------------------------------------------------- 风控相关配置

#: 站点首页：设备指纹（buvid3 / b_nut）由这里下发，也是默认 Referer
_HOME_URL = "https://www.bilibili.com/"

#: 设备指纹接口，返回 ``b_3``（buvid3）与 ``b_4``（buvid4）
_FINGER_API = f"{_API}/x/frontend/finger/spi"

#: 设备指纹缓存时长（秒），默认 6 小时。
#: 太短则每次解析都要多花两次请求；太长则一套指纹被风控盯上后会一直用旧的。
_FP_TTL = float(os.environ.get("BILI_FP_TTL", "21600"))

#: 命中 412 后「换一套指纹重试」的次数（默认 2，即最多发 3 次）。
_RETRY_412 = int(os.environ.get("BILI_412_RETRY", "2"))

#: 两次 B 站接口请求之间的全局最小间隔（秒），默认 0.2。
#: 设为 0 可关闭；调大更稳但解析更慢。
_MIN_INTERVAL = float(os.environ.get("BILI_MIN_INTERVAL", "0.2"))

# ---------------------------------------------------------------- 链接识别

# BV 号：BV + 10 位 base58（前缀大小写不敏感，便于兼容手抄的小写链接）
_BV_RE = re.compile(r"BV[0-9A-Za-z]{10}", re.I)
# 旧版 av 号
_AV_RE = re.compile(r"(?:^|[^\w])av(\d{1,12})(?:[^\d]|$)", re.I)
# b23.tv 短链
_B23_RE = re.compile(r"b23\.tv/([0-9A-Za-z]+)", re.I)
# 分P：?p=2
_PAGE_RE = re.compile(r"[?&]p=(\d{1,4})")

# 从分享文本里抽链接（B 站分享格式：【标题-哔哩哔哩】 https://b23.tv/xxxxx）
_URL_IN_TEXT_RE = re.compile(r"https?://[^\s\u4e00-\u9fff\uff00-\uffef]+", re.I)
_BARE_URL_RE = re.compile(
    r"(?:www\.)?bilibili\.com/[^\s\u4e00-\u9fff]*|b23\.tv/[0-9A-Za-z]+", re.I
)
_BILI_HOST_SUFFIXES = ("bilibili.com", "b23.tv")
_URL_TRAILING_CHARS = "，。、！？；：）】》」』\"'.,!?;:)]}>"

# ---------------------------------------------------------------- 画质表

# 画质代号 -> (短标签, 补充描述)。取自 B 站官方清晰度命名。
_QUALITY_META: Dict[int, tuple] = {
    6: ("240P", "极速"),
    16: ("360P", "流畅"),
    32: ("480P", "清晰"),
    64: ("720P", "准高清"),
    74: ("720P60", "准高清60帧"),
    80: ("1080P", "高清"),
    112: ("1080P+", "高码率"),
    116: ("1080P60", "高清60帧"),
    120: ("4K", "超清"),
    125: ("HDR", "真彩"),
    126: ("杜比视界", "杜比视界"),
    127: ("8K", "超高清"),
}

# 画质代号 -> 目标短边像素（用于给用户一个直观的尺寸参考）
_QUALITY_SHORT_SIDE: Dict[int, int] = {
    6: 240, 16: 360, 32: 480, 64: 720, 74: 720,
    80: 1080, 112: 1080, 116: 1080, 120: 2160, 125: 2160, 126: 2160, 127: 4320,
}

# 逐档尝试的顺序（高 -> 低），实际以接口真实返回的 quality 为准
_QN_CANDIDATES = (120, 112, 116, 80, 74, 64, 32, 16)

#: 单个视频最多发出几次 playurl 请求（防止接口调用过多被限流）。
#: 登录后可选档位会变多（1080P / 1080P+ / 4K…），所以留出足够余量。
_MAX_PLAYURL_CALLS = 8

#: DASH 格式标识：4048 = 16|32|64|128|256|512|1024|2048，即「所有 DASH 流」。
#: 1080P60 / 4K / HDR / 杜比视界等高阶档位只在 DASH 下提供。
_DASH_FNVAL = 4048

#: 视频编码代号（dash.video[].codecid）-> 可读名称
_CODEC_NAMES: Dict[int, str] = {
    7: "H.264",
    12: "H.265/HEVC",
    13: "AV1",
    14: "H.266/VVC",
}

#: 编码兼容性优先级，数字越小越通用。
#: H.264 几乎所有播放器都能放；AV1 / H.266 兼容性差，仅在无更优选择时使用。
_CODEC_PRIORITY: Dict[int, int] = {7: 0, 12: 1, 13: 2, 14: 3}

#: ffmpeg 可执行文件路径。留空则用 PATH 里的 ffmpeg（Docker 镜像里是 apt 装的）。
FFMPEG_BIN = os.environ.get("FFMPEG_BIN", "").strip()

#: ffmpeg 可用性探测缓存：``(路径或 None, 探测时刻)``，见 ``ffmpeg_path``。
_ffmpeg_cache: Optional[tuple] = None

#: 探测结果缓存时长（秒）。不能永久缓存——用户可能在服务运行期间才装上 ffmpeg。
_FFMPEG_TTL = 600.0

_VIEW_ERRORS = {
    -400: "请求参数有误，请确认链接是否完整",
    -403: "访问权限不足，无法解析该视频",
    -404: "视频不存在，请确认链接是否正确",
    62002: "视频不存在或已失效（可能已被删除、设为私密）",
    62004: "视频正在审核中，暂时无法观看",
}


class ParseError(Exception):
    """业务解析错误，message 会直接展示给用户。"""


# ---------------------------------------------------------------- 链接工具

def _host_of(url: str) -> str:
    try:
        return (urlparse(url).hostname or "").lower()
    except Exception:
        return ""


#: B 站自家 CDN 的域名关键字。用于给直链排序，见 ``prefer_official_cdn``。
_OFFICIAL_CDN_KEYS = ("bilivideo", "hdslb", "bilibili", "akamaized.net", "upos")


def prefer_official_cdn(urls: List[str]) -> List[str]:
    """把 B 站官方 CDN 的地址排到前面，PCDN / 未知域名垫后。

    ``playurl`` 返回的 ``url`` 有时是**第三方 PCDN 边缘节点**
    （实测形如 ``809aj93l.edge.mountaintoys.cn:4483``），``backup_url`` 里才放着
    ``upos-*.bilivideo.com`` 这类官方 CDN。而前端只会拿列表里的**第一个**地址去
    请求 ``/api/download``，那个接口带域名白名单（防止被当开放代理），PCDN 域名
    不在白名单里 —— 用户看到的现象就是「解析成功，一点下载就 400」。

    排序后官方 CDN 优先：既绕开白名单问题，稳定性也明显更好（PCDN 节点抖动大）。
    其余地址原样保留在末尾，作为服务端合并下载时的兜底。

    用稳定排序，同档位不改变接口给出的原始优先级。
    """
    def rank(u: str) -> int:
        host = _host_of(u)
        return 0 if any(k in host for k in _OFFICIAL_CDN_KEYS) else 1

    return sorted([u for u in urls if u], key=rank)


def is_bilibili_link(url: str) -> bool:
    """判断链接是否属于 B 站系域名（含 b23.tv 短链）。"""
    host = _host_of(url)
    return any(host == s or host.endswith("." + s) for s in _BILI_HOST_SUFFIXES)


def is_bilibili_short_link(url: str) -> bool:
    """是否是 b23.tv 短链（需要跟随后才能拿到 BV 号）。"""
    return _host_of(url).endswith("b23.tv")


def is_bilibili_id(text: str) -> bool:
    """输入里是否含 B 站视频号（裸 BV 号 / av 号）。

    用户常直接粘贴 ``BV1GJ411x7h7`` 这类纯视频号（前端 placeholder 也这么写），
    这种输入没有域名，靠 ``is_bilibili_link`` 识别不出来，需要单独判断。
    BV 号形如 ``BV`` + 10 位字母数字，特征足够明显，误判风险很低。
    """
    t = (text or "").strip()
    if not t:
        return False
    return bool(_BV_RE.search(t) or _AV_RE.search(t))


def extract_share_url(text: str) -> str:
    """从分享内容中提取纯链接。

    B 站 App「复制链接」得到的通常是这样一段文字::

        【标题-哔哩哔哩】 https://b23.tv/xxxxx

    这里把链接单独抽出来；若传入的本身就是纯链接则原样返回。
    """
    text = (text or "").strip()
    if not text:
        return ""

    if re.match(r"^https?://\S+$", text, re.I):
        return text.rstrip(_URL_TRAILING_CHARS)

    first_any = ""
    for m in _URL_IN_TEXT_RE.finditer(text):
        u = m.group(0).rstrip(_URL_TRAILING_CHARS)
        if is_bilibili_link(u):
            return u
        if not first_any:
            first_any = u
    if first_any:
        return first_any

    m = _BARE_URL_RE.search(text)                       # 无协议的裸域名
    if m:
        return "https://" + m.group(0).rstrip(_URL_TRAILING_CHARS)
    return ""


def extract_bvid(text: str) -> Optional[str]:
    """从任意文本中提取 BV 号（大小写不敏感，统一为大写 BV 前缀）。"""
    m = _BV_RE.search(text or "")
    if not m:
        return None
    raw = m.group(0)
    return "BV" + raw[2:]


def extract_av_id(text: str) -> Optional[int]:
    """提取旧版 av 号。"""
    m = _AV_RE.search(text or "")
    return int(m.group(1)) if m else None


def extract_page(text: str) -> Optional[int]:
    """提取分P号（``?p=2``）。"""
    m = _PAGE_RE.search(text or "")
    if not m:
        return None
    n = int(m.group(1))
    return n if n >= 1 else None


# ---------------------------------------------------------------- 基础工具

def _fmt_size(n: Optional[int]) -> str:
    if not n:
        return "大小未知"
    mb = n / 1024 / 1024
    return f"{mb:.1f} MB" if mb >= 1 else f"{n / 1024:.0f} KB"


def _clean_desc(desc: str) -> str:
    """B 站简介经常是单字符占位符（如 "-"），需要当成空处理。"""
    d = (desc or "").strip()
    return "" if d in ("-", "—", "－", "无", ".") else d


def make_summary(title: str, desc: str, min_len: int = 40, max_len: int = 100) -> str:
    """生成 40~100 字的内容简介（与抖音侧口径一致，便于前端统一展示）。"""
    text = _clean_desc(desc)
    if text:
        text = re.sub(r"\s+", " ", text).strip().rstrip("。，,")
    if len(text) >= min_len:
        return text if len(text) <= max_len else text[:max_len].rstrip() + "…"

    base = text or (title or "").strip()
    if not base:
        return "该视频发布于哔哩哔哩平台，可通过解析结果查看封面与标题了解具体内容。"
    filler = "，视频由 UP 主发布于哔哩哔哩平台，选择对应画质即可保存完整视频。"
    return (base + filler)[:max_len]


def _fmt_duration(secs: int) -> str:
    if not secs:
        return "--:--"
    m, s = divmod(int(secs), 60)
    if m >= 60:
        h, m = divmod(m, 60)
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m}:{s:02d}"


# ---------------------------------------------------------------- HTTP 客户端

_client: Optional[httpx.AsyncClient] = None


def _browser_headers(referer: str = "") -> Dict[str, str]:
    """构造「长得像真实 Chrome」的请求头。

    风控会拿请求头跟真实浏览器逐项比对。只给 ``UA + Referer`` 的请求即使内容
    完全合法也可能被判成脚本（HTTP 412），所以这里把 Chrome 首屏会带的那几组
    头补齐：``Accept`` / ``Origin`` / ``sec-ch-ua`` / ``sec-fetch-*``。

    :param referer: 传具体视频页（``https://www.bilibili.com/video/BV…/``）比只给
                    站点首页更"真"，风控宽松时单靠它就能过。留空则用首页。
    """
    return {
        "User-Agent": PC_UA,
        "Accept": "*/*",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        "Referer": referer or _HOME_URL,
        "Origin": "https://www.bilibili.com",
        "sec-ch-ua": '"Chromium";v="124", "Google Chrome";v="124", "Not-A.Brand";v="99"',
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": '"Windows"',
        "Sec-Fetch-Dest": "empty",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Site": "same-site",
    }


def video_referer(bvid: str = "") -> str:
    """该视频页地址；没有 BV 号时退回站点首页。"""
    return f"https://www.bilibili.com/video/{bvid}/" if bvid else _HOME_URL


def _headers(cookie: str = "", referer: str = "") -> Dict[str, str]:
    """**同步版**请求头，仅供共享客户端初始化与 CDN 下载使用。

    这里不含设备指纹——指纹要异步抓取并缓存，见 ``request_cookie``。需要指纹的
    接口调用请走 ``_api_get``，它会自己补上。
    """
    h = _browser_headers(referer)
    effective = resolve_cookie(cookie)
    if effective:
        h["Cookie"] = effective
    return h


def resolve_cookie(session_cookie: str = "") -> str:
    """决定这次请求用哪个**账号凭据**。

    优先级：**会话级（访客自己扫码登录的）> 全局（``.env`` 里配的）**。
    这样即使服务器配了统一的账号，某个访客用自己的账号登录后也能立刻
    用上自己账号的权益（比如大会员），而不会互相串号。

    注意这里只管账号凭据，设备指纹是另一回事，见 ``request_cookie``。
    """
    return (session_cookie or "").strip() or BILI_COOKIE


# ---------------------------------------------------------------- 设备指纹

#: 指纹缓存与并发去重锁
_fp_lock = asyncio.Lock()
_fp_cookie: str = ""
_fp_at: float = 0.0


def _random_buvid3() -> str:
    """按 B 站真实格式造一个 buvid3。

    真实值形如 ``BCE1BF7A-A9B2-2BA8-80BA-80EAD87E672362661infoc``，即
    「32 位十六进制 UUID + 若干数字 + ``infoc`` 后缀」。只有在首页都拿不到指纹
    时（典型就是 IP 已被风控）才会走到这里——一个格式合法的随机值比"完全不带
    这个 Cookie"更容易过检，失败也只会退化成原来的报错，没有副作用。
    """
    hx = "0123456789ABCDEF"
    u = "".join(secrets.choice(hx) for _ in range(32))
    tail = "".join(secrets.choice("0123456789") for _ in range(5))
    return f"{u[:8]}-{u[8:12]}-{u[12:16]}-{u[16:20]}-{u[20:]}{tail}infoc"


async def _fetch_fingerprint() -> str:
    """抓一套设备指纹 Cookie（``buvid3`` / ``buvid4`` / ``b_nut``）。

    两个来源互补，缺一不可：

    * **站点首页的 ``Set-Cookie``**——真实浏览器首次访问时边缘节点就会种下
      ``buvid3`` 与 ``b_nut``（首次访问时间戳）。拿到 ``b_nut`` 只有这一条路径，
      而它正是"这个请求来自一个真实访客"最直接的证据。
    * **``x/frontend/finger/spi``**——补 ``buvid4``。新版风控会校验它，只带
      ``buvid3`` 的请求在新策略下有一定概率被拦。

    每一步都「尽力而为」：失败就少几个字段，绝不在这里抛错。真的连不通，
    后续业务接口给出的错误更准确、更有指导性。
    """
    pairs: Dict[str, str] = {}
    h = _browser_headers()
    timeout = httpx.Timeout(10, read=20)

    try:
        async with httpx.AsyncClient(headers=h, timeout=timeout,
                                     follow_redirects=True) as c:
            await c.get(_HOME_URL)
            for k, v in c.cookies.items():
                pairs[k] = v
    except httpx.HTTPError:
        pass

    try:
        async with httpx.AsyncClient(headers=h, timeout=timeout,
                                     follow_redirects=True) as c:
            r = await c.get(_FINGER_API)
        if r.status_code == 200:
            data = (r.json() or {}).get("data") or {}
            # 首页已给的 buvid3 优先保留（两者等价，但首页那套跟 b_nut 是配套的）
            if data.get("b_3"):
                pairs.setdefault("buvid3", str(data["b_3"]))
            if data.get("b_4"):
                pairs["buvid4"] = str(data["b_4"])
    except (httpx.HTTPError, ValueError):
        pass

    # ``b_nut`` 就是「首次访问时间」，缺了按当前时间补一个（秒级时间戳）
    pairs.setdefault("b_nut", str(int(time.time())))
    pairs.setdefault("buvid3", _random_buvid3())
    return "; ".join(f"{k}={v}" for k, v in pairs.items() if v)


async def fingerprint() -> str:
    """取当前设备指纹（带缓存 + 并发去重）。

    首次调用会真的发两次请求（首页 + finger），之后 6 小时内零成本复用。
    """
    global _fp_cookie, _fp_at
    if _fp_cookie and (time.time() - _fp_at) < _FP_TTL:
        return _fp_cookie
    async with _fp_lock:
        if _fp_cookie and (time.time() - _fp_at) < _FP_TTL:   # 等锁期间可能已被刷新
            return _fp_cookie
        _fp_cookie = await _fetch_fingerprint()
        _fp_at = time.time()
    return _fp_cookie


async def reset_fingerprint() -> str:
    """丢弃缓存并重新抓一套指纹（命中 412 后调用，等价于"换台设备"）。"""
    global _fp_cookie, _fp_at
    async with _fp_lock:
        _fp_cookie = ""
        _fp_at = 0.0
    return await fingerprint()


def merge_cookie(fp: str, account: str) -> str:
    """把设备指纹与账号凭据合成一个 Cookie 串。

    **账号优先**：同名键（账号自己也会带 ``buvid3``）保留账号的值。否则换指纹会
    让登录态和新设备对不上，反而更容易被判为异常。指纹只负责补账号没有的键。
    """
    out: Dict[str, str] = {}
    for src in (fp, account):
        for item in (src or "").split(";"):
            item = item.strip()
            if not item or "=" not in item:
                continue
            k, v = item.split("=", 1)
            k, v = k.strip(), v.strip()
            if k and v:
                out[k] = v
    return "; ".join(f"{k}={v}" for k, v in out.items())


async def request_cookie(session_cookie: str = "") -> str:
    """本次请求实际要带的 Cookie = **设备指纹 + 账号凭据**（账号优先）。

    这是防 412 的第一道也是最重要的一道防线：未登录用户也必须有指纹，
    否则 B 站会直接把请求判成脚本。
    """
    return merge_cookie(await fingerprint(), resolve_cookie(session_cookie))


# ---------------------------------------------------------------- 请求节流

_throttle_lock = asyncio.Lock()
_last_call: float = 0.0


async def _throttle() -> None:
    """给 B 站接口请求套一个全局最小间隔。

    一次解析要连发近十个请求（view + 逐档 playurl + dash）。机房 IP 上这种
    「零间隔连发」是触发风控的主要原因之一，串行摊平后代价只有几百毫秒。
    间隔可用 ``BILI_MIN_INTERVAL`` 调整，设 0 关闭。
    """
    global _last_call
    if _MIN_INTERVAL <= 0:
        return
    async with _throttle_lock:
        wait = _MIN_INTERVAL - (time.monotonic() - _last_call)
        if wait > 0:
            await asyncio.sleep(wait)
        _last_call = time.monotonic()


async def startup() -> None:
    """应用启动时创建共享客户端（由 main.py 的 lifespan 调用）。"""
    global _client
    if _client is None:
        _client = httpx.AsyncClient(
            headers=_headers(), timeout=httpx.Timeout(20, read=60), follow_redirects=True
        )


async def shutdown() -> None:
    global _client
    if _client is not None:
        await _client.aclose()
        _client = None


def _ensure_client() -> httpx.AsyncClient:
    if _client is None:
        raise ParseError("解析服务未初始化")
    return _client


async def _api_get(api: str, params: Dict[str, Any], cookie: str = "",
                   referer: str = "") -> Dict[str, Any]:
    """调用 B 站接口并返回 JSON（不含业务错误判断）。

    这里集中处理两件与风控相关的事，调用方无需关心：

    1. **总是带设备指纹**——哪怕用户没登录。不带 ``buvid3`` 的裸请求是这个项目
       线上被 412 拦掉的头号原因。
    2. **412 自动换指纹重试**——命中风控说明当前这套指纹被盯上了，换一套新的
       （等价于"换台设备"）通常立刻恢复。重试仍失败才把错误交给用户。
    """
    client = _ensure_client()
    headers = _browser_headers(referer)
    headers["Cookie"] = await request_cookie(cookie)

    last_status = 0
    for attempt in range(_RETRY_412 + 1):
        await _throttle()
        try:
            r = await client.get(api, params=params, headers=headers)
        except httpx.HTTPError as e:
            raise ParseError("无法连接哔哩哔哩服务器，请检查网络后重试") from e

        last_status = r.status_code
        if last_status == 200:
            try:
                return r.json()
            except ValueError as e:
                raise ParseError("哔哩哔哩接口返回内容异常，请稍后重试") from e

        if last_status != 412:
            break                                   # 其它状态码重试也没意义

        if attempt < _RETRY_412:
            await asyncio.sleep(0.5 * (attempt + 1))    # 退避，别把风控喂饱
            await reset_fingerprint()
            headers["Cookie"] = await request_cookie(cookie)

    if last_status == 412:
        raise ParseError(
            "哔哩哔哩触发了风控校验（HTTP 412），已自动更换设备指纹重试仍未通过。"
            "通常是服务器出口 IP 被 B 站限制，或短时间内解析过于频繁。请稍后重试；"
            "若持续出现，建议在服务器上配置 BILI_COOKIE（带登录态能显著提升请求信誉度），"
            "或更换服务器出口 IP。"
        )
    raise ParseError(f"哔哩哔哩接口返回 HTTP {last_status}，请稍后重试")


# ---------------------------------------------------------------- 分P 与画质

async def _fetch_view(bvid: Optional[str] = None, aid: Optional[int] = None,
                      cookie: str = "") -> Dict[str, Any]:
    """获取稿件元信息。"""
    params: Dict[str, Any] = {"bvid": bvid} if bvid else {"aid": aid}
    # Referer 精确到视频页，风控判定时比站点首页更宽松
    body = await _api_get(_VIEW_API, params, cookie, referer=video_referer(bvid or ""))
    code = int(body.get("code") or 0)
    if code != 0:
        raise ParseError(
            _VIEW_ERRORS.get(code) or body.get("message") or f"解析失败（错误码 {code}）"
        )
    data = body.get("data")
    if not isinstance(data, dict) or not data.get("pages"):
        raise ParseError("未获取到视频信息，视频可能已失效")
    return data


async def _playurl(bvid: str, cid: int, qn: int, cookie: str = "") -> Optional[Dict[str, Any]]:
    """请求指定清晰度的播放地址；该档位不可用时接口会返回更低的 quality。

    返回 ``None`` 表示这一档没有拿到可下载的成品流（付费 / 无权限 / 仅多段流）。
    """
    body = await _api_get(
        _PLAYURL_API,
        {"bvid": bvid, "cid": cid, "qn": qn, "fnval": 1, "fnver": 0, "fourk": 1},
        cookie,
        referer=video_referer(bvid),
    )
    if int(body.get("code") or 0) != 0:
        return None
    data = body.get("data")
    if not isinstance(data, dict):
        return None
    durl = data.get("durl") or []
    # 多段流（早期 FLV 视频）无法在不解码的情况下合并成一个文件，直接跳过，
    # 避免用户下到只有第一段的残缺视频。
    if len(durl) != 1 or not durl[0].get("url"):
        return None
    return data


def _quality_label(qn: int, payload: Optional[Dict[str, Any]] = None) -> tuple:
    """返回 ``(短标签, 补充描述)``，优先用接口自己的描述文案。"""
    if payload:
        for f in payload.get("support_formats") or []:
            if int(f.get("quality") or 0) == qn:
                short = (f.get("display_desc") or "").strip()
                full = (f.get("new_description") or "").strip()
                if short or full:
                    short = short or full.split(" ")[0]
                    note = full
                    if short and note.startswith(short):
                        note = note[len(short):].strip()
                    return short, note or _QUALITY_META.get(qn, ("", ""))[1]
    meta = _QUALITY_META.get(qn)
    if meta:
        return meta
    return f"{qn}P", ""


async def _collect_qualities(bvid: str, cid: int, cookie: str = "") -> List[Dict[str, Any]]:
    """逐档请求，只保留接口**真实返回**的清晰度，从高到低排序。

    这样用户看到几档就一定能下到几档，不会出现「点了 1080P 实际给 720P」。

    传入登录凭据后，同一段代码会自动解锁更高档位——因为「哪些档位真的能拿到」
    完全由接口的返回决定，不需要为登录态单独写分支。

    **跳档优化（重要）**：接口被降级时会告诉我们"实际给到了哪一档"。比如拿
    qn=120 去问、返回 ``quality=64``，说明 (64, 120] 这一整段账号都拿不到，
    中间那些档位再问一遍纯属浪费——而每多一次请求就多一分被风控拦的风险。
    所以这里在降级时直接跳到「返回档位之下的第一档」继续问。

    未登录用户的实际请求数因此从 7 次降到 3 次（120→64、32、16），
    既省时间也显著降低了 412 概率。
    """
    tiers: Dict[int, Dict[str, Any]] = {}
    asked: set = set()                      # 已经发过请求的档位，避免重复问

    first_qn = _QN_CANDIDATES[0]
    first = await _playurl(bvid, cid, first_qn, cookie)
    if first is None:
        raise ParseError(
            "该视频未提供可直接下载的完整视频流，可能是付费内容、会员专属或受版权保护"
        )
    asked.add(first_qn)
    q0 = int(first.get("quality") or 0)
    if q0:
        tiers[q0] = first

    candidates = sorted(
        {int(q) for q in (first.get("accept_quality") or []) if int(q or 0) > 0},
        reverse=True,
    )

    def next_below(value: int) -> int:
        """候选表里第一个**低于** ``value`` 的下标（没有则返回末尾）。

        接口把请求从 q 降到 v，等于告诉我们 (v, q] 这一段账号全拿不到，
        下一次请求直接从 v 之下起手——中间那些档位问也是白问，
        而每多一次请求就多一分被风控拦的风险。
        """
        for k, c in enumerate(candidates):
            if c < value:
                return k
        return len(candidates)

    calls = 1
    # 首探通常直接问最高档（120）。若被降级到 q0，同样说明 (q0, 120] 拿不到，
    # 起手位置就该跳到 q0 之下，而不是从候选表头部一个个试。
    i = next_below(q0) if (q0 and q0 < first_qn) else 0
    while i < len(candidates) and calls < _MAX_PLAYURL_CALLS:
        qn = candidates[i]
        # ``asked`` 挡住首次探顶那个档位（探 120 被降到 64 时，120 并不在 tiers 里，
        # 不挡就会再问一次 120，纯属白打请求）；``tiers`` 挡住已被降级顺带拿到的档位。
        if qn in asked or qn in tiers:
            i += 1
            continue
        calls += 1
        asked.add(qn)
        payload = await _playurl(bvid, cid, qn, cookie)
        if payload is None:                 # 这一档彻底不可用，继续看下一档
            i += 1
            continue
        got = int(payload.get("quality") or 0)
        if got:
            tiers.setdefault(got, payload)
        # 如愿拿到（或档位未知）就正常往下走；被降级则跳过中间必然失败的档位
        i = i + 1 if (not got or got >= qn) else next_below(got)

    qualities: List[Dict[str, Any]] = []
    for qn, payload in tiers.items():
        durl0 = (payload.get("durl") or [{}])[0]
        label, note = _quality_label(qn, payload)
        # 主地址 + 备用地址一起给出，并把官方 CDN 排到第一位——前端只取第一个，
        # 而 /api/download 有域名白名单，PCDN 节点排前面会直接 400（见 prefer_official_cdn）
        urls = prefer_official_cdn([durl0.get("url")] + list(durl0.get("backup_url") or []))
        qualities.append({
            "key": f"qn{qn}",
            "qn": qn,
            "label": label,
            "note": note,
            "width": 0,          # 成品 MP4 不返回分辨率，留空由前端隐藏该字段
            "height": 0,
            "size_bytes": durl0.get("size"),
            "size_text": _fmt_size(durl0.get("size")),
            "codec": "MP4",
            "urls": urls,
            "duration_ms": int(durl0.get("length") or 0),
            "format": "mp4",     # 可直接下载，无需合并
            "needs_merge": False,
        })

    qualities.sort(key=lambda q: int(q["qn"]), reverse=True)
    if qualities:
        qualities[0]["best"] = True
    return qualities


def merge_formats(mp4: List[Dict[str, Any]],
                  dash: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """把 MP4 与 DASH 两类档位合并成一份列表。

    规则：**同一档位优先用 MP4**——它本身自带音轨，少一次服务端合并，下载更快、
    服务器负担也更小。DASH 只用来**补齐 MP4 拿不到的档位**（典型就是 1080P60 / 4K /
    HDR，这些只在 DASH 下提供）。

    DASH 档位还需**高于 MP4 的最高档**才会收录。否则会出现「720P 已有 MP4，
    却还列一个需要服务端合并的 480P」这种荒唐选项——用户要低画质直接用 MP4 就好，
    没道理为了更差的画质多等一次合并。

    这样普通视频（MP4 已覆盖全部档位）完全不会引入合并开销，而高画质需求也能满足。
    """
    have = {int(q.get("qn") or 0) for q in mp4}
    best_mp4 = max(have) if have else -1
    merged = list(mp4)
    merged.extend(d for d in dash
                  if int(d.get("qn") or 0) not in have
                  and int(d.get("qn") or 0) > best_mp4)

    merged.sort(key=lambda q: int(q.get("qn") or 0), reverse=True)
    for q in merged:
        q.pop("best", None)
    if merged:
        merged[0]["best"] = True
    # 注：DASH 档位不再往 note 里追加文案——前端已用角标「音视频合并」+ 按钮文案 +
    # 下方提示三处表达，再塞进 note 会把卡片撑成三行、按钮对不齐。
    return merged


# ---------------------------------------------------------------- DASH

def _ffmpeg_runs(path: str) -> bool:
    """真的执行一次 ``ffmpeg -version``，确认这个路径**跑得起来**。

    只判断「文件是否存在」是不够的。实测（Windows + WinGet 安装的 ffmpeg）：
    ``WinGet\\Links`` 目录下会留下一个 **0 字节的 reparse point**，
    ``shutil.which()`` 能找到它、``Path.exists()`` 也是 True，
    但真正执行会抛 ``OSError [WinError 193] 不是有效的 Win32 应用程序``。

    这类「占着名字却跑不起来」的情况若漏过检测，用户就会看到 DASH 档位、
    点下去、等上十几秒，最后拿到一个报错——正是 ``dash_supported`` 要避免的。
    """
    try:
        r = subprocess.run(
            [path, "-version"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=15,
        )
        return r.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def ffmpeg_path() -> Optional[str]:
    """返回**确认可执行**的 ffmpeg 路径；没有则返回 ``None``。

    合并 DASH 音视频必须依赖 ffmpeg。没装时不应该把 DASH 档位列给用户
    （否则点了必然失败），所以前端展示要以此为准（见 ``dash_supported``）。

    这里刻意做**真实执行探测**而非单纯的存在性检查，原因见 ``_ffmpeg_runs``。
    探测会起一个子进程（约几十毫秒），因此结果按 ``_FFMPEG_TTL`` 缓存；
    不能永久缓存——用户可能在服务运行期间才装上 ffmpeg。
    """
    global _ffmpeg_cache
    now = time.monotonic()
    if _ffmpeg_cache and (now - _ffmpeg_cache[1]) < _FFMPEG_TTL:
        return _ffmpeg_cache[0]

    candidates: List[str] = []
    if FFMPEG_BIN:
        candidates.append(FFMPEG_BIN)            # 显式配置优先，方便覆盖 PATH 里的坏路径
    found = shutil.which("ffmpeg")
    if found and found not in candidates:
        candidates.append(found)

    path = next((c for c in candidates if Path(c).exists() and _ffmpeg_runs(c)), None)
    _ffmpeg_cache = (path, now)
    if path is None and candidates:
        # 找到了名字却跑不起来，这是最容易让人困惑的情形，值得留下日志线索
        print(f"[bilibili] 检测到 ffmpeg 候选 {candidates} 但均无法执行，"
              f"DASH 高清档位将不展示。可用 FFMPEG_BIN 指定正确的可执行文件路径。",
              file=sys.stderr)
    return path


def dash_supported() -> bool:
    """当前环境能否提供 DASH 合并下载。"""
    return ffmpeg_path() is not None


def _stream_urls(s: Dict[str, Any]) -> List[str]:
    """取一条流的可用地址（主地址 + 备用 CDN），官方 CDN 排在前面。

    新接口用 ``baseUrl`` / ``backupUrl``，老接口用 ``base_url`` / ``backup_url``，
    两种都要兼容。排序原因见 ``prefer_official_cdn``——同一份返回里主地址可能
    落在第三方 PCDN 节点上，服务端逐条尝试时先打官方 CDN 成功率高得多。
    """
    urls: List[str] = []
    for k in ("baseUrl", "base_url"):
        if s.get(k):
            urls.append(str(s[k]))
    for k in ("backupUrl", "backup_url"):
        for u in (s.get(k) or []):
            if u:
                urls.append(str(u))
    return prefer_official_cdn(urls)


def _pick_audio(dash: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """挑一条音轨。

    只用 ``dash.audio``（AAC 系列），**不碰** ``dash.dolby`` / ``dash.flac``——
    杜比全景声（EC-3）和 Hi-Res 无损需要特殊解码器，浏览器和多数播放器放不了，
    强行选会让用户下到一个「能下不能放」的文件。
    """
    audios = [a for a in (dash.get("audio") or []) if _stream_urls(a)]
    if not audios:
        return None
    return max(audios, key=lambda a: int(a.get("bandwidth") or 0))


def pick_dash_streams(dash: Dict[str, Any], want_qn: Optional[int] = None,
                      want_codecid: Optional[int] = None):
    """挑出「一条视频流 + 一条音轨」，返回 ``(video, audio, qn)``。

    :param want_qn: 期望清晰度；该档位不存在时回落到可用的最高档。
    :param want_codecid: 期望编码；同档位有多个编码时用它精确匹配。
    """
    videos = [v for v in (dash.get("video") or []) if _stream_urls(v)]
    if not videos:
        return None, None, None

    ids = sorted({int(v.get("id") or 0) for v in videos if v.get("id")}, reverse=True)
    if not ids:
        return None, None, None
    qn = want_qn if want_qn in ids else ids[0]

    pool = [v for v in videos if int(v.get("id") or 0) == qn]
    if want_codecid:
        exact = [v for v in pool if int(v.get("codecid") or 0) == want_codecid]
        if exact:
            pool = exact
    # 同一档位可能有 H.264 / H.265 / AV1 多份，选兼容性最好的那份
    video = min(pool, key=lambda v: _CODEC_PRIORITY.get(int(v.get("codecid") or 0), 9))
    return video, _pick_audio(dash), qn


async def fetch_dash(bvid: str, cid: int, cookie: str = "") -> Optional[Dict[str, Any]]:
    """请求 DASH 播放信息。返回 ``data``（含 ``dash`` 节点），失败返回 ``None``。"""
    body = await _api_get(_PLAYURL_API, {
        "bvid": bvid, "cid": cid, "qn": _QN_CANDIDATES[0],
        "fnval": _DASH_FNVAL, "fnver": 0, "fourk": 1,
    }, cookie, referer=video_referer(bvid))
    if int(body.get("code") or 0) != 0:
        return None
    data = body.get("data")
    if not isinstance(data, dict):
        return None
    return data if isinstance(data.get("dash"), dict) else None


async def collect_dash_qualities(bvid: str, cid: int, cookie: str = "",
                                 duration: int = 0) -> List[Dict[str, Any]]:
    """列出 DASH 可用的清晰度（每条流一个档位）。

    DASH 的好处是**一次请求就能拿到全部可用档位**，不像 MP4 那样需要逐档试探，
    所以这里只发一次 playurl 请求。

    体积是**估算**的：用 ``bandwidth``（bit/s）× 时长 ÷ 8。DASH 不返回整段大小，
    但估算值足够帮用户判断该下哪个档位。分辨率则是真实值（比 MP4 更准）。
    """
    if not dash_supported():
        return []                                    # 没 ffmpeg 就别列，免得点了必失败

    data = await fetch_dash(bvid, cid, cookie)
    if not data:
        return []
    dash = data.get("dash") or {}
    audio = _pick_audio(dash)
    audio_bw = int((audio or {}).get("bandwidth") or 0)

    groups: Dict[int, List[Dict[str, Any]]] = {}
    for v in (dash.get("video") or []):
        qn = int(v.get("id") or 0)
        if qn and _stream_urls(v):
            groups.setdefault(qn, []).append(v)

    out: List[Dict[str, Any]] = []
    for qn, streams in groups.items():
        v = min(streams, key=lambda x: _CODEC_PRIORITY.get(int(x.get("codecid") or 0), 9))
        codecid = int(v.get("codecid") or 0)
        vbw = int(v.get("bandwidth") or 0)
        total_bw = vbw + audio_bw
        est = int(total_bw * duration / 8) if (total_bw and duration) else None
        label, note = _quality_label(qn)
        out.append({
            "key": f"dash{qn}",
            "qn": qn,
            "label": label,
            "note": note,
            "width": int(v.get("width") or 0),
            "height": int(v.get("height") or 0),
            "size_bytes": est,
            "size_text": _fmt_size(est) + ("（估算）" if est else ""),
            "codec": _CODEC_NAMES.get(codecid, f"codec{codecid}"),
            "urls": _stream_urls(v),                 # 仅供诊断展示，前端不用
            "format": "dash",
            "needs_merge": True,
            "codecid": codecid,
            "bandwidth": vbw,
        })
    out.sort(key=lambda q: q["qn"], reverse=True)
    return out


async def _download_stream(urls: List[str], dest: Path, cookie: str = "") -> None:
    """把一条流下载到本地文件。任一个地址成功即返回。"""
    if not urls:
        raise ParseError("视频流地址为空")
    client = _ensure_client()
    # CDN 校验防盗链只看 Referer，但带上设备指纹更接近真实播放器的请求
    headers = _browser_headers()
    jar = await request_cookie(cookie)
    if jar:
        headers["Cookie"] = jar
    last = "未知错误"
    for u in urls:
        try:
            async with client.stream("GET", u, headers=headers) as r:
                if r.status_code != 200:
                    last = f"HTTP {r.status_code}"
                    continue
                with open(dest, "wb") as f:
                    async for chunk in r.aiter_bytes(1 << 16):
                        f.write(chunk)
            if dest.exists() and dest.stat().st_size > 0:
                return
            last = "响应为空"
        except httpx.HTTPError as e:
            last = type(e).__name__
    raise ParseError(f"音视频流下载失败（{last}），直链可能已过期，请重新解析")


async def _run_ffmpeg(ff: str, vpath: Path, apath: Path, out: Path) -> None:
    """用 ffmpeg 无损合并音视频（``-c copy``，不重新编码，很快）。"""
    cmd = [
        ff, "-hide_banner", "-loglevel", "error", "-y",
        "-i", str(vpath),
        "-i", str(apath),
        "-map", "0:v:0", "-map", "1:a:0",             # 明确取第一条视频+第一条音频
        "-c", "copy",                                 # 无损封装，不转码
        "-movflags", "+faststart",                    # moov 前置，边下边播也流畅
        str(out),
    ]
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE,
        )
    except OSError as e:
        raise ParseError(f"无法启动 ffmpeg：{e}") from e

    _, err = await proc.communicate()
    if proc.returncode != 0 or not out.exists() or out.stat().st_size == 0:
        detail = (err or b"").decode("utf-8", "ignore").strip().splitlines()
        raise ParseError("音视频合并失败" + (f"：{detail[-1][:200]}" if detail else ""))


async def build_dash_file(bvid: str, cid: int, qn: int = 0, codecid: int = 0,
                          cookie: str = "") -> Path:
    """下载 DASH 音视频并合并成一个完整 MP4，返回临时文件路径。

    **调用方负责清理**（用 ``cleanup_dir(path.parent)``）——合并产物动辄上百 MB，
    不清理会迅速吃满磁盘。

    :param qn: 目标清晰度。0 表示取可用最高档。
    :param codecid: 目标编码。0 表示按兼容性自动挑（优先 H.264）。
    """
    ff = ffmpeg_path()
    if not ff:
        raise ParseError("服务器未安装 ffmpeg，无法合并 DASH 音视频流，请改用 MP4 档位")

    data = await fetch_dash(bvid, cid, cookie)
    if not data:
        raise ParseError("未获取到 DASH 播放信息，该视频可能不支持高清档位")
    dash = data.get("dash") or {}

    video, audio, got_qn = pick_dash_streams(dash, qn or None, codecid or None)
    if video is None:
        raise ParseError("该清晰度没有可用的视频流")
    if audio is None:
        raise ParseError("该清晰度没有可用的音轨，无法合并")

    tmpdir = Path(tempfile.mkdtemp(prefix="bilidash_"))
    try:
        vpath = tmpdir / "video.m4s"
        apath = tmpdir / "audio.m4s"
        out = tmpdir / "merged.mp4"
        await _download_stream(_stream_urls(video), vpath, cookie)
        await _download_stream(_stream_urls(audio), apath, cookie)
        await _run_ffmpeg(ff, vpath, apath, out)
        # 合并完就不需要源流了，尽早释放磁盘（大视频能省几百 MB）
        for p in (vpath, apath):
            try:
                p.unlink()
            except OSError:
                pass
        return out
    except Exception:
        cleanup_dir(tmpdir)
        raise


def cleanup_dir(d: Path) -> None:
    """删除临时目录（供下载接口在响应结束后调用，失败也不抛）。"""
    try:
        shutil.rmtree(d, ignore_errors=True)
    except Exception:
        pass


# ---------------------------------------------------------------- 诊断

async def debug_qualities(raw: str, page: Optional[int] = None,
                          cookie: str = "") -> Dict[str, Any]:
    """诊断「登录了但还是只有 720P」这类问题。

    逐档探测并原样回报接口的返回，同时用 DASH（fnval=4048）问一次
    「这个视频到底有哪些档位」——DASH 会把所有可用档位一次性列出来，
    因此它能告诉我们上限是多少，从而区分：

    * 视频本身就只到 720P（DASH 最高也只有 64）
    * 凭据没生效（DASH 最高 64 且 nav 显示未登录）
    * 凭据生效但没有更高权益（DASH 有 80/112，MP4 拿不到）

    返回值里**不含凭据明文**，可以安全贴出来。
    """
    text = (raw or "").strip()
    url = extract_share_url(text) or text
    bare_id = False
    if not url and is_bilibili_id(text):
        url, bare_id = text, True
    if not url:
        raise ParseError("未在输入内容中找到链接")

    client = _ensure_client()
    if not bare_id:
        if not re.match(r"^https?://", url, re.I):
            url = "https://" + url
        if is_bilibili_link(url) and is_bilibili_short_link(url):
            url = await _resolve_short(client, url)

    bvid = extract_bvid(url) or extract_bvid(text)
    aid = None if bvid else (extract_av_id(url) or extract_av_id(text))
    if not bvid and not aid:
        raise ParseError("无法识别视频编号")

    meta = await _fetch_view(bvid=bvid, aid=aid, cookie=cookie)
    bvid = meta.get("bvid") or bvid
    pages = meta.get("pages") or []
    want = page or extract_page(url) or extract_page(text) or 1
    idx = want - 1 if 1 <= want <= len(pages) else 0
    cur = pages[idx] if pages else {}
    cid = int(cur.get("cid") or meta.get("cid") or 0)

    session_cookie = (cookie or "").strip()
    # 把设备指纹的状态也报出来：排查 412 时，第一件要确认的就是「指纹到底拿到没有」。
    # 只回报键名与长度，不回明文（虽然指纹本身不敏感，但没必要外传）。
    fp = await fingerprint()
    fp_keys = [kv.split("=", 1)[0].strip() for kv in fp.split(";") if "=" in kv]
    out: Dict[str, Any] = {
        "bvid": bvid,
        "cid": cid,
        "title": (meta.get("title") or "")[:60],
        "duration_sec": int(cur.get("duration") or meta.get("duration") or 0),
        "cookie_source": "session(访客扫码)" if session_cookie else ("env(BILI_COOKIE)" if BILI_COOKIE else "none(未登录)"),
        "cookie_len": len(resolve_cookie(cookie)),
        "fingerprint": {
            "keys": fp_keys,
            "has_buvid3": "buvid3" in fp_keys,
            "has_buvid4": "buvid4" in fp_keys,
            "has_b_nut": "b_nut" in fp_keys,
            "cached_at": int(_fp_at),
            "ttl": int(_FP_TTL),
        },
        "nav": await fetch_nav(cookie),
        "mp4_probe": [],
    }

    # 1) 逐档问 MP4（durl，我们实际用来下载的格式）
    for qn in _QN_CANDIDATES:
        try:
            body = await _api_get(_PLAYURL_API, {
                "bvid": bvid, "cid": cid, "qn": qn,
                "fnval": 1, "fnver": 0, "fourk": 1,
            }, cookie, referer=video_referer(bvid))
        except ParseError as e:
            out["mp4_probe"].append({"ask": qn, "error": str(e)})
            continue
        data = body.get("data") if isinstance(body.get("data"), dict) else {}
        durl = data.get("durl") or []
        out["mp4_probe"].append({
            "ask": qn,
            "code": body.get("code"),
            "quality_got": data.get("quality"),
            "accept_quality": data.get("accept_quality"),
            "durl_segments": len(durl),
            "size_bytes": (durl[0].get("size") if durl else None),
        })

    # 2) 问一次 DASH：它会一次性列出该视频「所有可用档位」，用来看真实上限
    try:
        body = await _api_get(_PLAYURL_API, {
            "bvid": bvid, "cid": cid, "qn": 127,
            "fnval": 4048, "fnver": 0, "fourk": 1,
        }, cookie, referer=video_referer(bvid))
        data = body.get("data") if isinstance(body.get("data"), dict) else {}
        dash = data.get("dash") or {}
        vids = dash.get("video") or []
        got = sorted({int(v.get("id")) for v in vids if v.get("id")}, reverse=True)
        out["dash"] = {
            "code": body.get("code"),
            "accept_quality": data.get("accept_quality"),
            "video_qualities": got,
            "video_quality_names": [_quality_label(q)[0] for q in got],
            "has_audio": bool(dash.get("audio")),
        }
    except ParseError as e:
        out["dash"] = {"error": str(e)}

    # 3) 给出人话结论
    dash = out.get("dash") or {}
    dash_max = max(dash.get("video_qualities") or [0])
    dash_meta_max = max(dash.get("accept_quality") or [0])
    mp4_max = max([p.get("quality_got") or 0 for p in out["mp4_probe"]] or [0])
    label = lambda q: _quality_label(q)[0] if q else "—"   # noqa: E731
    nav = out["nav"]
    logged_in = bool(nav.get("is_login"))
    is_vip = bool(nav.get("vip"))

    if not logged_in:
        if out["cookie_source"].startswith("none"):
            out["verdict"] = (
                f"当前未登录，最高只能下 {label(mp4_max)}。"
                "扫码登录后是否解锁更高画质，取决于账号权益（见下表对照）。"
            )
        else:
            out["verdict"] = "凭据已失效或未生效（nav 显示未登录），请退出后重新扫码"
    elif not is_vip:
        # 这是最常见的情形：账号有效、但非大会员
        out["verdict"] = (
            f"凭据已生效（{nav.get('name')}，非大会员），最高只能下 {label(mp4_max)}。"
            "B 站对非大会员账号封顶 720P：1080P / 1080P60 / 4K / HDR 均需大会员。"
            + (f" 该视频最高有 {label(dash_meta_max)}，开通大会员后即可解锁。" if dash_meta_max > mp4_max else "")
        )
    elif mp4_max >= 80:
        out["verdict"] = f"一切正常，凭据已生效，最高可下 {label(mp4_max)}"
    elif dash_max > mp4_max or dash_meta_max > mp4_max:
        out["verdict"] = (
            f"凭据已生效（大会员），但 MP4 格式只给到 {label(mp4_max)}；"
            f"该视频还有 {label(max(dash_max, dash_meta_max))} 档位，只能通过 DASH 获取（音视频分离，需服务端合并）。"
        )
    else:
        out["verdict"] = (
            f"凭据已生效（大会员），但该视频本身最高只有 {label(mp4_max)}，不是登录问题。"
        )
    return out


# ---------------------------------------------------------------- 主流程

async def _resolve_short(client: httpx.AsyncClient, url: str) -> str:
    """b23.tv 短链 -> 真实长链。

    两种情况都要处理（实测都会遇到）：

    * 正常短链：返回 301/302，``Location`` 就是视频页；
    * 无效短码：返回 **HTTP 200**，响应体是 ``{"code":-404,"message":"啥都木有"}``，
      不会跳转。所以不能只看状态码，还要从响应体里再挖一次地址。
    """
    current = url
    for _ in range(6):
        try:
            r = await client.get(current, follow_redirects=False)
        except httpx.HTTPError as e:
            raise ParseError("短链解析失败，请重新复制分享链接后再试") from e

        if r.status_code in (301, 302, 303, 307, 308):
            loc = r.headers.get("location") or ""
            if not loc:
                break
            if not loc.startswith("http"):
                loc = "https://www.bilibili.com" + loc
            if extract_bvid(loc) or extract_av_id(loc):
                return loc
            current = loc
            continue

        if r.status_code == 200:
            hit = _dig_url_from_body(r.text)
            if hit:
                return hit
        break

    raise ParseError("短链已失效或无效，请在 B 站 App 里重新复制分享链接")


def _dig_url_from_body(text: str) -> Optional[str]:
    """从 b23 的 200 响应体里挖真实地址（兼容 JSON 与 HTML 两种形态）。"""
    text = (text or "")[:65536]
    if not text:
        return None
    m = re.search(r"https?://(?:www\.)?bilibili\.com/video/[^\s\"'<>\\]+", text)
    if m:
        return m.group(0).rstrip(_URL_TRAILING_CHARS)
    m = _BV_RE.search(text)
    if m:
        return f"https://www.bilibili.com/video/{'BV' + m.group(0)[2:]}"
    return None


async def parse_share_url(raw: str, page: Optional[int] = None,
                         cookie: str = "") -> Dict[str, Any]:
    """解析 B 站分享内容（BV 长链 / av 号 / b23.tv 短链 / 整段分享文字）。

    :param page: 指定分P（1 起）。不传则取链接里的 ``?p=`` ，再默认第 1 P。
    :param cookie: 访客会话级凭据。带上它才能解锁登录后可见的更高画质。
    """
    text = (raw or "").strip()
    if not text:
        raise ParseError("请输入哔哩哔哩视频链接")

    url = extract_share_url(text)
    bare_id = False
    if not url and is_bilibili_id(text):
        # 用户可能直接粘贴裸视频号（BV1GJ411x7h7 / av170001），这类输入没有域名，
        # 不走域名校验，交给下面的 extract_bvid / extract_av_id 处理。
        url = text
        bare_id = True
    if not url:
        raise ParseError("未在输入内容中找到链接，请粘贴 B 站「分享」得到的完整内容")
    if not bare_id:
        if not re.match(r"^https?://", url, re.I):
            url = "https://" + url
        if not is_bilibili_link(url):
            raise ParseError("这不是哔哩哔哩链接，请粘贴 b23.tv 或 www.bilibili.com 开头的分享链接")

    client = _ensure_client()

    # 1) 短链还原成含 BV 号的长链
    if is_bilibili_short_link(url):
        url = await _resolve_short(client, url)

    # 2) 定位视频标识
    bvid = extract_bvid(url) or extract_bvid(text)
    aid = None if bvid else (extract_av_id(url) or extract_av_id(text))
    if not bvid and not aid:
        raise ParseError("无法从链接中识别视频编号，请确认是完整的 B 站视频链接")

    meta = await _fetch_view(bvid=bvid, aid=aid, cookie=cookie)
    bvid = meta.get("bvid") or bvid

    # 3) 选择分P
    pages = meta.get("pages") or []
    want = page or extract_page(url) or extract_page(text) or 1
    idx = want - 1 if 1 <= want <= len(pages) else 0
    cur = pages[idx]
    cid = int(cur.get("cid") or meta.get("cid") or 0)
    if not cid:
        raise ParseError("未获取到视频分P信息，请稍后重试")

    # 4) 收集可下载画质（带凭据时接口会返回更高档位，无需另写分支）
    logged_in = bool(resolve_cookie(cookie))
    mp4_qualities = await _collect_qualities(bvid, cid, cookie)
    # DASH 补齐 MP4 拿不到的高阶档位（1080P60 / 4K / HDR 只在 DASH 下提供）。
    # 没装 ffmpeg 时 collect_dash_qualities 直接返回空，不会给用户列出点了必然失败的档位。
    duration = int(cur.get("duration") or meta.get("duration") or 0)
    dash_qualities = await collect_dash_qualities(bvid, cid, cookie, duration=duration)
    qualities = merge_formats(mp4_qualities, dash_qualities)

    # 5) 组装结果（字段与抖音侧保持一致）
    owner = meta.get("owner") or {}
    pic = (meta.get("pic") or "").strip()
    if pic.startswith("http://"):                       # 避免 https 页面出现混合内容
        pic = "https://" + pic[len("http://"):]

    desc = meta.get("desc") or ""
    title = (meta.get("title") or "").strip()
    part = (cur.get("part") or "").strip()
    cur_page = int(cur.get("page") or 1)
    duration = int(cur.get("duration") or meta.get("duration") or 0)

    # 多分P时把 P 名拼进标题，避免用户分不清下的是哪一集
    display_title = title
    if len(pages) > 1 and part and part != title:
        display_title = f"{title} — P{cur_page} {part}"

    return {
        "ok": True,
        "platform": "bilibili",
        "video_id": bvid,
        # DASH 档位需要 bvid + cid 由服务端重新申请直链（直链带时效签名，
        # 传给前端再传回来可能已过期），所以这两个字段必须暴露出去。
        "cid": cid,
        "title": display_title or "（无标题）",
        "summary": make_summary(title, desc),
        "cover": pic,
        "author": owner.get("name") or "未知UP主",
        "author_avatar": owner.get("face") or "",
        "duration_ms": duration * 1000,
        "duration_text": _fmt_duration(duration),
        "page_title": part,
        "current_page": cur_page,
        "total_pages": len(pages),
        # 本次解析是否带着登录凭据（前端据此决定要不要提示「登录解锁更高画质」）
        "logged_in": logged_in,
        "pages": [
            {
                "page": int(p.get("page") or i + 1),
                "cid": int(p.get("cid") or 0),
                "part": (p.get("part") or "").strip() or f"P{i + 1}",
                "duration_text": _fmt_duration(int(p.get("duration") or 0)),
            }
            for i, p in enumerate(pages)
        ],
        "qualities": qualities,
    }


# ================================================================ 扫码登录

_QR_GENERATE_API = "https://passport.bilibili.com/x/passport-login/web/qrcode/generate"
_QR_POLL_API = "https://passport.bilibili.com/x/passport-login/web/qrcode/poll"
_NAV_API = f"{_API}/x/web-interface/nav"

#: poll 接口的 data.code -> 内部状态。86101 未扫码 / 86090 已扫待确认 /
#: 86038 已失效 / 0 登录成功（实测确认）。
_QR_CODES = {
    0: "success",
    86101: "waiting",
    86090: "scanned",
    86038: "expired",
}

_QR_MESSAGES = {
    "success": "登录成功",
    "waiting": "请用哔哩哔哩 App 扫描二维码",
    "scanned": "已扫码，请在手机上确认登录",
    "expired": "二维码已失效，请点击刷新",
    "unknown": "二维码状态异常，请刷新重试",
}


def _qr_client() -> httpx.AsyncClient:
    """扫码登录专用客户端。

    刻意**不带任何已有 Cookie**：登录是要拿到新凭据，如果混入旧 Cookie，
    B 站可能直接按已登录返回，反而污染结果。
    """
    return httpx.AsyncClient(
        headers={"User-Agent": PC_UA, "Referer": "https://www.bilibili.com/",
                 "Accept-Language": "zh-CN,zh;q=0.9"},
        timeout=httpx.Timeout(15, read=30),
        follow_redirects=True,
    )


async def qr_generate() -> Dict[str, str]:
    """申请一个登录二维码。

    返回 ``{qrcode_key, url}``——``url`` 交给前端渲染成二维码给用户扫。
    """
    async with _qr_client() as c:
        try:
            r = await c.get(_QR_GENERATE_API)
        except httpx.HTTPError as e:
            raise ParseError("无法连接哔哩哔哩登录服务器，请检查网络后重试") from e
    if r.status_code != 200:
        raise ParseError(f"申请二维码失败（HTTP {r.status_code}），请稍后重试")
    try:
        body = r.json()
    except ValueError as e:
        raise ParseError("申请二维码失败，请稍后重试") from e
    if int(body.get("code") or 0) != 0:
        raise ParseError(body.get("message") or "申请二维码失败，请稍后重试")
    data = body.get("data") or {}
    key, url = data.get("qrcode_key"), data.get("url")
    if not key or not url:
        raise ParseError("申请二维码失败：接口未返回二维码信息")
    return {"qrcode_key": str(key), "url": str(url)}


async def qr_poll(qrcode_key: str) -> Dict[str, Any]:
    """查询扫码状态。登录成功时顺带取回凭据与账号信息。

    返回 ``{status, message, cookie?, user?}``，``status`` 取值见 ``_QR_CODES``。
    """
    async with _qr_client() as c:
        try:
            r = await c.get(_QR_POLL_API, params={"qrcode_key": qrcode_key})
        except httpx.HTTPError as e:
            raise ParseError("查询扫码状态失败，请检查网络后重试") from e
    if r.status_code != 200:
        raise ParseError(f"查询扫码状态失败（HTTP {r.status_code}）")
    try:
        body = r.json()
    except ValueError as e:
        raise ParseError("查询扫码状态失败，请稍后重试") from e

    data = body.get("data") or {}
    code = int(data.get("code") or 0) if body.get("code") == 0 else -1
    status = _QR_CODES.get(code, "unknown")
    out: Dict[str, Any] = {"status": status, "message": _QR_MESSAGES.get(status, "")}

    if status != "success":
        return out

    # 登录成功：凭据就在本次响应的 Set-Cookie 里
    cookie = _join_cookies(r)
    if not cookie:
        raise ParseError("登录成功但未取到凭据，请重试")
    out["cookie"] = cookie
    out["user"] = await fetch_nav(cookie)
    return out


def _join_cookies(r: httpx.Response) -> str:
    """把响应下发的多个 Cookie 拼成请求用的 Cookie 头。"""
    try:
        pairs = list(r.cookies.items())
    except Exception:
        pairs = []
    if not pairs:
        # 兜底：直接解析 Set-Cookie 头（正常路径用不到）
        for raw in r.headers.get_list("set-cookie"):
            first = raw.split(";")[0].strip()
            if "=" in first:
                pairs.append(tuple(first.split("=", 1)))
    return "; ".join(f"{k}={v}" for k, v in pairs if k and v)


async def fetch_nav(cookie: str) -> Dict[str, Any]:
    """查当前凭据对应的账号信息。凭据无效时返回 ``{"is_login": False}``。"""
    try:
        body = await _api_get(_NAV_API, {}, cookie)
    except ParseError:
        return {"is_login": False}
    data = body.get("data") or {}
    if not data.get("isLogin"):
        return {"is_login": False}
    return {
        "is_login": True,
        "name": data.get("uname") or "",
        "uid": str(data.get("mid") or ""),
        "avatar": data.get("face") or "",
        # vipStatus: 0 非会员 / 1 大会员；vipType 2 年度大会员
        "vip": int(data.get("vipStatus") or 0) == 1,
        "vip_label": "大会员" if int(data.get("vipStatus") or 0) == 1 else "普通用户",
        "level": int((data.get("level_info") or {}).get("current_level") or 0),
    }
