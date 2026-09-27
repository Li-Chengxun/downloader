#!/usr/bin/env bash
#
# 抖音 / 哔哩哔哩 视频下载站 — 增量更新脚本（Docker 方式）
#
# 用法（在服务器上的项目根目录执行）：
#   bash deploy/update.sh              # 更新（自动沿用当前的 Nginx 配置）
#   bash deploy/update.sh --clean      # 忽略构建缓存，强制全量重建
#
# 与 deploy.sh 的区别：deploy.sh 是「首次部署」（会生成 .env、放通防火墙、
# 写 Nginx 配置）；本脚本是「已有部署的更新」，只做重建镜像 + 重启容器，
# 不动 .env / 防火墙 / 证书，尽量把变更面缩到最小。
#
# 安全保证：
#   - **不覆盖 .env**（你的 Cookie / 端口等配置原样保留）
#   - 构建失败会立即退出，**旧容器继续运行**，服务不中断
#   - 更新前自动给当前镜像打备份 tag，出问题可一条命令回滚（见脚本末尾提示）

set -euo pipefail

cd "$(dirname "$0")/.."
ROOT="$(pwd)"

BLUE='\033[0;34m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; RED='\033[0;31m'; NC='\033[0m'
info()  { printf "${BLUE}[信息]${NC} %s\n" "$1"; }
ok()    { printf "${GREEN}[完成]${NC} %s\n" "$1"; }
warn()  { printf "${YELLOW}[注意]${NC} %s\n" "$1"; }
die()   { printf "${RED}[错误]${NC} %s\n" "$1" >&2; exit 1; }

CLEAN=0
for a in "$@"; do
  case "$a" in
    --clean) CLEAN=1 ;;
    -h|--help)
      # 打印文件头的注释块（到第一个非注释行为止），不用写死行号
      awk 'NR>1 && /^#/ {sub(/^# ?/, ""); print; next} NR>1 {exit}' "$0"
      exit 0 ;;
    *) die "未知参数：$a（可用：--clean / --help）" ;;
  esac
done

printf "${BLUE}抖音 / 哔哩哔哩 视频下载站 · 更新${NC}\n\n"

# ── 1. 前置检查 ──
[[ -f backend/main.py && -f docker-compose.yml ]] || die "请在项目根目录执行（当前：$ROOT）"
command -v docker >/dev/null 2>&1 || die "未安装 Docker"
docker info >/dev/null 2>&1 || die "无法访问 Docker 守护进程，请用 sudo 运行"

if [[ ! -f .env ]]; then
  [[ -f .env.example ]] || die "未找到 .env，也没有 .env.example 可参考。首次部署请用 bash deploy/deploy.sh"
  warn "未找到 .env，将从 .env.example 生成（首次部署请改用 deploy.sh）"
  cp .env.example .env
fi
ok "环境检查通过：$ROOT"

# ── 2. 沿用当前的 Nginx 配置 ──
# 首次部署若用了 --proxy，更新时也必须带 profile，否则 nginx 容器不会被纳入编排。
USE_PROXY=0
if docker compose ps --services 2>/dev/null | grep -qx nginx; then
  USE_PROXY=1
  ok "检测到正在使用 Nginx 反向代理，将沿用（--profile proxy）"
else
  info "未检测到 Nginx 容器，按直连模式更新"
fi

# ── 3. 备份当前镜像 ──
CUR_ID="$(docker images -q douyin-downloader:latest 2>/dev/null | head -1 || true)"
BACKUP_TAG=""
if [[ -n "$CUR_ID" ]]; then
  BACKUP_TAG="douyin-downloader:backup-$(date +%Y%m%d-%H%M%S)"
  docker tag "$CUR_ID" "$BACKUP_TAG" && ok "已备份当前镜像 -> $BACKUP_TAG"
else
  info "未发现既有镜像（首次构建）"
fi

# ── 4. 重新构建镜像 ──
# 这一步是必须的：本次更新改动了 Dockerfile（新增 ffmpeg），
# 只重启容器不会把 ffmpeg 装进去，DASH 高清档位会不可用。
if [[ $CLEAN -eq 1 ]]; then
  warn "已指定 --clean：忽略缓存全量重建，会比较慢"
  BUILD_ARGS=(--no-cache)
else
  BUILD_ARGS=(--pull)
fi

echo
info "开始构建镜像（新增了 ffmpeg，首次会多下载几十 MB，请耐心等待）..."
echo "  提示：若报 'load metadata ... i/o timeout'，说明连不上 Docker Hub，"
echo "        先执行 sudo bash deploy/fix-docker-mirror.sh 配置加速源后重试。"
echo

if ! docker compose build "${BUILD_ARGS[@]}"; then
  echo
  die "构建失败。**旧版本服务未受影响，仍在正常运行。**
  排查方向（按报错关键字对号入座）：
    1) 'load metadata ... i/o timeout' / 'DeadlineExceeded'
       -> 连不上 Docker Hub：sudo bash deploy/fix-docker-mirror.sh
    2) 'apt-get' / 'ffmpeg' 相关失败
       -> bash deploy/update.sh --clean 重试；
          或改 .env 里的 APT_MIRROR 指向其它 Debian 源
    3) 'ResolutionImpossible' / 'Cannot install' / 'conflicting dependencies'
       -> pip 依赖解析失败（跟 ffmpeg 无关，是 Python 包的事）：
          · 先在 .env 里加一行备用源再重试：
              PIP_EXTRA_INDEX_URL=https://mirrors.aliyun.com/pypi/simple
          · 想看清到底哪个包缺了，单独跑一次解析（几秒钟，不重建镜像）：
              docker run --rm -v \"\$PWD/backend:/w:ro\" python:3.12-slim \\
                pip install --dry-run -i \"\$(grep -E '^PIP_INDEX_URL=' .env | cut -d= -f2)\" -r /w/requirements.txt
            （若 .env 里改过 BASE_IMAGE，把 python:3.12-slim 换成那个镜像）
    4) 'no space left on device'
       -> df -h 检查，docker image prune -f 清理旧镜像"
fi
ok "镜像构建完成"

# ── 5. 重启容器 ──
echo
info "重启服务..."
if [[ $USE_PROXY -eq 1 ]]; then
  docker compose --profile proxy up -d
else
  docker compose up -d
fi
ok "容器已更新"

# ── 6. 关键项验证：容器内 ffmpeg 是否可用 ──
# 这是本次更新的核心目标（解锁 1080P60 / 4K / HDR 等 DASH 档位）。
echo
info "验证容器内 ffmpeg..."
if ! docker compose ps --services --status running 2>/dev/null | grep -qx web; then
  warn "web 容器尚未运行，跳过 ffmpeg 检查"
  warn "请用 bash deploy/deploy.sh --logs 确认启动是否正常"
elif docker compose exec -T web ffmpeg -version >/dev/null 2>&1; then
  FF_VER="$(docker compose exec -T web ffmpeg -version 2>/dev/null | head -1)"
  ok "ffmpeg 可用：$FF_VER"
  ok "DASH 高清档位（1080P60 / 4K / HDR）已解锁"
else
  warn "容器内未找到 ffmpeg —— DASH 高清档位将不可用（其余功能正常）"
  warn "多半是镜像构建时跳过了 apt 层，执行：bash deploy/update.sh --clean"
fi

# ── 7. 健康检查 ──
echo
info "等待服务就绪..."
# APP_PORT 只存在于 .env 文件里，脚本环境里没有该变量，必须从文件读，
# 否则用户改过端口就会探测错端口、误报「健康检查未通过」。
READY_PORT="$(grep -E '^APP_PORT=' .env 2>/dev/null | head -1 | cut -d= -f2 | tr -d '[:space:]')"
READY_PORT="${READY_PORT:-8000}"
[[ $USE_PROXY -eq 1 ]] && READY_PORT=80

READY=0
for i in $(seq 1 20); do
  if curl -fsS --max-time 3 "http://127.0.0.1:${READY_PORT}/" >/dev/null 2>&1; then
    READY=1; break
  fi
  sleep 1
done

echo
if [[ $READY -eq 1 ]]; then
  ok "服务已就绪（端口 ${READY_PORT}）"
else
  warn "健康检查未通过，请查看日志：bash deploy/deploy.sh --logs"
fi

# ── 8. 结果与回滚提示 ──
echo
docker compose ps
echo
printf "${BLUE}更新完成。${NC}\n"
echo "  查看状态：bash deploy/deploy.sh --status"
echo "  查看日志：bash deploy/deploy.sh --logs"
echo
echo "  验证新功能：打开网页 → 粘贴一个 B 站链接 → 应能看到带「音视频合并」"
echo "             角标的高清档位；点结果卡片里的「画质诊断」可确认上限。"
echo
if [[ -n "$BACKUP_TAG" ]]; then
  echo "  如需回滚到更新前的版本："
  echo "    docker tag ${BACKUP_TAG} douyin-downloader:latest"
  if [[ $USE_PROXY -eq 1 ]]; then
    echo "    docker compose --profile proxy up -d --force-recreate web"
  else
    echo "    docker compose up -d --force-recreate web"
  fi
  echo
  echo "  确认新版正常后，可清理旧镜像释放空间（ffmpeg 会让镜像变大）："
  echo "    docker image prune -f"
fi
