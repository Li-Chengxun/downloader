"""抖音 / 哔哩哔哩 视频下载站后端（FastAPI）。

接口一览：
- GET  /api/parse?url=&p=            解析分享链接（自动识别平台），返回视频信息与清晰度列表
- GET  /api/download?url=&filename=  代理流式下载视频（绕过 Referer 防盗链、触发保存）
- POST /api/images/zip               把图文帖的图片打包成一个 zip 下载
- POST /api/slideshow                把图文帖的图片合成为一个幻灯片视频（服务端 ffmpeg）
- GET  /api/cover?url=...            代理封面图（抖音图床对 Referer 有限制，B 站封面是 http 需转 https）
- GET  /api/bili/login              查询当前会话的 B 站登录状态
- POST /api/bili/login/qr           申请登录二维码（返回待扫码的 url）
- GET  /api/bili/login/qr/poll      轮询扫码状态；成功即把凭据写入本会话
- POST /api/bili/logout             退出登录（只清本会话，不影响别人）
- GET  /                             前端页面（static/index.html）

两个平台各自一个解析模块：
- ``parser.py``   抖音（双引擎：Evil0ctal API + 本地分享页）
- ``bilibili.py`` 哔哩哔哩（官方 web 接口，durl 完整 MP4；内部再分普通投稿 UGC 与番剧 PGC）
- ``slideshow.py`` 图文帖合成视频 / 打包图片（抖音图文专用）
- ``ffmpeg_tool.py`` ffmpeg 定位与探测（DASH 合并与图文合成共用）
对外返回的数据结构一致，前端复用同一张结果卡片。``kind`` 字段区分渲染方式：
``video``（默认）/ ``bangumi``（番剧）/ ``images``（图文帖）/ ``text``（纯文字帖）。

关于扫码登录的凭据归属（重要）：
B 站未登录时只能下到 720P，登录后可解锁 1080P 及以上。凭据按**浏览器会话**
隔离存放在服务端内存里（见 ``sessions.py``），不会下发给前端，也不会跨访客
共用——A 扫码不会让 B 也用 A 的账号。语义上是「访客自己的登录态」，
与 ``.env`` 里配置的 ``BILI_COOKIE``（服务端级全局账号）是两回事，后者优先级更低。
"""

from __future__ import annotations

import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import List, Optional
from urllib.parse import quote

import httpx
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
# FastAPI 只导出 ``BackgroundTasks``（复数），单个任务类要从 starlette 取
from starlette.background import BackgroundTask

import bilibili as bili
import ffmpeg_tool
import parser as douyin
import sessions as session_store
import slideshow

BASE_DIR = Path(__file__).resolve().parent


def _extra_hosts(env_name: str) -> tuple:
    """读取额外的放通域名（逗号分隔）。CDN 域名变动时可临时补充，一般无需配置。"""
    raw = os.environ.get(env_name, "")
    return tuple(h.strip().lower() for h in raw.split(",") if h.strip())


# 各平台 CDN 域名关键字（防开放代理滥用）
_PLATFORM_HOSTS = {
    "douyin": (
        "douyin", "bytedance", "bytecdn", "zjcdn", "douyinvod", "toutiao", "pstatp",
    ) + _extra_hosts("EXTRA_DOUYIN_HOSTS"),
    "bilibili": (
        "bilivideo", "bilibili", "hdslb", "akamaized.net", "mcdn", "upos",
    ) + _extra_hosts("EXTRA_BILIBILI_HOSTS"),
}

# 直链请求头：两个平台的防盗链要求不同，必须分开
_DL_HEADERS = {
    "douyin": {"User-Agent": douyin.MOBILE_UA, "Referer": "https://www.douyin.com/"},
    "bilibili": {"User-Agent": bili.PC_UA, "Referer": "https://www.bilibili.com/"},
}

_PARSE_ERRORS = (douyin.ParseError, bili.ParseError)

#: 服务端会话表（内存）：把扫码登录的凭据按访客隔离，见 sessions.py
sessions = session_store.SessionStore()


class ImageItem(BaseModel):
    """图文帖里的单张图片，字段与 ``/api/parse`` 返回的 ``images[]`` 对齐。"""

    url: str = ""
    urls: List[str] = Field(default_factory=list)
    width: int = 0
    height: int = 0
    ext: str = ""


class PackRequest(BaseModel):
    """打包图片 / 合成视频的请求体。

    为什么用 POST + JSON 而不是 GET 查询串：一个图文帖可能有 30 张图，
    每张的签名直链约 300 字符，全塞进 query 会逼近一万字符——
    反代（Nginx 默认请求行上限约 8KB）会直接回 414 Request-URI Too Large。
    """

    images: List[ImageItem]
    filename: str = ""
    per_image_sec: float = slideshow.DEFAULT_PER_IMAGE_SEC


def _check_image_hosts(images: List[ImageItem]) -> None:
    """只放通两个平台的图床域名。

    这两个接口会在**服务端**主动去下载用户给的地址。若不校验，就等于对外提供了
    「任意 URL 打包 / 转码」的开放代理，会被拿去当免费的中转——与 ``/api/download``
    同一道理，所以共用 ``_platform_of_host`` 这份白名单。
    """
    for im in images:
        for u in ([im.url] if im.url else []) + list(im.urls):
            if _platform_of_host(u) is None:
                host = ""
                try:
                    host = httpx.URL(u).host
                except Exception:
                    pass
                raise HTTPException(
                    status_code=400,
                    detail=f"图片地址 {host or '（无法解析）'} 不在白名单内，"
                           "只允许抖音 / 哔哩哔哩 图床的直链。",
                )


@asynccontextmanager
async def lifespan(_: FastAPI):
    await douyin.startup()      # 初始化抖音解析引擎（Evil0ctal API）客户端
    await bili.startup()        # 初始化哔哩哔哩接口客户端
    yield
    await douyin.shutdown()
    await bili.shutdown()


app = FastAPI(title="抖音 / 哔哩哔哩 视频下载站", version="1.3.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
    allow_credentials=True,
)


@app.middleware("http")
async def attach_session(request: Request, call_next):
    """为每个访客绑定一个会话，用于隔离扫码登录得到的凭据。

    会话 id 放在 HttpOnly Cookie 里，前端 JS 读不到，降低被窃取的风险。
    仅在新会话创建时下发 Cookie，已有会话不重复设置。
    """
    sid = request.cookies.get(session_store.SESSION_COOKIE)
    sess = sessions.get(sid)
    fresh = sess is None
    if sess is None:
        sess = sessions.create()
    request.state.session = sess

    response = await call_next(request)
    if fresh:
        response.set_cookie(
            session_store.SESSION_COOKIE,
            sess.sid,
            max_age=session_store.DEFAULT_TTL,
            httponly=True,
            samesite="lax",
            path="/",
        )
    return response


def _platform_of_host(url: str) -> Optional[str]:
    """按域名判断直链属于哪个平台；都不匹配返回 None。"""
    try:
        netloc = httpx.URL(url).host.lower()
    except Exception:
        return None
    for name, keys in _PLATFORM_HOSTS.items():
        if any(k in netloc for k in keys):
            return name
    return None


def _detect_platform(text: str) -> str:
    """按输入判断属于哪个平台。

    判定优先级（越靠前越明确）：
    1. 含 B 站域名（bilibili.com / b23.tv）→ bilibili  （番剧链接走的就是这条）
    2. 含抖音域名（douyin.com / iesdouyin.com）→ douyin
    3. 含裸编号（BV… / av… / ep… / ss… / md…）→ bilibili   用户常直接粘贴裸号
    4. 都识别不出 → 按抖音处理

    第 3 步放在抖音之后，是为了避免抖音分享文案里恰好出现 ``BV…`` 字样时被误判；
    只要文案里有明确的抖音链接，就以抖音为准。

    识别不出时默认抖音，以便沿用原有的报错文案（抖音侧会明确提示
    「这不是抖音链接」，比统一报「无法识别」更有指导性）。

    注意：这里只需判断到「B 站」这一层，**不必**再细分普通视频 / 番剧——
    那是 ``bilibili.parse`` 内部的事，前端提示与后端路由共用同一份实现，
    避免两边规则不一致（历史上就吃过这个亏）。
    """
    link = bili.extract_share_url(text) or text
    if bili.is_bilibili_link(link):
        return "bilibili"
    if douyin.is_douyin_link(link):
        return "douyin"
    if bili.is_bilibili_id(link):
        return "bilibili"
    return "douyin"


# ---------------------------------------------------------------- 解析接口

@app.get("/api/parse")
async def api_parse(
    request: Request,
    url: str = Query(..., description="抖音 / 哔哩哔哩 分享链接或整段分享文字"),
    p: Optional[int] = Query(None, description="哔哩哔哩分P序号（从 1 开始），可选"),
    ep: Optional[int] = Query(None, description="哔哩哔哩番剧单集 ep_id，可选"),
):
    try:
        if _detect_platform(url) == "bilibili":
            # 带上本会话的登录凭据，已登录时自动解锁更高画质
            sess = request.state.session
            # bili.parse 是 B 站统一入口，内部自行区分普通投稿与番剧，
            # 前端不需要（也不应该）自己判断该传哪个参数
            data = await bili.parse(url, page=p, ep=ep, cookie=sess.bili_cookie)
        else:
            data = await douyin.parse_share_url(url)
            data.setdefault("platform", "douyin")

        # 回报服务端能力。图文帖的「下载成视频」依赖 ffmpeg，
        # 前端据此决定要不要显示该按钮——不给用户列点了必然失败的入口。
        # 统一在这里附加，两个平台的结果都带上，前端不必各判一次。
        if isinstance(data, dict):
            data["server_ffmpeg"] = ffmpeg_tool.available()
        return data
    except _PARSE_ERRORS as e:
        return JSONResponse(status_code=422, content={"ok": False, "message": str(e)})
    except httpx.HTTPError:
        return JSONResponse(
            status_code=502,
            content={"ok": False, "message": "无法连接视频平台服务器，请检查网络后重试"},
        )
    except Exception:
        return JSONResponse(
            status_code=500,
            content={"ok": False, "message": "解析出现未知错误，请稍后重试"},
        )


# ---------------------------------------------------------------- B 站扫码登录

@app.get("/api/bili/login")
async def api_bili_login(request: Request):
    """查询当前浏览器的 B 站登录状态。

    会区分凭据来源，前端据此给出不同文案：
    - ``session``：该访客自己扫码登录的（可随时退出）
    - ``env``：服务器在 ``.env`` 里配置的统一账号（访客无法退出）
    """
    sess = request.state.session

    if sess.logged_in:
        return {"ok": True, "logged_in": True, "source": "session",
                "user": sess.public_login(), "can_logout": True}

    if bili.BILI_COOKIE:                       # .env 里的全局兜底账号
        user = await bili.fetch_nav("")
        if user.get("is_login"):
            return {"ok": True, "logged_in": True, "source": "env",
                    "user": user, "can_logout": False}
        return {"ok": True, "logged_in": False, "source": "none", "can_logout": False,
                "notice": "服务器配置的 BILI_COOKIE 已失效，请在 .env 里更新后重启"}

    return {"ok": True, "logged_in": False, "source": "none", "can_logout": False}


@app.post("/api/bili/login/qr")
async def api_bili_qr(request: Request):
    """申请一个登录二维码。二维码归属当前会话，防止被他人串用。"""
    try:
        qr = await bili.qr_generate()
    except bili.ParseError as e:
        return JSONResponse(status_code=502, content={"ok": False, "message": str(e)})
    except httpx.HTTPError:
        return JSONResponse(status_code=502, content={"ok": False, "message": "无法连接哔哩哔哩登录服务器"})

    sessions.begin_qr(request.state.session, qr["qrcode_key"])
    return {"ok": True, "qrcode_key": qr["qrcode_key"], "url": qr["url"],
            "expires_in": session_store.QR_TTL}


@app.get("/api/bili/login/qr/poll")
async def api_bili_qr_poll(request: Request,
                           key: str = Query(..., description="二维码标识")):
    """轮询扫码状态；扫码确认后把凭据写进本会话。"""
    sess = request.state.session

    # 只接受本会话自己申请的那个二维码，避免拿别人的 key 把登录结果写进自己会话
    if not sessions.qr_is_current(sess, key):
        return JSONResponse(
            status_code=409,
            content={"ok": False, "status": "stale",
                     "message": "二维码已过期或不属于当前会话，请刷新后重试"},
        )

    try:
        result = await bili.qr_poll(key)
    except bili.ParseError as e:
        return JSONResponse(status_code=502, content={"ok": False, "message": str(e)})
    except httpx.HTTPError:
        return JSONResponse(status_code=502, content={"ok": False, "message": "查询扫码状态失败"})

    status = result.get("status")
    payload = {"ok": True, "status": status, "message": result.get("message", "")}

    if status == "success":
        user = result.get("user") or {}
        if not user.get("is_login"):
            # 拿到了凭据但校验不通过（极少见），按失败处理并要求重试
            return JSONResponse(
                status_code=502,
                content={"ok": False, "status": "failed",
                         "message": "登录凭据校验失败，请刷新二维码重试"},
            )
        sessions.complete_qr(sess, result["cookie"], user)
        payload.update({"logged_in": True, "user": user,
                        "message": f"登录成功，欢迎 {user.get('name') or ''}"})

    return payload


@app.post("/api/bili/logout")
async def api_bili_logout(request: Request):
    """退出登录：只清当前会话的凭据，不影响其他访客。"""
    sessions.logout_bili(request.state.session)
    return {"ok": True, "logged_in": False,
            "message": "已退出登录" + ("（服务器统一账号仍生效）" if bili.BILI_COOKIE else "")}


@app.get("/api/bili/debug")
async def api_bili_debug(request: Request,
                         url: str = Query(..., description="B 站视频 / 番剧链接"),
                         p: Optional[int] = Query(None, description="分P序号，可选"),
                         ep: Optional[int] = Query(None, description="番剧单集 ep_id，可选")):
    """诊断画质 / 播放权限问题：逐档探测并回报接口真实返回（不含凭据明文）。

    普通视频用于排查「登录了还是只有 720P」——能区分是视频本身上限、凭据未生效，
    还是 MP4 格式拿不到更高档位。
    番剧则额外回报 ``is_preview`` 与「返回时长 vs 官方时长」，用于识别
    「解析成功但下到的只是三分钟试看」这种情况。
    """
    try:
        return {"ok": True, "data": await bili.debug_qualities(
            url, page=p, ep=ep, cookie=request.state.session.bili_cookie)}
    except _PARSE_ERRORS as e:
        return JSONResponse(status_code=422, content={"ok": False, "message": str(e)})
    except httpx.HTTPError:
        return JSONResponse(status_code=502, content={"ok": False, "message": "无法连接哔哩哔哩服务器"})
    except Exception:
        return JSONResponse(status_code=500, content={"ok": False, "message": "诊断出现未知错误"})


@app.get("/api/dash")
async def api_dash(
    request: Request,
    bvid: str = Query("", description="视频 BV 号（普通投稿用）"),
    cid: int = Query(..., description="分P 的 cid"),
    qn: int = Query(0, description="目标清晰度，0 表示最高档"),
    codecid: int = Query(0, description="目标编码，0 表示自动（优先 H.264）"),
    filename: str = Query("video.mp4", description="保存文件名"),
    ep_id: int = Query(0, description="番剧单集 ep_id（番剧用，传了它则忽略 bvid）"),
):
    """下载 DASH 高清档位：服务端合并音视频后返回完整 MP4。

    1080P60 / 4K / HDR 等档位原始形态是「视频流 + 音轨」两条独立文件，
    浏览器无法自己合并，所以由服务端下载后交给 ffmpeg 无损封装（``-c copy``）。

    这里传 bvid/cid/qn 而**不是**直链：DASH 的直链带时效签名，重新解析时可能已过期，
    让服务端在下载那一刻重新申请，天然规避过期问题。

    番剧传的标识是 ``ep_id``（B 站番剧没有 bvid 体系），二选一，不会同时给。
    """
    if not bili.dash_supported():
        raise HTTPException(status_code=503, detail="服务器未安装 ffmpeg，无法合并 DASH 音视频流")
    if not bvid and not ep_id:
        raise HTTPException(status_code=400, detail="缺少视频标识：普通视频传 bvid，番剧传 ep_id")

    try:
        path = await bili.build_dash_file(
            bvid, cid, qn=qn, codecid=codecid, ep_id=ep_id,
            cookie=request.state.session.bili_cookie,
        )
    except _PARSE_ERRORS as e:
        return JSONResponse(status_code=422, content={"ok": False, "message": str(e)})
    except httpx.HTTPError:
        return JSONResponse(status_code=502, content={"ok": False, "message": "无法连接哔哩哔哩服务器"})
    except Exception:
        return JSONResponse(status_code=500, content={"ok": False, "message": "音视频合并失败，请稍后重试"})

    # 合并产物可能上百 MB，响应发完必须删掉，否则磁盘很快被吃满
    return FileResponse(
        path,
        media_type="video/mp4",
        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(filename)}"},
        background=BackgroundTask(bili.cleanup_dir, path.parent),
    )


# ---------------------------------------------------------------- 下载接口

@app.get("/api/download")
async def api_download(
    url: str = Query(..., description="视频直链（来自解析结果）"),
    filename: str = Query("video.mp4", description="保存文件名"),
):
    """代理流式下载（绕过 Referer 防盗链 + 触发浏览器保存）。

    只放通两个平台的 CDN 域名，避免这个接口被当成开放代理滥用。注意 B 站会把
    部分直链下发到**第三方 PCDN 节点**，那些域名刻意不在白名单里——正常路径下
    ``bilibili.prefer_official_cdn`` 已经把官方 CDN 排到第一位，前端取的就是它；
    真走到这里报 400，基本只有两种可能：平台换了 CDN 域名，或该视频没给官方地址。
    """
    platform = _platform_of_host(url)
    if platform is None:
        host = ""
        try:
            host = httpx.URL(url).host
        except Exception:
            pass
        raise HTTPException(
            status_code=400,
            detail=(
                f"直链域名 {host or '（无法解析）'} 不在下载白名单内。"
                "若平台更换了 CDN 域名，请在 .env 的 EXTRA_BILIBILI_HOSTS / "
                "EXTRA_DOUYIN_HOSTS 里追加域名关键字。B 站第三方 PCDN 节点地址"
                "默认不放通（防止本接口被当成开放代理滥用），请重新解析获取官方 CDN 直链。"
            ),
        )

    client = httpx.AsyncClient(
        headers=_DL_HEADERS[platform],
        timeout=httpx.Timeout(30, read=120),
        follow_redirects=True,
    )
    try:
        r = await client.send(client.build_request("GET", url), stream=True)
    except httpx.HTTPError:
        await client.aclose()
        raise HTTPException(status_code=502, detail="视频直链连接失败，直链可能已过期，请重新解析")

    if r.status_code != 200:
        await r.aclose()
        await client.aclose()
        raise HTTPException(status_code=502, detail=f"视频直链返回 {r.status_code}，直链可能已过期，请重新解析")

    async def gen():
        try:
            async for chunk in r.aiter_bytes(1 << 16):
                yield chunk
        finally:
            await r.aclose()
            await client.aclose()

    headers = {"Content-Disposition": f"attachment; filename*=UTF-8''{quote(filename)}"}
    content_length = r.headers.get("content-length")
    if content_length:
        headers["Content-Length"] = content_length

    return StreamingResponse(gen(), media_type=r.headers.get("content-type", "video/mp4"), headers=headers)


# ---------------------------------------------------------------- 图文帖下载

def _attachment(name: str, default_ext: str) -> str:
    """补上扩展名并生成 Content-Disposition 头值。"""
    if not name.lower().endswith(default_ext):
        name += default_ext
    return f"attachment; filename*=UTF-8''{quote(name)}"


@app.post("/api/images/zip")
async def api_images_zip(req: PackRequest):
    """把图文帖的多张图片打包成一个 zip 下载。

    为什么值得单独做：一个图文帖常有 9~30 张图，让用户对着结果卡片逐张点，
    手机上基本没法用。
    """
    if not req.images:
        raise HTTPException(status_code=400, detail="没有可打包的图片")
    _check_image_hosts(req.images)

    try:
        path = await slideshow.pack_zip([im.model_dump() for im in req.images])
    except douyin.ParseError as e:
        return JSONResponse(status_code=422, content={"ok": False, "message": str(e)})
    except Exception:
        return JSONResponse(status_code=500, content={"ok": False, "message": "图片打包失败，请稍后重试"})

    return FileResponse(
        path,
        media_type="application/zip",
        headers={"Content-Disposition": _attachment(req.filename or "images", ".zip")},
        background=BackgroundTask(bili.cleanup_dir, path.parent),
    )


@app.post("/api/slideshow")
async def api_slideshow(req: PackRequest):
    """把图文帖的图片合成为一个幻灯片视频（MP4）。

    为什么放在服务端合成：图文动辄几十张、宽高比还可能混排，用浏览器侧的
    canvas / WebCodecs 做既慢又容易崩；而服务端本来就有 ffmpeg
    （B 站 DASH 合并用的同一套，见 ``ffmpeg_tool``），复用它是成本最低、
    结果最可控的做法。

    产物是**无声**的：抖音不给图文帖下发原始 BGM（``music.play_url`` 为 null），
    前端会明确标注，避免用户以为下载坏了。
    """
    if not req.images:
        raise HTTPException(status_code=400, detail="没有可用于合成的图片")
    if not ffmpeg_tool.available():
        raise HTTPException(status_code=503,
                            detail="服务器未安装 ffmpeg，无法把图片合成为视频")
    _check_image_hosts(req.images)

    try:
        path = await slideshow.build_slideshow(
            [im.model_dump() for im in req.images], req.per_image_sec)
    except douyin.ParseError as e:
        return JSONResponse(status_code=422, content={"ok": False, "message": str(e)})
    except Exception:
        return JSONResponse(status_code=500, content={"ok": False, "message": "图片合成视频失败，请稍后重试"})

    return FileResponse(
        path,
        media_type="video/mp4",
        headers={"Content-Disposition": _attachment(req.filename or "slideshow", ".mp4")},
        background=BackgroundTask(bili.cleanup_dir, path.parent),
    )


@app.get("/api/cover")
async def api_cover(url: str = Query(..., description="封面图直链")):
    platform = _platform_of_host(url)
    if platform is None:
        raise HTTPException(status_code=400, detail="仅支持抖音 / 哔哩哔哩 图片直链")
    try:
        async with httpx.AsyncClient(headers=_DL_HEADERS[platform], timeout=15) as client:
            r = await client.get(url)
    except httpx.HTTPError:
        raise HTTPException(status_code=502, detail="封面拉取失败")
    if r.status_code != 200:
        raise HTTPException(status_code=502, detail="封面拉取失败")
    return Response(content=r.content, media_type=r.headers.get("content-type", "image/jpeg"))


# ---------------------------------------------------------------- 前端静态托管

@app.get("/")
async def index():
    return FileResponse(BASE_DIR / "static" / "index.html")


app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")
