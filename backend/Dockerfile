# 基础镜像可替换：网络受限时可通过 build arg 指向国内镜像源
# 正常情况无需关心，默认走 Docker Hub（配合 registry-mirrors 加速）
ARG BASE_IMAGE=python:3.12-slim
FROM ${BASE_IMAGE}

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    TZ=Asia/Shanghai

WORKDIR /app

# ---------------------------------------------------------------- 系统依赖
# 必须装 ffmpeg：哔哩哔哩的 DASH 高清档位（1080P60 / 4K / HDR）是
# 「视频流 + 音轨」两条独立文件，浏览器无法自行合并，需服务端用 ffmpeg
# 无损封装（-c copy）成一个 MP4。没装 ffmpeg 时后端会自动隐藏这些档位。
#
# apt 走国内镜像：deb.debian.org 在部分网络下会长时间卡住甚至失败。
# APT_MIRROR 可覆盖（例如 mirrors.aliyun.com）。
ARG APT_MIRROR=mirrors.tuna.tsinghua.edu.cn
RUN set -eux; \
    if [ -f /etc/apt/sources.list.d/debian.sources ]; then \
      sed -i "s|deb.debian.org|${APT_MIRROR}|g; s|security.debian.org|${APT_MIRROR}|g" \
        /etc/apt/sources.list.d/debian.sources; \
    fi; \
    if [ -f /etc/apt/sources.list ]; then \
      sed -i "s|deb.debian.org|${APT_MIRROR}|g; s|security.debian.org|${APT_MIRROR}|g" \
        /etc/apt/sources.list; \
    fi; \
    apt-get update; \
    apt-get install -y --no-install-recommends ffmpeg; \
    apt-get clean; \
    rm -rf /var/lib/apt/lists/*

COPY requirements.txt .

# pip 源可替换：国内强烈建议设为 https://pypi.tuna.tsinghua.edu.cn/simple
# PIP_EXTRA_INDEX_URL 是**备用源**：主源临时缺包/限流时会自动兜底，
# 避免因为单个镜像站抖动导致整个构建失败。
ARG PIP_INDEX_URL=
ARG PIP_EXTRA_INDEX_URL=
RUN set -eux; \
    PIP_ARGS="--no-cache-dir --disable-pip-version-check --retries 10 --timeout 120"; \
    if [ -n "${PIP_INDEX_URL}" ]; then \
      PIP_ARGS="$PIP_ARGS -i ${PIP_INDEX_URL}"; \
    fi; \
    if [ -n "${PIP_EXTRA_INDEX_URL}" ]; then \
      PIP_ARGS="$PIP_ARGS --extra-index-url ${PIP_EXTRA_INDEX_URL}"; \
    fi; \
    pip install $PIP_ARGS -r requirements.txt; \
    pip freeze > /app/installed-versions.txt

COPY . .

RUN useradd -m -u 10001 appuser && chown -R appuser:appuser /app
USER appuser

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/',timeout=4).status==200 else 1)"

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000", \
     "--proxy-headers", "--forwarded-allow-ips", "*"]
