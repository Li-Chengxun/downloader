#!/usr/bin/env bash
#
# 抖音 / 哔哩哔哩 视频下载站 — 部署前环境自检
#
# 用法（在服务器上执行）：
#   bash deploy/preflight.sh
#
# 只做检查，不修改任何东西。部署前跑一遍能提前发现 90% 的问题。

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; BLUE='\033[0;34m'; NC='\033[0m'
FAIL=0

section() { printf "\n${BLUE}── %s ──${NC}\n" "$1"; }
ok()      { printf "  ${GREEN}✓${NC} %s\n" "$1"; }
bad()     { printf "  ${RED}✗${NC} %s\n" "$1"; FAIL=$((FAIL+1)); }
warn()    { printf "  ${YELLOW}!${NC} %s\n" "$1"; }

printf "${BLUE}抖音 / 哔哩哔哩 视频下载站 · 部署前自检${NC}\n"

# ── 1. 项目定位 ──
section "项目目录"
PROJ="$(cd "$(dirname "$0")/.." && pwd)"
if [[ -f "$PROJ/backend/main.py" && -f "$PROJ/docker-compose.yml" ]]; then
    ok "项目根目录：$PROJ"
else
    bad "项目文件不完整，缺少 backend/main.py 或 docker-compose.yml"
fi
for f in backend/main.py backend/parser.py backend/bilibili.py backend/sessions.py \
         backend/requirements.txt backend/static/index.html \
         backend/static/vendor/vue.global.prod.js backend/static/vendor/qrcode.js \
         docker-compose.yml .env.example; do
    [[ -f "$PROJ/$f" ]] && ok "$f" || bad "缺少 $f"
done

# ── 2. Docker ──
section "Docker 环境"
if command -v docker >/dev/null 2>&1; then
    ok "$(docker --version 2>&1)"
    if docker compose version >/dev/null 2>&1; then
        ok "$(docker compose version 2>&1 | head -1)"
    else
        bad "缺少 docker compose 插件（需要 v2），请升级 Docker"
    fi
    if docker info >/dev/null 2>&1; then
        ok "Docker 守护进程可访问"
    else
        bad "无法访问 Docker 守护进程。用 sudo 运行，或执行：sudo usermod -aG docker \$USER && newgrp docker"
    fi
else
    bad "未安装 Docker，请先执行：curl -fsSL https://get.docker.com | sh && sudo systemctl enable --now docker"
fi

# ── 3. 出网能力（最关键）──
section "网络连通性"
check_url() {
    local name="$1" url="$2"
    local code
    code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 10 "$url" 2>/dev/null)"
    if [[ "$code" =~ ^(200|301|302|403)$ ]]; then
        ok "$name 可达（HTTP $code）"
    elif [[ -z "$code" || "$code" == "000" ]]; then
        bad "$name 不可达 —— 超时或被拦截，这是解析失败的头号原因"
    else
        warn "$name 返回 HTTP $code"
    fi
}
check_url "抖音主站 douyin.com"  "https://www.douyin.com/"
check_url "分享页 iesdouyin.com" "https://www.iesdouyin.com/"
check_url "抖音解析引擎 api.douyin.wtf" "https://api.douyin.wtf/"
# 哔哩哔哩：解析走官方 web 接口，下载走 CDN，两者域名不同，都要通
check_url "B站接口 api.bilibili.com" "https://api.bilibili.com/x/web-interface/view?bvid=BV1GJ411x7h7"
check_url "B站短链 b23.tv"           "https://b23.tv/BV1GJ411x7h7"
check_url "B站封面 i0.hdslb.com"     "https://i0.hdslb.com/"

# Docker 仓库连通性：拉取基础镜像（python/nginx）依赖它
# 国内服务器连不上 Docker Hub 会导致 build 报 "load metadata ... i/o timeout"
REG_CODE="$(curl -s -o /dev/null -w '%{http_code}' --max-time 10 https://registry-1.docker.io/v2/ 2>/dev/null)"
MIRROR_OK=0
for m in https://docker.m.daocloud.io https://docker.1ms.run https://docker.1panel.live; do
    c="$(curl -s -o /dev/null -w '%{http_code}' --max-time 6 "${m}/v2/" 2>/dev/null)"
    [[ "$c" =~ ^(200|401)$ ]] && MIRROR_OK=1 && break
done
if [[ "$REG_CODE" =~ ^(200|401)$ ]]; then
    ok "Docker Hub 直连可达（HTTP $REG_CODE）"
elif [[ $MIRROR_OK -eq 1 ]]; then
    warn "Docker Hub 直连不通，但加速源可用（已配置镜像加速？）"
else
    bad "Docker 仓库不可达 —— 构建镜像会报 i/o timeout。执行：sudo bash deploy/fix-docker-mirror.sh"
fi

# ── 3.5 ffmpeg（仅裸机部署需要；Docker 镜像已内置）──
section "ffmpeg（DASH 高清档位）"
FF_BIN="${FFMPEG_BIN:-}"
if [[ -n "$FF_BIN" ]]; then
    if [[ -x "$FF_BIN" ]]; then
        ok "FFMPEG_BIN 指向可用文件：$FF_BIN"
    else
        bad "FFMPEG_BIN=$FF_BIN 不存在或不可执行"
    fi
elif command -v ffmpeg >/dev/null 2>&1; then
    ok "$(ffmpeg -version 2>&1 | head -1)"
else
    warn "未安装 ffmpeg —— 1080P60 / 4K / HDR 等 DASH 高清档位将不可用（其余功能正常）"
    warn "  裸机部署可执行：sudo apt-get install -y ffmpeg"
    warn "  用 docker compose 部署则无需处理，镜像已内置"
fi

# ── 4. 端口占用 ──
section "端口占用"
if command -v ss >/dev/null 2>&1; then
    for p in 80 443 8000; do
        if ss -lnt 2>/dev/null | grep -q ":$p "; then
            warn "端口 $p 已被占用（若用 --proxy 会冲突，注意改端口）"
        else
            ok "端口 $p 空闲"
        fi
    done
else
    warn "无 ss 命令，跳过端口检查"
fi

# ── 5. 资源 ──
section "服务器资源"
if command -v free >/dev/null 2>&1; then
    MEM_MB="$(free -m | awk '/^Mem:/{print $2}')"
    if [[ "${MEM_MB:-0}" -ge 1800 ]]; then
        ok "内存 ${MEM_MB}MB"
    else
        warn "内存仅 ${MEM_MB}MB，建议 2G 以上（下载代理较吃内存）"
    fi
fi
CPU="$(nproc 2>/dev/null || echo '?')"
ok "CPU 核心数：$CPU"
AVAIL="$(df -h "$PROJ" | awk 'NR==2{print $4}')"
ok "可用磁盘：$AVAIL"

# ── 6. 现有部署状态 ──
section "现有部署"
if command -v docker >/dev/null 2>&1 && docker info >/dev/null 2>&1; then
    if [[ -n "$(docker ps -q -f name=douyin 2>/dev/null)" ]]; then
        warn "检测到已运行的容器："
        docker ps --filter name=douyin --format '    {{.Names}}  {{.Status}}  {{.Ports}}'
    else
        ok "无运行中的本项目容器（首次部署正常）"
    fi
else
    warn "Docker 不可用，跳过"
fi

# ── 结论 ──
printf "\n"
if [[ $FAIL -eq 0 ]]; then
    printf "${GREEN}自检通过，可以部署。${NC}\n\n下一步：\n"
    printf "  cd %s\n" "$PROJ"
    if [[ -n "$(docker ps -q -f name=douyin 2>/dev/null)" ]]; then
        # 已经有容器在跑 = 这是「更新」而非「首次部署」
        printf "  bash deploy/update.sh            ${GREEN}# 更新已有部署（不动 .env / 防火墙）${NC}\n"
        printf "  # 首次部署请改用：bash deploy/deploy.sh --proxy\n\n"
    else
        printf "  cp .env.example .env\n"
        printf "  bash deploy/deploy.sh --proxy\n\n"
    fi
else
    printf "${RED}发现 %d 个阻塞问题，请先按上面的 ✗ 提示解决。${NC}\n" "$FAIL"
fi
