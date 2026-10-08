"""抖音图文帖（plog）的下载产物生成：图片打包 zip（含文案 Word）/ ffmpeg 合成幻灯片视频。

**为什么合成视频要自己做**：抖音不为图文帖提供可用的视频。分享页里那个 ``video``
对象是占位符——``play_addr.uri`` 指向 ``images_no_sound_volume_audio_file.mp3``
（一段空音频），``duration`` 为 0，``bit_rate`` 为空；引擎 A 更是直接返回
``media.video = None``。所以"下载成视频"只能由服务端拼：下载图片 → ffmpeg 合成。

**为什么 zip 里要放 Word**：图文帖的价值常常在文案上（长图文动辄几千字），
只存图片等于把内容丢掉一半。纯文本 docx 便于二次编辑与分享。

**关于声音**：原始 BGM 拿不到（``music.play_url`` 为 null，需登录态才下发），
因此合成产物是无声的。这一点会在前端明确告知用户，避免以为下载坏了。
"""

from __future__ import annotations

import asyncio
import io
import re
import tempfile
import zipfile
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

import httpx
import media_http
import resource_limits as limits

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
            async with media_http.media_stream(client, u) as r:
                if r.status_code == 200:
                    size = await media_http.save_media(r, dest, limits.MAX_IMAGE_BYTES)
                    if size:
                        return True
        except (media_http.UnsafeURL, media_http.MediaTooLarge) as exc:
            raise SlideshowError(str(exc)) from exc
        except httpx.HTTPError:
            continue
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
        async with media_http.media_client(headers=_UA_HEADERS,
                                           timeout=httpx.Timeout(30, read=_DL_TIMEOUT)) as client:
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

        try:
            _, err = await limits.communicate_process(proc)
        except asyncio.TimeoutError as exc:
            raise SlideshowError("图片合成超时，请减少图片数量后重试") from exc
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
    except BaseException:
        cleanup_dir(tmpdir)
        raise


# ---------------------------------------------------------------- Word 文案

#: XML 里非法、但抖音文案里可能混入的控制字符（\t \n \r 之外）。
#: 不剔除的话 Word 打开会直接报「文件已损坏，无法打开」。
_XML_BAD_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")

#: docx 其实就是个 zip，固定这几部分就够了（纯文本段落，不含图片/表格/样式表）
_CONTENT_TYPES = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
    '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
    '<Default Extension="xml" ContentType="application/xml"/>'
    '<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
    "</Types>"
)

_ROOT_RELS = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
    '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>'
    "</Relationships>"
)

_NS_W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"


def _xml_esc(s: str) -> str:
    """转义 XML 特殊字符并剔除非法控制字符。"""
    s = _XML_BAD_CHARS.sub("", s or "")
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _docx_para(text: str, *, bold: bool = False, size: int = 0,
               color: str = "", after: int = 120) -> str:
    """拼一个段落。``size`` 单位是**半磅**（Word 的惯例），``after`` 是段后间距（1/20 磅）。

    刻意用直接格式（``<w:rPr>``）而不引用样式：这样就不必再塞 ``styles.xml``，
    少一个部件就少一处可能出错的地方。
    """
    rpr = ""
    if bold:
        rpr += "<w:b/>"
    if color:
        rpr += f'<w:color w:val="{color}"/>'
    if size:
        rpr += f'<w:sz w:val="{size}"/><w:szCs w:val="{size}"/>'
    rpr = f"<w:rPr>{rpr}</w:rPr>" if rpr else ""
    run = f'<w:r>{rpr}<w:t xml:space="preserve">{_xml_esc(text)}</w:t></w:r>'
    return (f'<w:p><w:pPr><w:spacing w:after="{after}"/></w:pPr>{run}</w:p>')


def build_docx(title: str, author: str = "", desc: str = "",
               source: str = "", stats_text: str = "") -> bytes:
    """把图文帖的文案生成一份 .docx 的字节内容。

    **为什么手写 OOXML 而不是用 python-docx**：本项目 ``requirements.txt`` 是用
    精确版本锁定的，历史上还吃过依赖解析爆炸的亏（见 README 常见问题 15）。
    为一个「导出文案」的小功能新增依赖、还得跟着重建镜像，不划算。
    docx 本质就是 zip + 几个固定 XML，而这里只用到最基础的段落与文字格式，手写完全可控。

    排版：标题加粗放大 → 作者/来源/字数一行小字 → 正文按行分段 → 结尾落款。
    """
    lines = [ln.strip() for ln in (desc or "").splitlines()]
    body = [ln for ln in lines if ln]
    if not body:
        body = ["（本作品没有文字内容）"]

    parts: List[str] = []
    if title:
        parts.append(_docx_para(title, bold=True, size=32, after=200))

    meta = [x for x in (f"作者：{author}" if author else "",
                        f"来源：{source}" if source else "",
                        stats_text,
                        f"全文约 {len(desc or '')} 字") if x]
    for line in meta:
        parts.append(_docx_para(line, size=18, color="808080", after=60))
    if meta:
        parts.append(_docx_para("", after=120))

    for line in body:
        parts.append(_docx_para(line, size=22, after=140))

    parts.append(_docx_para("—— 由「抖音 / 哔哩哔哩 视频下载站」导出",
                            size=16, color="A0A0A0", after=0))

    document = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        f'<w:document xmlns:w="{_NS_W}"><w:body>'
        + "".join(parts)
        # A4 页面 + 四边 1 英寸页边距（单位：1/20 磅）
        + '<w:sectPr><w:pgSz w:w="11906" w:h="16838"/>'
          '<w:pgMar w:top="1440" w:right="1440" w:bottom="1440" w:left="1440"/>'
          "</w:sectPr>"
        + "</w:body></w:document>"
    )

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        # [Content_Types].xml 必须是包里的第一个条目，部分解析器对此敏感
        z.writestr("[Content_Types].xml", _CONTENT_TYPES)
        z.writestr("_rels/.rels", _ROOT_RELS)
        z.writestr("word/document.xml", document)
    return buf.getvalue()


#: 文案文件在 zip 里的名字。用中文便于用户一眼认出；
#: zipfile 会带 UTF-8 标记位，Windows 10+ / macOS / 7-Zip 都能正确显示。
DOCX_NAME = "文案.docx"


def build_docx_bytes(meta: Optional[Dict[str, Any]], image_count: int = 0) -> bytes:
    """按解析结果里的元信息生成文案 docx。"""
    m = meta or {}
    stats = m.get("stats_text") or ""
    if image_count:
        stats = (stats + "　" if stats else "") + f"共 {image_count} 张图片"
    return build_docx(
        title=(m.get("title") or "").strip(),
        author=(m.get("author") or "").strip(),
        desc=m.get("desc") or "",
        source=m.get("source_url") or "",
        stats_text=stats,
    )


async def pack_zip(images: Sequence[Dict[str, Any]],
                   meta: Optional[Dict[str, Any]] = None) -> Path:
    """把所有图片 + 一份文案 Word 打包成一个 zip，返回临时文件路径（调用方负责清理）。

    为什么值得单独做个 zip：一个图文帖常有 9~30 张图，让用户对着结果卡片
    逐张点下载几十次，体验很差——尤其手机上。
    为什么里面要塞 Word：图文帖的价值常常在**文案**上（长图文尤其），
    只存图片等于把内容丢掉一半；纯文本的 docx 比 txt 更便于二次编辑和分享。

    压缩用 ``ZIP_STORED``（不压缩）：JPEG / WebP 本身已是压缩格式，
    再 deflate 一遍几乎不减小体积，却要白白多花 CPU。
    """
    items = [im for im in images if (im.get("urls") or im.get("url"))]
    if not items:
        raise SlideshowError("没有可用的图片，无法打包")

    tmpdir = Path(tempfile.mkdtemp(prefix="dyimgzip_"))
    try:
        async with media_http.media_client(headers=_UA_HEADERS,
                                           timeout=httpx.Timeout(30, read=_DL_TIMEOUT)) as client:
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
            # 文案单独放一份 Word。文案为空时也写，里面会说明"没有文字内容"，
            # 免得用户以为漏了文件。
            z.writestr(DOCX_NAME, build_docx_bytes(meta, image_count=len(saved)))
        for p in saved:
            try:
                p.unlink()
            except OSError:
                pass
        return out
    except BaseException:
        cleanup_dir(tmpdir)
        raise
