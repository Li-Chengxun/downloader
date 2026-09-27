#!/usr/bin/env bash
#
# 抖音视频下载站 — 一键部署脚本（Docker 方式）
#
# 用法：
#   bash deploy/deploy.sh              # 部署（不含 Nginx）
#   bash deploy/deploy.sh --proxy      # 部署并启用 Nginx 反向代理
#   bash deploy/deploy.sh --status     # 查看状态
#   bash deploy/deploy.sh --logs       # 跟踪日志
#
# 只在 Ubuntu / Debian / CentOS 等常见 Linux 上验证过。

set -euo pipefail

cd "$(dirname "$0")/.."
ROOT="$(pwd)"

BLUE='\033[0;34m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; RED='\033[0;31m'; NC='\033[0m'
info()  { printf "${BLUE}[信息]${NC} %s\n" "$1"; }
ok()    { printf "${GREEN}[完成]${NC} %s\n" "$1"; }
warn()  { printf "${YELLOW}[注意]${NC} %s\n" "$1"; }
die()   { printf "${RED}[错误]${NC} %s\n" "$1" >&2; exit 1; }

# ---------------------------------------------------------------- 子命令

if [[ "${1:-}" == "--status" ]]; then
  docker compose ps
  echo
  info "健康检查："
  # 依次探测 Nginx(80) 与直连(APP_PORT)，哪个通用哪个
  READY=0
  for p in 80 "$(grep -E '^APP_PORT=' .env 2>/dev/null | head -1 | cut -d= -f2 | xargs)" 8000; do
    [[ -z "$p" ]] && continue
    if curl -fsS --max-time 4 "http://127.0.0.1:${p}/" >/dev/null 2>&1; then
      ok "端口 ${p} 响应正常"
      READY=1
      break
    fi
  done
  [[ $READY -eq 0 ]] && warn "未探测到响应，试试 --logs 看日志"
  exit 0
fi

if [[ "${1:-}" == "--logs" ]]; then
  docker compose logs -f --tail=100
  exit 0
fi

USE_PROXY=0
[[ "${1:-}" == "--proxy" ]] && USE_PROXY=1

# ---------------------------------------------------------------- 环境检查

info "检查 Docker..."
if ! command -v docker >/dev/null 2>&1; then
  die "未安装 Docker。请先执行：
    curl -fsSL https://get.docker.com | sh
    sudo systemctl enable --now docker
  然后重新运行本脚本。"
fi

if ! docker compose version >/dev/null 2>&1; then
  die "未找到 docker compose 插件（v2）。请升级 Docker 到较新版本。"
fi
ok "Docker 就绪：$(docker --version)"

if ! docker info >/dev/null 2>&1; then
  die "当前用户无权访问 Docker，请用 sudo 运行，或把用户加入 docker 组：
    sudo usermod -aG docker \$USER && newgrp docker"
fi

# 自动放通防火墙（如果检测到 ufw / firewalld）
if command -v ufw >/dev/null 2>&1 && ufw status 2>/dev/null | grep -q "Status: active"; then
  if [[ $USE_PROXY -eq 1 ]]; then
    warn "检测到 ufw 已启用，将放通 80/443 端口"
    ufw allow 80/tcp >/dev/null 2>&1 || true
    ufw allow 443/tcp >/dev/null 2>&1 || true
  fi
fi

# ---------------------------------------------------------------- 配置

if [[ ! -f .env ]]; then
  cp .env.example .env
  warn "已从 .env.example 生成 .env"
  warn "当前使用公共解析实例（有限流）。生产建议自托管解析引擎，见 README「上线部署」第 3 步"
fi

# 启用 Nginx 时，应用端口只监听本机，避免绕过反代直接访问 8000
if [[ $USE_PROXY -eq 1 ]]; then
  if grep -q '^APP_BIND=' .env; then
    sed -i 's|^APP_BIND=.*|APP_BIND=127.0.0.1|' .env
  else
    echo 'APP_BIND=127.0.0.1' >> .env
  fi
  info "已设置 APP_BIND=127.0.0.1（8000 端口仅本机可访问，对外统一走 Nginx）"
  # 等待就绪时改从 nginx 的 80 端口探测
  READY_PORT=80
else
  READY_PORT="${APP_PORT:-8000}"
fi

mkdir -p deploy/certs

# ---------------------------------------------------------------- 部署

info "构建镜像（首次约 1~2 分钟）..."
if ! docker compose build --pull; then
  echo
  die "镜像构建失败。如果是 'load metadata ... i/o timeout' / 'DeadlineExceeded'，
  说明服务器连不上 Docker Hub（国内服务器常见），执行下面的命令配置加速源后重试：

    sudo bash deploy/fix-docker-mirror.sh
    sudo bash deploy/deploy.sh --proxy"
fi

if [[ $USE_PROXY -eq 1 ]]; then
  info "启动服务（含 Nginx 反向代理）..."
  docker compose --profile proxy up -d
else
  info "启动服务..."
  docker compose up -d
fi

# ---------------------------------------------------------------- 等待就绪

# 从 .env 读取 APP_PORT（脚本环境里没有该变量）
if [[ -z "${APP_PORT:-}" && -f .env ]]; then
  APP_PORT="$(grep -E '^APP_PORT=' .env | head -1 | cut -d= -f2 | tr -d '"' | tr -d "'" | xargs || true)"
fi
info "等待应用就绪（探测端口 ${READY_PORT}）..."
for i in $(seq 1 30); do
  if curl -fsS --max-time 3 "http://127.0.0.1:${READY_PORT}/" >/dev/null 2>&1; then
    ok "应用已就绪"
    break
  fi
  [[ $i -eq 30 ]] && warn "等待超时，请用 bash deploy/deploy.sh --logs 检查日志"
  sleep 2
done

# ---------------------------------------------------------------- 结果

IP="$(hostname -I 2>/dev/null | awk '{print $1}')"
echo
ok "部署完成"
echo "─────────────────────────────────────────────"
if [[ $USE_PROXY -eq 1 ]]; then
  # 卷名随目录名变化，动态探测，避免硬编码失效
  WEBROOT_VOL="$(docker volume ls --format '{{.Name}}' 2>/dev/null | grep -E 'certbot-webroot$' | head -1)"
  echo "  访问地址：http://${IP:-服务器IP}/"
  echo
  echo "  下一步：绑定域名并开启 HTTPS"
  echo "    1) 把域名 A 记录解析到 ${IP:-服务器IP}"
  echo "    2) 改 Nginx 配置里的域名：vim deploy/nginx.conf（把 server_name _ 换成你的域名）"
  echo "    3) 申请证书（把 your-domain.com 和邮箱换成你的）："
  echo "       docker run --rm \\"
  echo "         -v \"\$PWD/deploy/certs:/etc/letsencrypt\" \\"
  echo "         -v \"\$(docker volume inspect ${WEBROOT_VOL:-<卷名>} -f '{{.Mountpoint}}'):/var/www/certbot\" \\"
  echo "         certbot/certbot certonly --webroot -w /var/www/certbot \\"
  echo "         -d your-domain.com --email you@example.com --agree-tos --no-eff-email"
  echo "    4) 启用 HTTPS 配置："
  echo "       cp deploy/nginx-https.conf deploy/nginx.conf"
  echo "       # 再把里面的 your-domain.com 全部替换成你的域名"
  echo "       docker compose exec nginx nginx -s reload"
else
  echo "  访问地址：http://${IP:-服务器IP}:${APP_PORT:-8000}/"
  echo "  提示：加 --proxy 参数可启用 Nginx 反向代理"
fi
echo "─────────────────────────────────────────────"
echo "  查看状态：bash deploy/deploy.sh --status"
echo "  查看日志：bash deploy/deploy.sh --logs"
