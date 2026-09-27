#!/usr/bin/env bash
#
# 国内网络加速一键修复
#
# 用法（在服务器上执行）：
#   sudo bash deploy/fix-docker-mirror.sh
#
# 解决三类拉取超时：
#   1) Docker Hub 拉基础镜像   → 配 registry-mirrors（实测可用源）
#   2) 容器内 pip 装依赖       → 写 PIP_INDEX_URL 到项目 .env
#   3) 附：apt 源慢             → 新版 Dockerfile 已不做 apt 安装，无需处理
#
# 特点：只写入实测可用的源；改动前备份；失败自动回滚。

set -uo pipefail

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; BLUE='\033[0;34m'; NC='\033[0m'
ok()   { printf "  ${GREEN}✓${NC} %s\n" "$1"; }
bad()  { printf "  ${RED}✗${NC} %s\n" "$1"; }
warn() { printf "  ${YELLOW}!${NC} %s\n" "$1"; }
info() { printf "\n${BLUE}── %s ──${NC}\n" "$1"; }

DAEMON_JSON=/etc/docker/daemon.json

if [[ $EUID -ne 0 ]]; then
    printf "${RED}请用 sudo 运行：sudo bash deploy/fix-docker-mirror.sh${NC}\n"
    exit 1
fi

printf "${BLUE}Docker 镜像加速配置${NC}\n"

# 候选加速源（按推荐顺序；已剔除确认失效的域名）
CANDIDATES=(
    "https://docker.m.daocloud.io"
    "https://docker.1ms.run"
    "https://docker.1panel.live"
    "https://hub.rat.dev"
    "https://docker.mirrors.sjtug.sjtu.edu.cn"
    "https://docker.1panelproxy.com"
)

info "测试各加速源连通性"
WORKING=()
for m in "${CANDIDATES[@]}"; do
    code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 8 "${m}/v2/" 2>/dev/null)"
    if [[ "$code" =~ ^(200|401)$ ]]; then
        ok "${m}  （HTTP ${code}）"
        WORKING+=("$m")
    else
        bad "${m}  （${code:-超时}）"
    fi
done

if [[ ${#WORKING[@]} -eq 0 ]]; then
    printf "\n${RED}所有候选加速源均不可用。${NC}\n"
    printf "可能原因：服务器网络整体受限。请检查：\n"
    printf "  1) 能否访问外网：curl -I https://www.baidu.com\n"
    printf "  2) 云厂商是否有出网限制 / 安全组规则\n"
    printf "  3) 若有 HTTP 代理，可配置 Docker 走代理：\n"
    printf "     /etc/systemd/system/docker.service.d/http-proxy.conf\n"
    exit 1
fi

info "备份原配置"
if [[ -f "$DAEMON_JSON" ]]; then
    BACKUP="${DAEMON_JSON}.bak.$(date +%Y%m%d%H%M%S)"
    cp "$DAEMON_JSON" "$BACKUP"
    ok "已备份到 $BACKUP"
    # 若原配置含 registry-mirrors 以外的键（如 data-root），提示避免覆盖
    if grep -qE '"(data-root|exec-opts|log-driver|bip|insecure-registries)"' "$DAEMON_JSON" 2>/dev/null; then
        warn "检测到原 daemon.json 还含有其它配置项"
        warn "为避免覆盖，请手动把下面 registry-mirrors 合并进 $DAEMON_JSON，然后重启 Docker"
        printf "\n"
        printf '{\n  "registry-mirrors": [\n'
        for m in "${WORKING[@]}"; do printf '    "%s",\n' "$m"; done
        printf '  ]\n}\n\n'
        exit 0
    fi
fi

info "写入加速配置"
mkdir -p /etc/docker
{
    printf '{\n  "registry-mirrors": [\n'
    n=${#WORKING[@]}
    i=0
    for m in "${WORKING[@]}"; do
        i=$((i+1))
        if [[ $i -lt $n ]]; then
            printf '    "%s",\n' "$m"
        else
            printf '    "%s"\n' "$m"
        fi
    done
    printf '  ]\n}\n'
} > "$DAEMON_JSON"
ok "已写入 $DAEMON_JSON（${#WORKING[@]} 个可用源）"

info "重启 Docker"
systemctl daemon-reload
if systemctl restart docker; then
    ok "Docker 已重启"
else
    printf "${RED}Docker 重启失败，正在回滚配置...${NC}\n"
    # 回滚到最近一次备份
    LAST_BACKUP="$(ls -t ${DAEMON_JSON}.bak.* 2>/dev/null | head -1)"
    if [[ -n "$LAST_BACKUP" ]]; then
        cp "$LAST_BACKUP" "$DAEMON_JSON"
        systemctl restart docker
        printf "${YELLOW}已回滚，请检查 $LAST_BACKUP${NC}\n"
    else
        rm -f "$DAEMON_JSON"
        systemctl restart docker
        printf "${YELLOW}已删除异常配置并重启${NC}\n"
    fi
    exit 1
fi

info "验证配置生效"
if docker info 2>/dev/null | grep -A 10 "Registry Mirrors" | grep -q "http"; then
    docker info 2>/dev/null | grep -A 10 "Registry Mirrors" | sed 's/^/    /'
    ok "加速源已生效"
else
    warn "docker info 里没看到 Registry Mirrors，配置可能未加载"
fi

info "实测拉取基础镜像"
if docker pull python:3.12-slim >/dev/null 2>&1; then
    ok "python:3.12-slim 拉取成功"
else
    warn "拉取仍失败，可能需要换网络环境或配置代理"
    exit 1
fi

# ── pip 源：写入项目 .env，避免容器内 pip 装依赖时也卡住 ──
info "配置 pip 加速（写入项目 .env）"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJ="$(cd "$SCRIPT_DIR/.." && pwd)"
ENV_FILE="$PROJ/.env"
PIP_MIRROR="https://pypi.tuna.tsinghua.edu.cn/simple"

if [[ -f "$ENV_FILE" ]]; then
    OWNER="$(stat -c '%U:%G' "$ENV_FILE" 2>/dev/null || echo '')"
    CUR="$(grep -E '^PIP_INDEX_URL=' "$ENV_FILE" 2>/dev/null | head -1 | cut -d= -f2- | xargs || true)"
    if [[ -n "$CUR" ]]; then
        ok "PIP_INDEX_URL 已配置：$CUR"
    else
        cp "$ENV_FILE" "${ENV_FILE}.bak.$(date +%Y%m%d%H%M%S)"
        sed -i '/^PIP_INDEX_URL=/d' "$ENV_FILE" 2>/dev/null || true
        printf 'PIP_INDEX_URL=%s\n' "$PIP_MIRROR" >> "$ENV_FILE"
        ok "已写入 PIP_INDEX_URL=$PIP_MIRROR"
        [[ -n "$OWNER" ]] && chown "$OWNER" "$ENV_FILE" 2>/dev/null || true
    fi
else
    warn "$ENV_FILE 不存在。稍后执行 cp .env.example .env 即可（模板已默认启用国内 pip 源）"
fi

printf "\n${GREEN}配置完成，可以继续部署了：${NC}\n"
printf "  cd %s\n" "$PROJ"
printf "  sudo bash deploy/deploy.sh --proxy\n\n"
printf "${YELLOW}提示：${NC}若构建仍卡在 apt / deb.debian.org，那是旧版 Dockerfile 的行为，\n"
printf "      请确认已上传最新版 backend/Dockerfile（新版不做任何 apt 安装）。\n\n"
