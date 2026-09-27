#!/usr/bin/env bash
#
# 部署进度诊断 —— 判断是「正在下载」还是「真卡住」
#
# 用法：另开一个 SSH 窗口，执行
#   bash deploy/check-progress.sh
#
# 原理：观察 /var/lib/docker 体积是否在增长、网络是否在跑。
# 体积在涨 = 正在下载，继续等；体积不动 = 卡住了，按提示处理。

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; BLUE='\033[0;34m'; NC='\033[0m'
ok()   { printf "  ${GREEN}✓${NC} %s\n" "$1"; }
bad()  { printf "  ${RED}✗${NC} %s\n" "$1"; }
warn() { printf "  ${YELLOW}!${NC} %s\n" "$1"; }
info() { printf "\n${BLUE}── %s ──${NC}\n" "$1"; }

SUDO=""
[[ $EUID -ne 0 ]] && SUDO="sudo"

SIZE1=""
if [[ -d /var/lib/docker ]]; then
    SIZE1="$($SUDO du -sm /var/lib/docker 2>/dev/null | awk '{print $1}')"
fi

info "1. Docker 里的活动"
if $SUDO docker ps --format '  {{.Names}}  {{.Status}}' 2>/dev/null | grep -q .; then
    $SUDO docker ps --format '  {{.Names}}  {{.Status}}  {{.Ports}}'
else
    warn "没有运行中的容器（构建阶段属正常）"
fi

# 看是否有活跃的 buildkit / pip / apt 进程
PROCS="$($SUDO ps aux 2>/dev/null | grep -E 'buildkit|docker-buildx|pip |apt-get|uvicorn' | grep -v grep | head -5)"
if [[ -n "$PROCS" ]]; then
    echo "$PROCS" | awk '{printf "  %s %s%%  %s\n", $11, $3, substr($0, index($0,$11))}' | cut -c1-100
else
    warn "没有发现活跃的构建/安装进程"
fi

info "2. 镜像是否已拉到"
$SUDO docker images --format '  {{.Repository}}:{{.Tag}}  {{.Size}}' 2>/dev/null | grep -E 'python|nginx|douyin' || warn "还没有相关镜像"

info "3. 下载是否在进行（观察 12 秒）"
if [[ -n "$SIZE1" ]]; then
    printf "  /var/lib/docker 当前 %sMB，等待 12 秒...\n" "$SIZE1"
    sleep 12
    SIZE2="$($SUDO du -sm /var/lib/docker 2>/dev/null | awk '{print $1}')"
    DIFF=$(( ${SIZE2:-0} - ${SIZE1:-0} ))
    printf "  12 秒后 %sMB（变化 %+dMB）\n" "$SIZE2" "$DIFF"
    if [[ $DIFF -gt 2 ]]; then
        ok "体积在增长 → 正在下载，网络是通的，继续等即可"
    elif [[ $DIFF -ge 0 ]]; then
        warn "体积几乎没变 → 很可能卡住了（见下方处理办法）"
    else
        warn "体积减少了 → 可能在做清理，稍后再看一次"
    fi
else
    warn "无法读取 /var/lib/docker"
fi

info "4. 当前加速源配置"
if [[ -f /etc/docker/daemon.json ]]; then
    cat /etc/docker/daemon.json | sed 's/^/  /'
else
    bad "没有 /etc/docker/daemon.json —— 加速源未配置"
fi

info "5. 逐个实测加速源速度"
FAST=""
for m in https://docker.m.daocloud.io https://docker.1ms.run https://docker.1panel.live https://docker.1panelproxy.com; do
    R="$(curl -s -o /dev/null -w '%{http_code} %{speed_download} %{time_total}' --max-time 10 "${m}/v2/" 2>/dev/null)"
    CODE="$(echo "$R" | awk '{print $1}')"
    SPD="$(echo "$R" | awk '{print $2}')"
    SPD_KB="$(awk "BEGIN{printf \"%.0f\", ${SPD:-0}/1024}")"
    if [[ "$CODE" =~ ^(200|401)$ ]]; then
        ok "${m}  （HTTP ${CODE}, ${SPD_KB} KB/s）"
        [[ -z "$FAST" ]] && FAST="$m"
    else
        bad "${m}  （${CODE:-超时}）"
    fi
done

info "6. 应用是否已起来"
for p in 80 8000 8001; do
    if curl -fsS --max-time 3 "http://127.0.0.1:${p}/" >/dev/null 2>&1; then
        ok "端口 ${p} 有响应 —— 应用可能已经跑起来了！"
    fi
done

printf "\n${BLUE}════ 结论与处理 ════${NC}\n"
if $SUDO docker ps --format '{{.Names}}' 2>/dev/null | grep -q douyin; then
    printf "${GREEN}容器已在运行。${NC}浏览器打开 http://服务器IP/ 试试。\n"
    printf "若打不开，检查云厂商安全组是否放通 80 端口。\n"
else
    printf "如果上面第 3 步显示「体积在增长」→ 正常下载中，再等几分钟。\n\n"
    printf "如果显示「体积几乎没变」→ 卡住了，按这个顺序处理：\n\n"
    printf "  ① 在原窗口按 Ctrl+C 中断\n\n"
    printf "  ② 换兜底通道（绕开 Docker Hub，直接从国内源拉基础镜像）：\n"
    printf "     cd /opt/douyin-downloader\n"
    printf "     sed -i 's|^# BASE_IMAGE=|BASE_IMAGE=|; s|^# NGINX_IMAGE=|NGINX_IMAGE=|; s|^# PIP_INDEX_URL=|PIP_INDEX_URL=|' .env\n"
    printf "     cat .env | grep -E 'BASE_IMAGE|NGINX_IMAGE|PIP_INDEX_URL'\n\n"
    printf "  ③ 重新部署：\n"
    printf "     sudo bash deploy/deploy.sh --proxy\n\n"
    if [[ -n "$FAST" ]]; then
        FAST_HOST="${FAST#https://}"; FAST_HOST="${FAST%/}"
        printf "  当前最快的源是 %s，可单独指定：\n" "$FAST"
        printf "     echo 'BASE_IMAGE=%s/library/python:3.12-slim' >> .env\n" "$FAST_HOST"
        printf "     echo 'NGINX_IMAGE=%s/library/nginx:1.27-alpine' >> .env\n" "$FAST_HOST"
    fi
fi
printf "\n"
