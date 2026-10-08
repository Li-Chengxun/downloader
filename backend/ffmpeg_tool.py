"""ffmpeg 可执行文件的定位与探测（抖音 / 哔哩哔哩 共用）。

为什么单独成一个模块：两个平台各自都需要 ffmpeg，但用途不同——
B 站 DASH 高清档位需要把「音视频分离流」合并成一个 MP4，
抖音图文帖需要把「多张图片」合成一个幻灯片视频。
而「找到真正能跑的 ffmpeg」这段逻辑踩过一串实测坑（见 ``_ffmpeg_runs``），
放在任一平台的解析模块里，都会让另一平台被迫去 import 对方，语义上说不通。
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import List, Optional

#: ffmpeg 可执行文件路径。留空则用 PATH 里的 ffmpeg（Docker 镜像里是 apt 装的）。
FFMPEG_BIN = os.environ.get("FFMPEG_BIN", "").strip()

#: 探测结果缓存：``(路径或 None, 探测时刻)``，见 ``ffmpeg_path``。
_ffmpeg_cache: Optional[tuple] = None

#: 探测结果缓存时长（秒）。不能永久缓存——用户可能在服务运行期间才装上 ffmpeg。
_FFMPEG_TTL = 600.0


def _ffmpeg_runs(path: str) -> bool:
    """真的执行一次 ``ffmpeg -version``，确认这个路径**跑得起来**。

    只判断「文件是否存在」是不够的。实测（Windows + WinGet 安装的 ffmpeg）：
    ``WinGet\\Links`` 目录下会留下一个 **0 字节的 reparse point**，
    ``shutil.which()`` 能找到它、``Path.exists()`` 也是 True，
    但真正执行会抛 ``OSError [WinError 193] 不是有效的 Win32 应用程序``。

    这类「占着名字却跑不起来」的情况若漏过检测，用户就会看到 DASH 档位、
    点下去、等上十几秒，最后拿到一个报错——正是检测本身要避免的。
    """
    try:
        r = subprocess.run(
            [path, "-version"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=15,
        )
        return r.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def _winget_ffmpeg_paths() -> List[str]:
    """Find installed Windows binaries when the PATH entry is a broken WinGet shim."""
    if sys.platform != "win32":
        return []
    local = os.environ.get("LOCALAPPDATA")
    roots = {Path.home() / "AppData" / "Local"}
    if local:
        roots.add(Path(local))
    return [str(binary) for root in sorted(roots)
            for package in sorted((root / "Microsoft" / "WinGet" / "Packages").glob("*FFmpeg*"))
            for binary in sorted(package.glob("*/bin/ffmpeg.exe"))]


def ffmpeg_path() -> Optional[str]:
    """返回**确认可执行**的 ffmpeg 路径；没有则返回 ``None``。

    没装时不应该把「依赖 ffmpeg 的能力」展示给用户（否则点了必然失败），
    所以前端的档位/按钮展示要以此为准。

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
    if path is None:
        for candidate in _winget_ffmpeg_paths():
            if candidate in candidates:
                continue
            candidates.append(candidate)
            if Path(candidate).exists() and _ffmpeg_runs(candidate):
                path = candidate
                break
    _ffmpeg_cache = (path, now)
    if path is None and candidates:
        # 找到了名字却跑不起来，这是最容易让人困惑的情形，值得留下日志线索
        print(f"[ffmpeg] 检测到候选 {candidates} 但均无法执行，"
              f"依赖 ffmpeg 的功能（B 站 DASH 档位 / 图文合成视频）将不展示。"
              f"可用 FFMPEG_BIN 指定正确的可执行文件路径。",
              file=sys.stderr)
    return path


def available() -> bool:
    """当前环境能否提供需要 ffmpeg 的能力。"""
    return ffmpeg_path() is not None


def cleanup_dir(d: Path) -> None:
    """删除临时目录（供下载接口在响应结束后调用，失败也不抛）。"""
    try:
        shutil.rmtree(d, ignore_errors=True)
    except Exception:
        pass
