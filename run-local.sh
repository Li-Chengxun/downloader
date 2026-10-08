#!/usr/bin/env bash
# ============================================================
#  本地开发启动脚本
#
#  用法：bash run-local.sh
#        PORT=9000 bash run-local.sh    # 换端口
#
#  这个脚本只做三件事，都是本地开发时容易卡住的地方：
#    1. 自动挑一个装了依赖的 Python（优先用 `.venv`，其次托管环境，最后系统 Python）
#    2. 自动挑一个**真的能执行**的 ffmpeg —— Windows 上 PATH 里那个可能是坏的 shim
#    3. 起 uvicorn
#
#  为什么不用 `uvicorn main:app` 直接跑：见下面 ffmpeg 那段的注释。
# ============================================================
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BACKEND_DIR="$PROJECT_DIR/backend"
PORT="${PORT:-8000}"
HOST="${HOST:-127.0.0.1}"

# 把 Git Bash 的 /c/xxx 转成 Python 能认的 C:/xxx（Windows 下子进程需要）
to_native() { echo "$1" | sed -E 's|^/([a-zA-Z])/|\1:/|'; }

# ------------------------------------------------------------ 1. 挑 Python
# 依赖版本在 backend/requirements.txt 里锁死了，用别的解释器可能缺包。
pick_python() {
  local candidates=(
    "$PROJECT_DIR/.venv/Scripts/python.exe"      # 项目内虚拟环境（若有）
    "$PROJECT_DIR/.venv/bin/python"              # macOS / Linux
    "$HOME/.workbuddy/binaries/python/envs/default/Scripts/python.exe"
    "$(command -v python3 || true)"
    "$(command -v python || true)"
  )
  for p in "${candidates[@]}"; do
    [ -n "$p" ] && [ -x "$p" ] || continue
    if "$p" -c "import fastapi, uvicorn, httpx" >/dev/null 2>&1; then
      echo "$p"; return 0
    fi
  done
  return 1
}

if ! PY="$(pick_python)"; then
  echo "✗ 找不到装了依赖的 Python。" >&2
  echo "" >&2
  echo "  请先装依赖（任选其一）：" >&2
  echo "    # 方式 A：项目内虚拟环境" >&2
  echo "    python -m venv .venv" >&2
  echo "    .venv/Scripts/pip install -r backend/requirements.txt   # Windows" >&2
  echo "    .venv/bin/pip    install -r backend/requirements.txt   # macOS/Linux" >&2
  echo "" >&2
  echo "    # 方式 B：直接用现有解释器" >&2
  echo "    python -m pip install -r backend/requirements.txt" >&2
  exit 1
fi
echo "· Python : $PY"

# ------------------------------------------------------------ 2. 挑 ffmpeg
# ⚠️ 两个坑叠在一起，这里必须同时避开：
#
#   坑 1：只检查「文件存在」不够。Windows 上用 WinGet 装的 ffmpeg，PATH 里那个
#          ffmpeg.exe 可能是 **0 字节的 reparse point（shim）**，一执行就报
#          [WinError 193] 不是有效的 Win32 应用程序。所以要真的跑一次 -version。
#
#   坑 2（更隐蔽）：**必须用「应用实际使用的那个解释器」去验证**。
#          实测同一个 shim：Git Bash 直接执行**成功**（bash 会跟随 reparse point），
#          但 Python 的 subprocess 执行**必失败**。若用 bash 的 `-version` 做判断，
#          就会把坏路径当成可用的选出来 —— 启动时一切正常，直到用户点了 DASH
#          档位才炸。所以下面统一调 $PY 来验证。
ffmpeg_ok() {
  [ -n "${1:-}" ] || return 1
  "$PY" - "$1" <<'PYEOF' >/dev/null 2>&1
import os, subprocess, sys
p = sys.argv[1]
sys.exit(0 if os.path.exists(p) and subprocess.run(
    [p, "-version"], stdout=subprocess.DEVNULL,
    stderr=subprocess.DEVNULL, timeout=15).returncode == 0 else 1)
PYEOF
}

FFMPEG=""
# ① 先用 .env 里显式配的（优先级最高，方便覆盖坏路径）
if [ -f "$PROJECT_DIR/.env" ]; then
  ENV_FF="$(grep -E '^FFMPEG_BIN=' "$PROJECT_DIR/.env" 2>/dev/null | head -1 | cut -d= -f2- | tr -d '"' | tr -d "'" | xargs || true)"
  ffmpeg_ok "$(to_native "$ENV_FF")" && FFMPEG="$ENV_FF"
fi
# ② 再试 PATH 里的（先用 Python 的 which 拿到 Windows 视角的真实路径）
if [ -z "$FFMPEG" ]; then
  P="$("$PY" -c "import shutil,sys; print(shutil.which('ffmpeg') or '')" 2>/dev/null || true)"
  ffmpeg_ok "$(to_native "$P")" && FFMPEG="$P"
fi
# ③ 最后找 WinGet 安装目录里的真身（shim 背后的实际文件）
if [ -z "$FFMPEG" ]; then
  REAL="$(find "$HOME/AppData/Local/Microsoft/WinGet/Packages" \
            -path "*FFmpeg*/bin/ffmpeg.exe" -type f 2>/dev/null | head -1 || true)"
  ffmpeg_ok "$(to_native "$REAL")" && FFMPEG="$REAL"
fi

if [ -n "$FFMPEG" ]; then
  export FFMPEG_BIN="$(to_native "$FFMPEG")"
  echo "· ffmpeg : $FFMPEG_BIN"
else
  echo "· ffmpeg : 未找到可用版本 —— DASH 高清档位（1080P60/4K/HDR）不会出现在列表里"
  echo "           MP4 档位不受影响，功能正常。要启用请装 ffmpeg 并在 .env 里设 FFMPEG_BIN"
  echo "           （注意：文件存在 ≠ 能执行。若 PATH 里那个是坏的 shim，直接指向真实路径）"
fi

# ------------------------------------------------------------ 3. 起服务
if [ ! -f "$PROJECT_DIR/.env" ]; then
  echo "· .env   : 不存在，使用 .env.example 的默认值（抖音走公共演示实例，B站不登录）"
  echo "           需要自定义请执行：cp .env.example .env"
fi

echo ""
echo "  服务地址 → http://${HOST}:${PORT}"
echo "  停止服务 → Ctrl+C"
echo ""

cd "$BACKEND_DIR"
if [ -f "$PROJECT_DIR/.env" ]; then
  exec "$PY" -m uvicorn main:app --host "$HOST" --port "$PORT" --env-file "$PROJECT_DIR/.env"
fi
exec "$PY" -m uvicorn main:app --host "$HOST" --port "$PORT"
