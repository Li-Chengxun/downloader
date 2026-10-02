"""把抖音图文帖（plog）的多张图片合成为一个幻灯片视频。

**为什么必须自己合成**：抖音不为图文帖提供可用的视频。分享页里那个 ``video``
对象是占位符——``play_addr.uri`` 指向 ``images_no_sound_volume_audio_file.mp3``
（一段空音频），``duration`` 为 0，``bit_rate`` 为空；引擎 A 更是直接返回
``media.video = None``。所以"下载成视频"只能由服务端拼：下载图片 → ffmpeg 合成。

**关于声音**：原始 BGM 拿不到（``music.play_url`` 为 null，需登录态才下发），
因此合成产物是无声的。这一点会在前端明确告知用户，避免以为下载坏了。
"""

from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

import httpx

from ffmpeg_tool import cleanup_dir, ffmpeg_path  # noqa: F401  (cleanup_dir 供 main 复用)
from parser import MOBILE_UA, ParseError

#: 单张图片在视频里停留的秒数。
DEFAULT_PER_IMAGE_SEC = 3.0

#: 画布上限。几十张超清原图拼起来很容易做出几百 MB、根本传不动的视频，
#: 所以按最长边限一下；抖音图文本身基本不会超过这个尺寸。
MAX_CANVAS_W = 1080
MAX_CANVAS_H = 1920

#: 尺寸未知时的兜底画布（竖屏 9:16，抖音最常见的形态）。
FALLBACK_CANVAS = (1080, 1920)

_UA_HEADERS = {
    "User-Agent": MOBILE_UA,
    "Referer": "https://www.douyin.com/",
}

#: 每张图的下载重试次数（直链带时效签名，偶发 403 时换备用地址再试）。
_DL_RETRY = 3
_DL_TIMEOUT = 60


class SlideshowError(ParseError):
    """合成幻灯片视频失败（继承 ParseError，复用上层的错误出口）。"""


def _ext_of(im: Dict[str, Any]) -> str:
    """给临时文件挑个真实扩展名。

    刻意不用 ``.bin``：ffmpeg 靠内容嗅探也能认，但带上真实后缀既更稳妥，
    出问题时也能直接在临时目录里点开看。
    """
    ext = str(im.get("ext") or "").lstrip(".").lower()
    if ext not in ("jpg", "jpeg", "png", "webp"):
        url = str(im.get("url") or (im.get("urls") or [""])[0] or "").split("?", 1)[0].lower()
        ext = next((e for e in ("jpeg", "jpg", "png", "webp") if url.endswith("." + e)), "jpg")
    return "jpg" if ext == "jpeg" else ext


def _candidate_urls(im: Dict[str, Any]) -> List[str]:
    """把主地址排到最前，其余作为备用（直链带时效签名，主地址偶发 403）。"""
    urls = [u for u in (im.get("urls") or []) if u]
    primary = im.get("url")
    if primary and primary in urls:
        urls = [primary] + [u for u in urls if u != primary]
    elif primary:
        urls = [primary] + urls
    return urls


def _even(n: int) -> int:
    """H.264 的 yuv420p 要求宽高都是偶数，向下取偶。"""
    return max(2, n - (n % 2))


def _canvas_size(images: Sequence[Dict[str, Any]]) -> tuple:
    """按所有图片的最大宽高算出画布尺寸。

    用「最大宽高」而不是第一张图的尺寸：图文帖里混排横竖图很常见，
    按最大者建画布可以保证每张图都只缩不放（不会被拉伸糊掉），
    多出来的部分用白边补齐（见 ``_vf``）。
    """
    widths = [int(im.get("width") or 0) for im in images]
    heights = [int(im.get("height") or 0) for im in images]
    w, h = max(widths or [0]), max(heights or [0])
    if w <= 0 or h <= 0:
        return FALLBACK_CANVAS
    return _even(min(w, MAX_CANVAS_W)), _even(min(h, MAX_CANVAS_H))


def _vf(w: int, h: int) -> str:
    """统一的画面滤镜：等比缩放 + 居中白边补足 + 统一像素格式。

    ``force_original_aspect_ratio=decrease`` 保证不拉伸；
    ``pad`` 把不同宽高比的图都补成同一画布，这样 concat 解复用器
    才能把它们当成同一条流（否则会因参数不一致直接报错）。
    """
    return (
        f"scale={w}:{h}:force_original_aspect_ratio=decrease,"
        f"pad={w}:{h}:(ow-iw)/2:(oh-ih)/2:color=white,"
        f"setsar=1,format=yuv420p"
    )


def _write_concat_list(paths: List[Path], per_sec: float, dest: Path) -> None:
    """写 concat 解复用器需要的清单文件。

    ⚠️ 结尾**不要**再重复一次最后一个文件。网上常见的写法是
    「末尾把最后一张再写一遍」，那是针对**视频**输入的惯用法
    （视频会丢掉最后一个 duration）。但对**图片**输入实测相反：
    重复一次会实打实多播一张的时长——3 张 × 2s 期望 6.00s，
    重复后变成 7.97s（多出来一整张）。不重复才是 5.97s ≈ 6.00s。
    """
    lines: List[str] = []
    for p in paths:
        lines.append("file '{}'".format(str(p).replace("'", "'\\''")))
        lines.append(f"duration {per_sec}")
    dest.write_text("\n".join(lines) + "\n", encoding="utf-8")


async def _download_image(client: httpx.AsyncClient, urls: List[str], dest: Path) -> bool:
    """把一张图下到 dest；依次尝试候选地址，全部失败返回 False。"""
    for u in urls[:_DL_RETRY]:
        try:
            r = await client.get(u)
        except httpx.HTTPError:
            continue
        if r.status_code == 200 and r.content:
            dest.write_bytes(r.content)
            return True
    return False


async def build_slideshow(images: Sequence[Dict[str, Any]],
                          per_image_sec: float = DEFAULT_PER_IMAGE_SEC) -> Path:
    """下载图片并合成为幻灯片 MP4，返回临时文件路径。

    **调用方负责清理**（``cleanup_dir(path.parent)``）——视频可能几十 MB。

    :param images: 形如 ``[{"urls": [...], "width": 960, "height": 1353}]``；
        只要有 ``urls`` 或 ``url`` 其一即可，``width``/``height`` 用于算画布。
    :param per_image_sec: 每张图停留秒数。
    """
    ff = ffmpeg_path()
    if not ff:
        raise SlideshowError("服务器未安装 ffmpeg，无法把图片合成为视频")

    items = [im for im in images if (im.get("urls") or im.get("url"))]
    if not items:
        raise SlideshowError("没有可用的图片，无法合成视频")

    per_sec = max(0.5, min(float(per_image_sec or DEFAULT_PER_IMAGE_SEC), 15.0))
    w, h = _canvas_size(items)

    tmpdir = Path(tempfile.mkdtemp(prefix="dyslideshow_"))
    try:
        async with httpx.AsyncClient(headers=_UA_HEADERS,
                                     timeout=httpx.Timeout(30, read=_DL_TIMEOUT),
                                     follow_redirects=True) as client:
            saved: List[Path] = []
            for i, im in enumerate(items):
                dest = tmpdir / f"img_{i:03d}.{_ext_of(im)}"
                if await _download_image(client, _candidate_urls(im), dest):
                    saved.append(dest)
                else:
                    # 单张失败不致命：跳过它继续，总比整单失败好
                    print(f"[slideshow] 第 {i + 1} 张图片下载失败，已跳过")

        if not saved:
            raise SlideshowError("所有图片都下载失败，直链可能已过期，请重新解析")

        listfile = tmpdir / "list.txt"
        out = tmpdir / "slideshow.mp4"
        _write_concat_list(saved, per_sec, listfile)

        cmd = [
            ff, "-hide_banner", "-loglevel", "error", "-y",
            "-f", "concat", "-safe", "0", "-i", str(listfile),
            "-vf", _vf(w, h),
            "-r", "30",
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
            "-movflags", "+faststart",          # moov 前置，浏览器可边下边播
            str(out),
        ]
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE)
        except OSError as e:
            raise SlideshowError(f"无法启动 ffmpeg：{e}") from e

        _, err = await proc.communicate()
        if proc.returncode != 0 or not out.exists() or out.stat().st_size == 0:
            detail = (err or b"").decode("utf-8", "ignore").strip().splitlines()
            raise SlideshowError(
                "图片合成视频失败" + (f"：{detail[-1][:200]}" if detail else ""))

        # 源图不再需要，尽早释放（几十张原图也有几 MB）
        for p in saved:
            try:
                p.unlink()
            except OSError:
                pass
        return out
    except Exception:
        cleanup_dir(tmpdir)
        raise


async def pack_zip(images: Sequence[Dict[str, Any]]) -> Path:
    """把所有图片打包成一个 zip，返回临时文件路径（调用方负责清理）。

    为什么值得单独做个 zip：一个图文帖常有 9~30 张图，让用户对着结果卡片
    逐张点下载几十次，体验很差——尤其手机上。

    压缩用 ``ZIP_STORED``（不压缩）：JPEG / WebP 本身已是压缩格式，
    再 deflate 一遍几乎不减小体积，却要白白多花 CPU。
    """
    import zipfile

    items = [im for im in images if (im.get("urls") or im.get("url"))]
    if not items:
        raise SlideshowError("没有可用的图片，无法打包")

    tmpdir = Path(tempfile.mkdtemp(prefix="dyimgzip_"))
    try:
        async with httpx.AsyncClient(headers=_UA_HEADERS,
                                     timeout=httpx.Timeout(30, read=_DL_TIMEOUT),
                                     follow_redirects=True) as client:
            saved: List[Path] = []
            for i, im in enumerate(items):
                # 文件名补零，解压后按序号自然排序，与帖内顺序一致
                dest = tmpdir / f"{i + 1:02d}.{_ext_of(im)}"
                if await _download_image(client, _candidate_urls(im), dest):
                    saved.append(dest)

        if not saved:
            raise SlideshowError("所有图片都下载失败，直链可能已过期，请重新解析")

        out = tmpdir / "images.zip"
        with zipfile.ZipFile(out, "w", zipfile.ZIP_STORED) as z:
            for p in saved:
                z.write(p, arcname=p.name)
        for p in saved:
            try:
                p.unlink()
            except OSError:
                pass
        return out
    except Exception:
        cleanup_dir(tmpdir)
        raise
