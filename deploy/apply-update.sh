#!/usr/bin/env bash
# 从解压后的更新包执行：bash deploy/apply-update.sh /服务器/旧项目目录 [--clean]
set -euo pipefail
umask 077
PACKAGE="$(cd "$(dirname "$0")/.." && pwd -P)"
[[ $# -ge 1 && $# -le 2 ]] || { echo '用法：bash deploy/apply-update.sh /旧项目目录 [--clean]' >&2; exit 1; }
[[ $# -eq 1 || $2 == --clean ]] || { echo '第二个参数仅支持 --clean' >&2; exit 1; }
TARGET="$(cd "$1" && pwd -P)"
[[ "$TARGET" != "$PACKAGE" ]] || { echo '请把更新包解压到单独目录，不要覆盖旧项目。' >&2; exit 1; }
[[ -f "$TARGET/.env" && -f "$TARGET/docker-compose.yml" && -f "$TARGET/backend/main.py" ]] || { echo '目标目录不是完整的已有部署（需要 .env、docker-compose.yml、backend/main.py）。' >&2; exit 1; }
[[ -f "$PACKAGE/SHA256SUMS" ]] || { echo '缺少更新包内部校验清单。' >&2; exit 1; }
(cd "$PACKAGE" && sha256sum --check --quiet SHA256SUMS)
cd "$TARGET"
docker info >/dev/null
docker compose config --quiet
[[ -n "$(docker compose ps -q web)" ]] || { echo '旧 web 服务没有运行，请先检查部署。' >&2; exit 1; }
BACKUP="$TARGET/.update-backups/code-$(date +%Y%m%d-%H%M%S)-$$"
mkdir -p "$BACKUP"
FILES=(backend docker-compose.yml .env)
for file in deploy/update.sh README.md .env.example; do
  [[ ! -f "$file" ]] || FILES+=("$file")
done
tar --exclude='__pycache__' --exclude='.venv' --exclude='venv' --exclude='*.pyc' -czf "$BACKUP/before-update.tar.gz" "${FILES[@]}"
echo "代码和配置已备份：$BACKUP/before-update.tar.gz（包含凭据，请妥善保管）"
# 仅覆盖发布内容；现有 Compose、.env、Nginx、证书及其他部署脚本保留。
cp -a "$PACKAGE/backend/." "$TARGET/backend/"
mkdir -p "$TARGET/deploy"
cp "$PACKAGE/deploy/update.sh" "$TARGET/deploy/update.sh"
cp "$PACKAGE/UPDATE.md" "$TARGET/UPDATE.md"
cp "$PACKAGE/.env.example" "$TARGET/.env.example"
cp "$PACKAGE/README.md" "$TARGET/README.md"
echo '配置已保留；开始重建并更新服务。'
if ! bash deploy/update.sh "${@:2}"; then
  echo "更新未完成。代码备份：$BACKUP/before-update.tar.gz；请按 UPDATE.md 排查或回滚。" >&2
  exit 1
fi
echo "更新成功。代码备份：$BACKUP/before-update.tar.gz"
