#!/usr/bin/env bash
# 已有 Docker 部署更新：保留配置，备份运行镜像，检查失败时恢复旧镜像。
# 用法：bash deploy/update.sh [--clean]
set -Eeuo pipefail
cd "$(dirname "$0")/.."
[[ ${1:-} != --help && ${1:-} != -h ]] || { echo '用法：bash deploy/update.sh [--clean]'; exit 0; }
# Bash 4.2 / 4.3 在 set -u 下展开空数组会报 unbound variable；始终保留服务名。
BUILD_ARGS=(web)
if [[ ${1:-} == --clean && $# -eq 1 ]]; then
  BUILD_ARGS=(--no-cache web)
elif [[ $# -ne 0 ]]; then
  echo '仅支持 --clean 或 --help' >&2; exit 1
fi
[[ -f .env && -f docker-compose.yml && -f backend/main.py ]] || { echo '缺少已有部署文件；本脚本不用于首次部署。' >&2; exit 1; }
docker info >/dev/null
docker compose config --quiet
CURRENT_CONTAINER="$(docker compose ps -q web)"
[[ -n "$CURRENT_CONTAINER" ]] || { echo '未找到正在运行的 web 容器，请先检查旧服务。' >&2; exit 1; }
SERVICE_IMAGE="$(docker inspect --format '{{.Config.Image}}' "$CURRENT_CONTAINER")"
CURRENT_IMAGE="$(docker inspect --format '{{.Image}}' "$CURRENT_CONTAINER")"
BACKUP_TAG="douyin-downloader:backup-$(date +%Y%m%d-%H%M%S)-$$"
docker tag "$CURRENT_IMAGE" "$BACKUP_TAG"
mkdir -p .update-backups
umask 077
STATE_FILE=".update-backups/image-$(date +%Y%m%d-%H%M%S)-$$.txt"
printf 'BACKUP_TAG=%s\nSERVICE_IMAGE=%s\n' "$BACKUP_TAG" "$SERVICE_IMAGE" > "$STATE_FILE"
echo "旧镜像已备份：$BACKUP_TAG（记录：$STATE_FILE）"

NGINX_CONTAINER="$(docker compose --profile proxy ps -q nginx)"
reload_proxy() {
  [[ -z "$NGINX_CONTAINER" ]] || docker exec "$NGINX_CONTAINER" nginx -s reload
}
if [[ -n "$NGINX_CONTAINER" ]]; then
  docker exec "$NGINX_CONTAINER" nginx -t
fi
SWAPPED=0
wait_ready() {
  local attempts=$1
  for ((i=0; i<attempts; i++)); do
    if docker compose exec -T web python -c 'import urllib.request; assert urllib.request.urlopen("http://127.0.0.1:8000/", timeout=4).status == 200' >/dev/null 2>&1; then
      return 0
    fi
    sleep 2
  done
  return 1
}
failed() {
  trap - ERR
  echo '更新失败，正在恢复旧镜像标签。' >&2
  docker compose logs --tail=50 web >&2 || true
  if ! docker tag "$BACKUP_TAG" "$SERVICE_IMAGE"; then
    echo "恢复镜像标签失败，请根据 $STATE_FILE 手动回滚。" >&2
  elif [[ $SWAPPED -eq 1 ]]; then
    if docker compose up -d --no-deps --force-recreate web && wait_ready 30 && reload_proxy; then
      echo '已恢复旧镜像并通过启动检查。代码文件仍为新版，可按 UPDATE.md 恢复代码备份。' >&2
    else
      echo "自动恢复未通过检查，请根据 $STATE_FILE 和 UPDATE.md 手动回滚。" >&2
    fi
  else
    echo '旧容器没有被替换，继续运行。代码文件可按 UPDATE.md 恢复。' >&2
  fi
  exit 1
}
trap failed ERR
echo '构建新版镜像（构建期间旧服务继续运行）...'
docker compose build "${BUILD_ARGS[@]}"
# 使用现有 Compose 的环境变量验证新后端，配置错误在替换旧容器之前发现。
docker compose run --rm --no-deps -T --entrypoint python web -c 'import main'
SWAPPED=1
docker compose up -d --no-deps --force-recreate web
echo '等待新版后端就绪（最长约 3 分钟）...'
wait_ready 30
docker compose exec -T web ffmpeg -version >/dev/null
docker compose exec -T web python -c 'import urllib.request; r=urllib.request.urlopen("http://127.0.0.1:8000/static/style.css", timeout=4); assert r.status == 200 and r.read()'
# web 重建后可能更换容器 IP；让现有 Nginx 重新解析上游，保留配置与证书。
reload_proxy
trap - ERR
docker compose ps
echo "更新完成。回滚镜像：$BACKUP_TAG；操作步骤见 UPDATE.md。"
