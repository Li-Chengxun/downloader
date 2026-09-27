"""抖音分享链接解析模块（双引擎）。

背景（重要）：2025 年后抖音分享页已不再在 ``window._ROUTER_DATA`` 里内嵌
视频数据（实测 ``videoInfoRes`` 恒为空），官方 detail 接口又需要 ``a_bogus``
签名。因此解析链路改为双引擎：

引擎 A（主）：调用 Evil0ctal/Douyin_TikTok_Download_API 的 API 实例
    默认公共实例 ``api.douyin.wtf``（demo 账号有限流），可通过环境变量
    ``DTK_BASE_URL`` / ``DTK_USERNAME`` / ``DTK_PASSWORD`` 指向自托管实例。
    端点：``GET /api/v1/douyin/video?url=...&wait=true``，必要时降级为
    异步任务轮询。返回 ``media.streams`` 多码率列表（540P ~ 4K）。

引擎 B（备用）：本地分享页解析
    短链 301 重定向 → iesdouyin 分享页 → 提取 ``window._ROUTER_DATA``
    → ``loaderData.videoInfoRes.item_list[0]``。若抖音未来恢复 SSR 数据
    或对爬虫 UA 放行，此引擎可独立工作。

两个引擎都直接取平台发布的干净视频流，不做二次转码，画质无损。
"""

from __future__ import annotations

import asyncio
import json
import os
import re
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

import httpx

MOBILE_UA = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_2 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.2 "
    "Mobile/15E148 Safari/604.1"
)

# ---------------------------------------------------------------- 引擎 A 配置

DTK_BASE_URL = os.environ.get("DTK_BASE_URL", "https://api.douyin.wtf").rstrip("/")
DTK_USERNAME = os.environ.get("DTK_USERNAME", "demo")
DTK_PASSWORD = os.environ.get("DTK_PASSWORD", "kelp-kelp-delta-3690")

_TASK_TERMINAL = {"done", "succeeded", "failed"}

_VIDEO_ID_RE = re.compile(r"(?:video|note)/(\d{6,})")
# 网页版分享常见形态：?modal_id=xxx / object_id=xxx / aweme_id=xxx
_MODAL_ID_RE = re.compile(r"[?&](?:modal_id|aweme_id|object_id)=(\d{6,})")
# 从分享文本中提取 http(s) 链接（到中文或空白为止）
_URL_IN_TEXT_RE = re.compile(r"https?://[^\s\u4e00-\u9fff\uff00-\uffef]+", re.I)
# 无协议裸链接（用户偶尔只复制了域名部分）
_BARE_URL_RE = re.compile(
    r"(?:v\.douyin\.com|www\.douyin\.com|douyin\.com|www\.iesdouyin\.com|iesdouyin\.com)/[^\s\u4e00-\u9fff]*",
    re.I,
)
_DOUYIN_HOST_SUFFIXES = ("douyin.com", "iesdouyin.com")
# 链接结尾常见的标点/括号，提取时需要剥掉
_URL_TRAILING_CHARS = "，。、！？；：）】》」』\"'.,!?;:)]}>"

_ROUTER_DATA_RE = re.compile(r"window\._ROUTER_DATA\s*=\s*(\{.*?\})\s*</script>", re.S)

SHARE_TPL = [
    "https://www.iesdouyin.com/share/video/{vid}/",
    "https://www.iesdouyin.com/share/note/{vid}/",
]

# 清晰度标签推断：(gear_name 关键词, 短边分辨率下限, 标签)，顺序即优先级
_LABEL_RULES = [
    ("4k", 2000, "4K"),
    ("2k", 1400, "2K"),
    ("1080", 1000, "1080P"),
    ("720", 700, "720P"),
    ("540", 500, "540P"),
]

_CODEC_MAP = {"h264": "H.264", "h265": "H.265/HEVC", "hevc": "HEVC", "av1": "AV1"}


class ParseError(Exception):
    """业务解析错误，message 会直接展示给用户。"""


# ---------------------------------------------------------------- 基础工具

def extract_video_id(url: str) -> Optional[str]:
    """从任意形态的抖音链接中提取视频 ID。

    覆盖 ``/video/{id}``、``/note/{id}``、``?modal_id={id}``、``?object_id={id}``
    等网页版分享的常见形态。
    """
    text = url or ""
    m = _VIDEO_ID_RE.search(text) or _MODAL_ID_RE.search(text)
    return m.group(1) if m else None


def _host_of(url: str) -> str:
    try:
        return (urlparse(url).hostname or "").lower()
    except Exception:
        return ""


def is_douyin_link(url: str) -> bool:
    """判断链接是否属于抖音系域名。"""
    host = _host_of(url)
    return any(host == s or host.endswith("." + s) for s in _DOUYIN_HOST_SUFFIXES)


def extract_share_url(text: str) -> str:
    """从分享内容中提取纯链接。

    抖音「复制链接」实际复制出的常是「口令 + 链接 + 提示语」的整段文字，例如::

        8.88 abc:/ 复制打开抖音，看看【某作者的作品】…
        https://v.douyin.com/xxxx/ 复制此链接，打开 Dou 音搜索…

    这里把其中的链接单独抽出来；若文本本身就是纯链接则原样返回。
    """
    text = (text or "").strip()
    if not text:
        return ""

    if re.match(r"^https?://\S+$", text, re.I):          # 本身就是纯链接
        return text.rstrip(_URL_TRAILING_CHARS)

    first_any = ""
    for m in _URL_IN_TEXT_RE.finditer(text):
        u = m.group(0).rstrip(_URL_TRAILING_CHARS)
        if is_douyin_link(u):
            return u
        if not first_any:
            first_any = u
    if first_any:
        return first_any

    m = _BARE_URL_RE.search(text)                        # 无协议的裸域名链接
    if m:
        return "https://" + m.group(0).rstrip(_URL_TRAILING_CHARS)
    return ""


async def resolve_canonical_url(url: str, client: Optional[httpx.AsyncClient] = None) -> str:
    """把任意抖音链接规范化为含视频 ID 的长链。

    解析引擎（Evil0ctal API）只接受 ``www.douyin.com/video/{id}`` 这类规范
    长链，直接传 ``v.douyin.com`` 短链会返回 400（missing content_id）。
    因此这里先跟随 301/302 重定向，从跳转目标里取出视频 ID。
    """
    url = (url or "").strip()
    vid = extract_video_id(url)
    if vid:
        return f"https://www.douyin.com/video/{vid}"

    own = client is None
    if own:
        client = httpx.AsyncClient(
            headers={"User-Agent": MOBILE_UA, "Accept-Language": "zh-CN,zh;q=0.9"},
            timeout=15,
            follow_redirects=False,
        )
    try:
        current = url
        for _ in range(6):
            r = await client.get(current, follow_redirects=False)
            loc = r.headers.get("location") or ""
            if not loc:
                break
            if not loc.startswith("http"):
                loc = "https://www.douyin.com" + loc
            vid = extract_video_id(loc)
            if vid:
                return f"https://www.douyin.com/video/{vid}"
            current = loc
        vid = extract_video_id(current)
        return f"https://www.douyin.com/video/{vid}" if vid else current
    except httpx.HTTPError as e:
        raise ParseError("链接已失效或网络异常，请重新复制分享链接后再试") from e
    finally:
        if own:
            await client.aclose()


def make_summary(desc: str, min_len: int = 40, max_len: int = 100) -> str:
    """根据标题/描述生成 50-100 字左右的内容简介。

    标题（desc）足够完整时直接使用；过短则拼接补充说明。
    后期可在这一步替换为 AI 智能摘要接口。
    """
    text = re.sub(r"#\S+", "", desc or "")            # 去掉话题标签
    text = re.sub(r"\s+", " ", text).strip().rstrip("。，,")
    if len(text) >= min_len:
        if len(text) <= max_len:
            return text
        return text[:max_len].rstrip() + "…"
    if not text:
        return "该视频发布于抖音平台，可通过解析结果查看封面与标题了解具体内容。"
    filler = "，视频由作者发布于抖音平台，选择对应清晰度即可保存原画质的完整视频。"
    return (text + filler)[:max_len]


def _fmt_size(n: Optional[int]) -> str:
    if not n:
        return "大小未知"
    mb = n / 1024 / 1024
    return f"{mb:.1f} MB" if mb >= 1 else f"{n / 1024:.0f} KB"


def _first_url(obj: Optional[Dict[str, Any]]) -> str:
    """从 {url, urls[]} 形态的字段中取第一个可用地址。"""
    if not obj:
        return ""
    if obj.get("url"):
        return obj["url"]
    urls = obj.get("urls") or []
    return urls[0] if urls else ""


def _label_from_dims(width: int, height: int, gear: str = "") -> str:
    """按短边分辨率推断清晰度标签（竖屏 1080x1920 -> 1080P）。"""
    gear = (gear or "").lower()
    base = min(width, height) if width and height else (height or width)
    if "lowest" in gear:
        return "最低"
    for key, floor, label in _LABEL_RULES:
        if key in gear or base >= floor:
            return label
    if "highest" in gear or "adaptive" in gear or "adapt" in gear:
        return "自适应"
    return f"{base}P" if base else "默认"


def _dedupe_sort(qualities: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """按清晰度标签去重（同档保留体积最大者），按短边降序，最高档标记 best。"""
    out: Dict[str, Dict[str, Any]] = {}
    for q in qualities:
        old = out.get(q["label"])
        if old is None or (q["size_bytes"] or 0) >= (old["size_bytes"] or 0):
            out[q["label"]] = q
    result = list(out.values())
    result.sort(key=lambda q: min(q.get("width") or 0, q.get("height") or 0), reverse=True)
    if result:
        result[0]["best"] = True
    return result


def _build_result(vid: str, title: str, desc: str, cover: str, nickname: str,
                  avatar: str, duration_ms: int, qualities: List[Dict[str, Any]]) -> Dict[str, Any]:
    secs = duration_ms // 1000
    return {
        "ok": True,
        "video_id": vid,
        "title": title or "（无标题）",
        "summary": make_summary(desc or title),
        "cover": cover,
        "author": nickname or "未知作者",
        "author_avatar": avatar,
        "duration_ms": duration_ms,
        "duration_text": f"{secs // 60}:{secs % 60:02d}" if secs else "--:--",
        "qualities": _dedupe_sort(qualities),
    }


# ================================================================ 引擎 A：Evil0ctal API

_wtf_client: Optional[httpx.AsyncClient] = None
_wtf_logged_in = False
_wtf_lock = asyncio.Lock()


async def _wtf_ensure_login(client: httpx.AsyncClient, force: bool = False) -> None:
    global _wtf_logged_in
    async with _wtf_lock:
        if _wtf_logged_in and not force:
            return
        r = await client.post(
            f"{DTK_BASE_URL}/api/v1/auth/login",
            json={"username": DTK_USERNAME, "password": DTK_PASSWORD},
        )
        if r.status_code != 200:
            raise ParseError(f"解析引擎登录失败（HTTP {r.status_code}），请检查 DTK_* 配置")
        _wtf_logged_in = True


async def _wtf_wait_task(client: httpx.AsyncClient, task_id: str) -> Dict[str, Any]:
    """轮询异步任务直到完成（最长约 25 秒）。"""
    for _ in range(20):
        await asyncio.sleep(1.2)
        r = await client.get(f"{DTK_BASE_URL}/api/v1/tasks/{task_id}")
        if r.status_code == 401:
            await _wtf_ensure_login(client, force=True)
            continue
        if r.status_code != 200:
            continue
        t = (r.json().get("data") or {})
        state = t.get("state")
        if state in _TASK_TERMINAL:
            if state == "failed":
                err = (t.get("error") or {})
                raise ParseError(err.get("message") or "解析任务失败，视频可能不存在或已删除")
            payload = t.get("data")
            if isinstance(payload, dict):
                return payload
    raise ParseError("解析任务超时，请稍后重试")


async def parse_via_wtf(url: str) -> Dict[str, Any]:
    """引擎 A：调用 Evil0ctal API 解析分享链接。"""
    global _wtf_logged_in
    if _wtf_client is None:
        raise ParseError("解析引擎未初始化")

    await _wtf_ensure_login(_wtf_client)
    # wait=30：服务端同步等待最多 30 秒直接返回结果（float 类型）
    r = await _wtf_client.get(
        f"{DTK_BASE_URL}/api/v1/douyin/video",
        params={"url": url, "wait": 30},
    )
    if r.status_code == 401:                      # 会话过期，重登一次
        await _wtf_ensure_login(_wtf_client, force=True)
        r = await _wtf_client.get(
            f"{DTK_BASE_URL}/api/v1/douyin/video",
            params={"url": url, "wait": 30},
        )
    if r.status_code == 429:
        raise ParseError("解析请求过于频繁（公共演示接口限流），请稍等几秒再试")
    if r.status_code in (400, 404):
        # 引擎把「链接无效 / 视频不存在」也归到 400/404，转成对用户友好的提示
        raise ParseError("未解析到视频：链接可能已失效，或视频已被删除、设为私密")
    if r.status_code >= 400:
        raise ParseError(f"解析引擎返回 HTTP {r.status_code}，请稍后重试")

    body = r.json()
    if not body.get("success"):
        err = body.get("error") or {}
        raise ParseError(err.get("message") or "解析引擎返回错误")

    data = body.get("data") or {}
    # wait=true 时直接带结果；否则返回 {task_id, state} 需要轮询
    if "task_id" in data and data.get("state") not in _TASK_TERMINAL:
        data = await _wtf_wait_task(_wtf_client, data["task_id"])

    payload = data.get("data") if isinstance(data.get("data"), dict) else data
    if not payload:
        raise ParseError("解析引擎未返回视频数据")

    return _map_wtf_payload(payload)


def _map_wtf_payload(p: Dict[str, Any]) -> Dict[str, Any]:
    if p.get("is_deleted") or p.get("is_private"):
        raise ParseError("该视频已被删除或设为私密")

    media = p.get("media") or {}
    video = media.get("video") or {}
    author = p.get("author") or {}

    vid = str(p.get("content_id") or "")
    title = (p.get("title") or "").strip()
    desc = (p.get("description") or "").strip()
    duration = int(p.get("duration_ms") or 0)

    cover = _first_url(_largest(media.get("covers") or [])) or _first_url(video)
    avatar = _first_url(author.get("avatar") or {})

    qualities: List[Dict[str, Any]] = []
    for s in media.get("streams") or []:
        # dash/webm 等非 mp4 流不是可直接下载的成品文件，优先排除
        if (s.get("format") or "").lower() not in ("mp4", ""):
            continue
        urls = ([s["url"]] if s.get("url") else []) + list(s.get("urls") or [])
        if not urls:
            continue
        w = int(s.get("width") or 0)
        h = int(s.get("height") or 0)
        fmt = (s.get("format") or "mp4").lower()
        qualities.append({
            "key": f"{w}x{h}-{s.get('bitrate') or 0}",
            "label": _label_from_dims(w, h),
            "width": w,
            "height": h,
            "size_bytes": s.get("size_bytes"),
            "size_text": _fmt_size(s.get("size_bytes")),
            "codec": _CODEC_MAP.get(fmt, fmt.upper() or "MP4"),
            "urls": urls,
        })

    # streams 为空时兜底用 media.video
    if not qualities and _first_url(video):
        w = int(video.get("width") or 0)
        h = int(video.get("height") or 0)
        qualities.append({
            "key": "default",
            "label": _label_from_dims(w, h),
            "width": w,
            "height": h,
            "size_bytes": video.get("size_bytes"),
            "size_text": _fmt_size(video.get("size_bytes")),
            "codec": "MP4",
            "urls": [u for u in ([video.get("url")] + list(video.get("urls") or [])) if u],
        })

    if not qualities:
        raise ParseError("未获取到可用的视频流地址")

    return _build_result(vid, title, desc, cover, author.get("nickname") or "",
                         avatar, duration, qualities)


def _largest(covers: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """取分辨率最大的一张封面。"""
    best, best_area = None, -1
    for c in covers:
        area = int(c.get("width") or 0) * int(c.get("height") or 0)
        if area > best_area and _first_url(c):
            best, best_area = c, area
    return best


# ================================================================ 引擎 B：本地分享页解析

async def _resolve_short_url(client: httpx.AsyncClient, url: str) -> str:
    """短链 301 重定向 -> 真实 URL。"""
    r = await client.get(url, follow_redirects=False)
    if r.status_code in (301, 302, 303, 307, 308):
        loc = r.headers.get("location", "")
        if loc:
            return loc if loc.startswith("http") else f"https://www.douyin.com{loc}"
    return url


async def _fetch_share_html(client: httpx.AsyncClient, vid: str) -> Optional[str]:
    """请求移动端分享页，返回包含 _ROUTER_DATA 的 HTML。"""
    for tpl in SHARE_TPL:
        try:
            r = await client.get(tpl.format(vid=vid), follow_redirects=True)
        except httpx.HTTPError:
            continue
        if r.status_code == 200 and "_ROUTER_DATA" in r.text:
            return r.text
    return None


def _parse_router_data(html: str) -> Dict[str, Any]:
    """从分享页 HTML 中提取 _ROUTER_DATA 并返回 item_list[0]。"""
    m = _ROUTER_DATA_RE.search(html)
    raw = None
    if m:
        raw = m.group(1)
    else:
        # 兜底：花括号配平提取（防止 JSON 后还有其它语句导致正则失效）
        i = html.find("window._ROUTER_DATA")
        if i >= 0:
            start = html.find("{", i)
            if start >= 0:
                raw = _balanced_json(html, start)
    if not raw:
        raise ParseError("分享页中未找到 _ROUTER_DATA，抖音页面结构可能已变化")
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        raise ParseError("_ROUTER_DATA JSON 解析失败") from e

    loader = data.get("loaderData") or {}
    if not loader:
        raise ParseError("_ROUTER_DATA 中缺少 loaderData")
    page = loader[next(iter(loader))] or {}
    res = page.get("videoInfoRes") or {}
    items = res.get("item_list") or []
    if not items:
        toast = res.get("toast")
        raise ParseError(
            (toast if isinstance(toast, str) and toast else "")
            or "未获取到视频信息：视频可能已被删除、设为私密，或被风控拦截"
        )
    return items[0]


def _balanced_json(text: str, start: int) -> Optional[str]:
    """从 start（指向 '{'）开始做花括号配平，返回完整 JSON 串。"""
    depth, in_str, escape = 0, False, False
    for i in range(start, len(text)):
        ch = text[i]
        if in_str:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[start:i + 1]
    return None


def _parse_local_item(item: Dict[str, Any]) -> Dict[str, Any]:
    """把本地 _ROUTER_DATA 的 item_list[0] 映射为标准结果。"""
    video = item.get("video") or {}
    author = item.get("author") or {}
    desc = (item.get("desc") or "").strip()
    duration = int(item.get("duration") or video.get("duration") or 0)

    cover = ""
    for k in ("origin_cover", "cover", "dynamic_cover"):
        urls = (video.get(k) or {}).get("url_list") or []
        if urls:
            cover = urls[0]
            break

    qualities: List[Dict[str, Any]] = []
    for br in video.get("bit_rate") or []:
        pa = br.get("play_addr") or {}
        urls = pa.get("url_list") or []
        if not urls:
            continue
        w = int(pa.get("width") or 0)
        h = int(pa.get("height") or 0)
        size = pa.get("data_size")
        if not size and duration and br.get("bit_rate"):
            size = int(br["bit_rate"] * duration / 8)
        codec_raw = (br.get("codec_type") or "").lower()
        qualities.append({
            "key": br.get("gear_name") or f"{w}x{h}",
            "label": _label_from_dims(w, h, br.get("gear_name") or ""),
            "width": w,
            "height": h,
            "size_bytes": size,
            "size_text": _fmt_size(size),
            "codec": _CODEC_MAP.get(codec_raw, "H.264"),
            "urls": urls,
        })
    if not qualities:
        pa = video.get("play_addr") or {}
        urls = pa.get("url_list") or []
        if urls:
            w = int(pa.get("width") or 0)
            h = int(pa.get("height") or 0)
            qualities.append({
                "key": "default",
                "label": _label_from_dims(w, h),
                "width": w,
                "height": h,
                "size_bytes": pa.get("data_size"),
                "size_text": _fmt_size(pa.get("data_size")),
                "codec": "H.264",
                "urls": urls,
            })
    if not qualities:
        raise ParseError("未获取到可用的视频流地址")

    avatar_urls = (author.get("avatar_thumb") or {}).get("url_list") or []
    return _build_result(str(item.get("aweme_id") or ""), desc, desc, cover,
                         author.get("nickname") or "", avatar_urls[0] if avatar_urls else "",
                         duration, qualities)


async def parse_via_share_page(url: str) -> Dict[str, Any]:
    """引擎 B：短链重定向 + 分享页 _ROUTER_DATA 本地解析。"""
    headers = {
        "User-Agent": MOBILE_UA,
        "Accept-Language": "zh-CN,zh;q=0.9",
        "Referer": "https://www.douyin.com/",
    }
    async with httpx.AsyncClient(headers=headers, timeout=15, follow_redirects=False) as client:
        if "v.douyin.com" in url or "iesdouyin.com/share" in url:
            url = await _resolve_short_url(client, url)
        vid = extract_video_id(url)
        if not vid:
            raise ParseError("无法从链接中识别视频 ID，请确认是完整的抖音分享链接")
        html = await _fetch_share_html(client, vid)
    if html is None:
        raise ParseError("分享页请求失败（可能被风控），请稍后重试")
    item = _parse_router_data(html)
    return _parse_local_item(item)


# ================================================================ 主入口

async def parse_share_url(raw: str) -> Dict[str, Any]:
    """解析抖音分享内容（短链 / 长链 / 整段分享文字均可）。

    流程：提取链接 → 短链还原为规范长链 → 引擎 A（Evil0ctal API），
    失败时降级引擎 B（本地分享页解析）；两者都失败则抛出信息更明确的引擎 A 错误。
    """
    text = (raw or "").strip()
    if not text:
        raise ParseError("请输入抖音分享链接")

    url = extract_share_url(text)
    if not url:
        raise ParseError("未在输入内容中找到链接，请粘贴抖音「复制链接」得到的完整内容")
    if not re.match(r"^https?://", url, re.I):
        url = "https://" + url
    if not is_douyin_link(url):
        raise ParseError("这不是抖音链接，请粘贴 v.douyin.com 或 www.douyin.com 开头的分享链接")

    # 关键一步：短链先还原成规范长链（解析引擎只认规范链接）
    canonical = await resolve_canonical_url(url)

    error_a: Optional[Exception] = None
    try:
        return await parse_via_wtf(canonical)
    except ParseError as e:
        error_a = e
    except httpx.HTTPError as e:
        error_a = e

    # 引擎 B 降级
    try:
        return await parse_via_share_page(canonical)
    except Exception:
        if isinstance(error_a, ParseError):
            raise error_a
        raise ParseError("无法连接解析引擎，请检查网络后重试") from error_a


async def startup() -> None:
    """应用启动时初始化引擎 A 的 HTTP 客户端（由 main.py lifespan 调用）。"""
    global _wtf_client
    if _wtf_client is None:
        _wtf_client = httpx.AsyncClient(timeout=httpx.Timeout(30, read=90))


async def shutdown() -> None:
    global _wtf_client
    if _wtf_client is not None:
        await _wtf_client.aclose()
        _wtf_client = None
