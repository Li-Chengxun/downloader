# 抖音 / 哔哩哔哩 视频下载站

粘贴分享链接，自动识别平台与内容类型，解析出封面、标题、简介与全部可用清晰度，一键下载原画质视频（不二次转码）。

**两个平台合并为一条通道**：只有一个输入框，不需要选择平台，也不需要选择内容类型。

### 下载与资源保护

下载、封面、图片打包和 DASH 媒体请求共用 `backend/media_http.py`：只接受明确的
平台域名及其子域名、HTTP/HTTPS 标准端口，拒绝 URL 里的用户名密码。每次跳转都
重新检查域名；DNS 结果必须全部是公网地址，实际连接固定到已验证的 IP，同时保留
原始 Host 与 TLS 证书校验域名。媒体请求不使用系统代理，也不向 CDN 发送 B 站账号 Cookie。

`EXTRA_DOUYIN_HOSTS` / `EXTRA_BILIBILI_HOSTS` 的值现在必须是**完整域名**，
例如 `example-cdn.com`（自动包含子域名），原先 `douyin` / `upos` 等关键字配置需要替换。
默认不放通第三方 PCDN 节点；确认是可信 CDN 后再添加域名。

普通视频下载代理转发 `Range` / `If-Range`，保留上游 `206` / `416`、
`Content-Range` / `Accept-Ranges` / `ETag` 等响应信息；能否续传取决于上游 CDN。
高清视频、ZIP 和幻灯片先生成，再由浏览器下载管理器接收，前端不再把完整文件读入 Blob。

生成接口附加 `prepare=true` 时返回 `{ok, download_url, expires_in}`；不附加时仍直接
返回文件，兼容原 API。生成地址属于当前浏览器会话，有效期默认 10 分钟，期间可以
重试或续传。过期文件每 30 秒清理一次，正在传输的文件会等传输结束再删除。
服务重启会使生成地址和扫码会话失效。所有额度都是**单进程**的，不支持多 worker
共享会话或文件状态。

| 配置 | 默认值 | 用途 |
|---|---:|---|
| `MAX_IMAGES` | 100 | 每次最多打包 / 合成的图片数量 |
| `MAX_IMAGE_BYTES` | 20971520 | 单张图片 / 封面最多 20 MiB |
| `MAX_STREAM_BYTES` | 4294967296 | 单条媒体流最多 4 GiB |
| `MAX_MEDIA_JOBS` | 2 | 同时生成文件的任务数量 |
| `MAX_DOWNLOADS` | 16 | 下载代理和封面拉取的并发数量 |
| `JOB_TIMEOUT` | 900 | 单次生成任务总超时，秒 |
| `FFMPEG_TIMEOUT` | 300 | ffmpeg 执行超时，秒 |
| `MAX_SESSIONS` | 4096 | 内存会话数量上限 |
| `MAX_PREPARED_FILES` | 8 | 尚未过期的生成文件数量 |
| `PREPARED_FILE_TTL` | 600 | 生成地址有效期，秒 |

以上数值必须为正整数，可在 `.env` 中调整，Docker Compose 与本地启动脚本均会加载。
任务满时立即返回 `429`，会话满时返回 `503`，不会无限排队或挤掉其他访客的登录态。
缺少 `Content-Length` 的媒体也会边读取边计数；若传输开始后才超限，会终止连接。
ffmpeg 超时或任务取消时会结束子进程并清理临时目录。

### 回归测试

安装 `backend/requirements.txt` 后，在项目根目录执行：

```bash
python -m unittest discover -s backend/tests -v
```

测试使用模拟媒体和平台接口，不需要真实 Cookie 或联网，覆盖地址与 DNS 校验、
跳转限制、大小限制、Range 响应、并发额度、会话隔离、二维码失效、生成文件有效期
及取消时的资源清理。GitHub Actions 会在 push / pull request 时运行同一组测试。

抖音侧三类内容（**视频 / 图文帖 / 纯文字帖**）都能处理：图文帖可逐张下无水印原图、打包成 zip（**内附文案 Word**）或合成为幻灯片视频；长图文（「文章」）可在卡片上展开读全文。

| 平台      | 支持的输入形态                                                                             |
| ------- | ----------------------------------------------------------------------------------- |
| 抖音      | `v.douyin.com` 短链、`www.douyin.com/video/{id}` 长链、`?modal_id=` 网页版链接、整段分享文字                 |
| 抖音图文    | `www.douyin.com/note/{id}` 长链、整段分享文字（界面上会标注「抖音图文」，提供图片网格与两种下载形式）                       |
| 哔哩哔哩    | `b23.tv` 短链、`www.bilibili.com/video/BV…` 长链、`av` 号、整段分享文字；多分P视频可切换选集                    |
| 哔哩哔哩番剧  | `bangumi/play/ep…`（单集）、`bangumi/play/ss…`（整季）、`bangumi/media/md…`（作品）；裸编号 `ep123` / `ss123` / `md123`；可切换集数与同作品其他季 |

### 平台识别规则

识别逻辑在前端（实时提示）和后端（真正路由）各有一份，**两边规则必须保持一致**，否则会出现「界面提示哔哩哔哩、实际却按抖音解析」的错位。判定顺序（越靠前越明确）：

1. 输入里有链接就**先抽出链接**再判定（分享文案里可能混着无关域名）；
2. 链接含 B 站域名（`bilibili.com` / `b23.tv`）→ **哔哩哔哩**（番剧链接走的就是这条）；
3. 链接含抖音域名（`douyin.com` / `iesdouyin.com`）→ **抖音**；
4. 没有链接时才可能是**裸编号**（`BV…` / `av…` / 番剧 `ep…`·`ss…`·`md…`）→ **哔哩哔哩**；
5. 都识别不出 → 按**抖音**兜底。

两个容易写错的地方：

- **第 4 步必须在抖音域名之后**：抖音分享文案常夹带无关字符，先认裸号会把文案里恰好出现的 `BV…` 误判成 B 站。
- **裸编号用「整个输入就是编号」的整串匹配**，不是在文本里搜。宽松匹配有真实误判——抖音文案里出现 `md5` 就会被 `md\d+` 命中而误判成番剧。用户从 App 分享出来的番剧一定是完整 URL（走域名规则），裸编号只可能是手输、格式必然干净，收紧不会漏掉正常输入。

第 5 步兜底抖音而不是统一报「无法识别」，是为了让报错更有指导性——抖音侧会明确提示「这不是抖音链接」。

判定只到「平台」这一层，**不再细分普通视频 / 番剧**——那是 `bilibili.parse()` 内部的事。

## 界面设计

清爽现代路线（冷调浅底 + 白色浮层 + 柔和投影 + 大圆角），但守两条纪律，避免滑向"满屏圆角卡片的模板感"：

1. **颜色分工**——靛蓝 `#4f46e5` **只用于「动作」**（取回 / 下载），平台色（抖音 `#111827`、哔哩哔哩 `#be185d`）**只用于「信息」**（徽标 / 识别状态），两者不混用。颜色始终带着含义，不是装饰。
2. **层级靠投影和底色差**——三种圆角对应三种层级（控件 14px / 面板 20px / 标签胶囊）；全站只有一张卡片（结果面板），不是卡片堆叠。

几个具体决定：

- 容器 1000px **把宽屏用满**；结果卡里封面与文字**并排**，不让封面独占一行；
- 清晰度每档带一条**体积对比条**（按本视频最大档归一化）。做得细、且一律中性色，免得被误读成"正在下载"的进度条；
- **空状态不留大片空白**：摆出「支持的内容」六项与三条使用要点，顺便帮用户判断手上这东西能不能用；
- 页脚**贴在视口底部**（flex 布局），内容少时页面不会像"浮在顶上"；
- 识别状态是一枚**浅底胶囊**，紧随输入框下方；
- 「取回」旁边有**「粘贴」按钮**（浅底 ghost 款，主色仍只留给「取回」，避免两个实心按钮打架）：一键把剪贴板内容填进输入框。**它只在真读得到剪贴板时才渲染**，原因见 FAQ 24；
- 只用系统字体（这站要能离线使用）；键盘焦点可见、尊重系统的「减少动态效果」、窄屏自适应。

样式集中在 `backend/static/style.css`，页面模板在 `backend/static/index.html`，**没有构建步骤**。
首屏采用浅色背景、靛蓝动作按钮和收藏主题插画；支持内容按网格分组，结果出现后收起主视觉。
390px 手机宽度下，输入和按钮分行、图文采用双列，画质下载行改为上下排列。
备选风格存样在 `_design/styles.html`（暗色 / 清爽 / 浓色）。

本地检查不同内容的布局，可用项目虚拟环境执行 `_design/preview.py`，打开
`http://127.0.0.1:8766`，输入 `video` / `images` / `text` / `bangumi` 后点击解析。
该服务仅使用合成内容，不访问视频平台，与正式后端分开运行。

## 项目结构

```
douyin-downloader/
├── backend/
│   ├── main.py              # FastAPI：/api/parse（含平台识别）、/api/download、/api/dash、/api/cover、B站登录、静态托管
│   ├── parser.py            # 抖音解析：链接规范化 + 双引擎 + 内容类型分发（视频 / 图文 / 文字）
│   ├── bilibili.py          # 哔哩哔哩解析：parse() 内分普通投稿(UGC)与番剧(PGC) + 画质枚举 + DASH 合并 + 扫码登录
│   ├── slideshow.py         # 图文帖产物：图片打包 zip（含文案 docx）/ ffmpeg 合成幻灯片
│   ├── ffmpeg_tool.py       # ffmpeg 定位与探测（DASH 合并与图文合成共用）
│   ├── sessions.py          # 会话表（内存）：按访客隔离扫码凭据
│   ├── requirements.txt     # Python 依赖（精确锁定版本，避免解析爆炸，见「常见问题 15」）
│   ├── Dockerfile           # 生产镜像（非 root + 健康检查 + 内置 ffmpeg）
│   ├── .dockerignore
│   └── static/
│       ├── index.html       # Vue 3 前端（Vue 已本地化，无需构建；样式内联，见「界面设计」）
│       └── vendor/          # vue.global.prod.js（Vue 3 运行时）+ qrcode.js（本地渲染登录二维码）
├── deploy/
│   ├── preflight.sh         # 部署前环境自检（只读，不修改系统）
│   ├── deploy.sh            # 一键部署（含 --status / --logs）
│   ├── update.sh            # 增量更新（已部署过的服务器用这个）
│   ├── fix-docker-mirror.sh # 自动挑选可用的镜像加速源
│   ├── check-progress.sh    # 构建卡住时判断「正在下载」还是「真卡死」
│   ├── nginx.conf / nginx-https.conf   # 反代（HTTP 版 / HTTPS 版模板）
│   ├── douyin-downloader.service       # 裸机部署的 systemd 服务
│   └── certs/               # HTTPS 证书存放目录（gitignore）
├── run-local.sh             # 本地启动脚本（自动挑 Python 与 ffmpeg，规避 WinGet 坏 shim）
├── .env.example             # 环境变量模板
├── docker-compose.yml       # 容器编排（web + 可选 nginx）
└── README.md
```

## 快速开始

### 方式一：本地运行

**最省事：用自带的启动脚本**（自动挑 Python 与 ffmpeg，见下方说明）

```bash
bash run-local.sh
# 换端口：PORT=9000 bash run-local.sh
```

脚本做三件事，都是本地开发最容易卡住的地方：挑一个真能 `import fastapi, uvicorn, httpx` 的 Python（按 `.venv/` → 托管环境 → 系统 Python 顺序）、挑一个**真能执行**的 ffmpeg（原因见下）、起 `uvicorn`。

**手动跑也可以**（等价做法）：

```bash
pip install -r backend/requirements.txt
cd backend
uvicorn main:app --host 0.0.0.0 --port 8000
# 浏览器打开 http://127.0.0.1:8000
```

> **可选：装 ffmpeg 以支持 DASH 高清档位**（1080P60 / 4K / HDR）。
> 不装也能正常用，只是这些档位不会出现在列表里（MP4 档位不受影响）。
>
> ```bash
> # Debian/Ubuntu
> apt-get install -y ffmpeg
> # macOS
> brew install ffmpeg
> ```
>
> 装在非标准路径时用 `FFMPEG_BIN=/path/to/ffmpeg` 指定即可。

#### ⚠️ 本地跑最容易踩的坑：ffmpeg 的「假可用」

Windows 上用 **WinGet** 装的 ffmpeg，`PATH` 里那个 `ffmpeg.exe` 是个 **0 字节的 reparse point（shim）**，真身在 `WinGet\Packages\Gyan.FFmpeg_*\...\bin\` 下。它看起来一切正常，**直到用户点了高清档位才失败**：

| 检查方式 | 结果 |
|---|---|
| `Path.exists()` / `shutil.which()` | ✅ 通过 |
| Git Bash 直接执行 | ✅ 通过（bash 会跟随 reparse point） |
| **Python `subprocess` 执行** | ❌ `[WinError 193]`——**这才是应用实际走的路径** |

最后一行才是重点：**bash 能跑不代表应用能跑**。所以 `run-local.sh` 用应用真正使用的解释器去验证 ffmpeg，而不是用 bash 的 `-version`——否则会把坏路径当成可用的选出来。`bilibili.py` 的 `ffmpeg_path()` 同理，详见「DASH 高清档位与 ffmpeg」。

想用上 ffmpeg，在 `.env` 里指向真身即可：

```ini
FFMPEG_BIN=C:/Users/你/AppData/Local/Microsoft/WinGet/Packages/Gyan.FFmpeg_xxx/ffmpeg-9.0.2-full_build/bin/ffmpeg.exe
```

### 方式二：Docker 一键部署

```bash
docker compose up -d
# 浏览器打开 http://服务器IP:8000
```

## 上线部署（部署到自己的服务器）

### 部署架构

```
用户浏览器 ──HTTPS──▶ Nginx 反向代理 ──▶ FastAPI 应用容器 ──▶ 解析引擎
                      (443 → 8000)        (解析 + 流式下载)      (自托管 / 公共实例)
```

### 前置条件

| 项   | 要求                                                          |
| --- | ----------------------------------------------------------- |
| 服务器 | 1 核 2G 起步，推荐 2 核 4G（下载走代理会消耗带宽和内存）                          |
| 系统  | Ubuntu 20.04+ / Debian 11+ / CentOS 7+ 均可                   |
| 网络  | **必须能出网访问 `douyin.com` 与 `bilibili.com`**（见下方「常见问题」第 1、2 条） |
| ffmpeg | 可选。**装了就支持 1080P60 / 4K / HDR 等 DASH 高清档位**（服务端合并音视频）；不装则自动隐藏这些档位，其余功能不受影响。Docker 镜像已内置 |
| 域名  | 可选，但强烈建议，用于 HTTPS 和正式访问地址                                   |

### 第 1 步：把代码传到服务器

```bash
# 方式一：本地打包上传（在你自己电脑上执行）
scp -r douyin-downloader user@你的服务器IP:/opt/

# 方式二：先推到 Git 仓库再在服务器上拉取
git clone <你的仓库地址> /opt/douyin-downloader
```

### 第 2 步：一键启动

```bash
cd /opt/douyin-downloader

# 装 Docker（如已安装可跳过）
curl -fsSL https://get.docker.com | sh
sudo systemctl enable --now docker

# 生成配置
cp .env.example .env

# 启动（不带 Nginx，直接暴露 8000 端口，适合先验证）
bash deploy/deploy.sh

# 或：启动并启用 Nginx 反向代理（推荐，用于配域名和 HTTPS）
bash deploy/deploy.sh --proxy
```

脚本会自动：检查 Docker → 生成 `.env` → 构建镜像 → 启动服务 → 等待健康检查通过 → 打印访问地址。之后浏览器打开 `http://服务器IP:8000`（用了 `--proxy` 则是 `http://服务器IP`）就能用。

其他常用命令：

```bash
bash deploy/deploy.sh --status   # 查看运行状态 + 健康检查
bash deploy/deploy.sh --logs     # 跟踪日志
docker compose down              # 停止服务
docker compose up -d             # 启动服务
```

### 第 3 步：自托管解析引擎（生产环境建议做）

默认走公共实例 `api.douyin.wtf` 的 demo 账号，**有频率限制**——请求稍快就会返回「解析请求过于频繁」。自用够，公开访问一定要换掉。

用上游 [Douyin\_TikTok\_Download\_API](https://github.com/Evil0ctal/Douyin_TikTok_Download_API) 的官方脚本一键部署即可：

```bash
curl -fsSL https://raw.githubusercontent.com/Evil0ctal/Douyin_TikTok_Download_API/main/install/install.sh -o install.sh
less install.sh        # 建议先看一眼脚本内容
bash install.sh
```

上游 v5 默认监听 **8000** 端口。启动后用日志里的 setup token 创建第一个管理员账号，再把账号密码填进本项目的 `.env`：

```bash
# /opt/douyin-downloader/.env
DTK_BASE_URL=http://127.0.0.1:8000    # 上游引擎地址（同机部署就是本机端口）
DTK_USERNAME=你的管理员账号
DTK_PASSWORD=你的管理员密码

docker compose up -d                  # 改完重启本项目
```

> 端口冲突就改上游的端口映射，`DTK_BASE_URL` 跟着改。不想用脚本的话，按上游仓库说明克隆后用 `docker/compose.yml` 手动部署也行。

### 第 4 步：绑定域名 + 开启 HTTPS

```bash
# 1) 把域名的 A 记录解析到服务器 IP（在域名服务商后台操作）
# 2) 修改 Nginx 配置里的 server_name，把 _ 换成你的域名
vim deploy/nginx.conf

# 3) 查出 certbot 用的 webroot 卷名（Compose 会给卷名加上项目目录名前缀）
WEBROOT=$(docker volume ls --format '{{.Name}}' | grep -E 'certbot-webroot$' | head -1)

# 4) 申请证书（把 your-domain.com / 邮箱 换成你的）
docker run --rm \
  -v "$PWD/deploy/certs:/etc/letsencrypt" \
  -v "$WEBROOT:/var/www/certbot" \
  certbot/certbot certonly --webroot -w /var/www/certbot \
  -d your-domain.com --email you@example.com --agree-tos --no-eff-email

# 5) 启用 HTTPS 配置
cp deploy/nginx-https.conf deploy/nginx.conf
docker compose exec nginx nginx -s reload
```

证书 90 天到期。把下面几行存成 `deploy/renew-cert.sh`，再挂到 cron（每月 1 号凌晨）：

```bash
#!/bin/bash
cd /opt/douyin-downloader
W=$(docker volume ls --format '{{.Name}}' | grep certbot-webroot$ | head -1)
docker run --rm -v "$PWD/deploy/certs:/etc/letsencrypt" -v "$W:/var/www/certbot" \
  certbot/certbot renew --webroot -w /var/www/certbot --quiet
docker compose exec nginx nginx -s reload
```

```bash
# crontab -e
0 3 1 * * bash /opt/douyin-downloader/deploy/renew-cert.sh
```

> 脚本里的 `cd` 不能省：cron 的工作目录是家目录，不切过去会挂载到错误的证书路径、也找不到 `docker-compose.yml`。
>
> 顺带一提：输入框旁的「粘贴」按钮只在 HTTPS（或 localhost）下出现，见 FAQ 24。

### 第 5 步：不用 Docker 的部署方式（裸机）

```bash
# 系统依赖 + 虚拟环境
sudo apt install -y python3-venv nginx
cd /opt/douyin-downloader/backend
python3 -m venv ../.venv
../.venv/bin/pip install -r requirements.txt

# 注册 systemd 服务（按需修改文件里的用户和路径）
sudo cp ../deploy/douyin-downloader.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now douyin-downloader
sudo systemctl status douyin-downloader
```

再按第 4 步配置 Nginx 反向代理即可（注意把 `upstream` 里的 `web:8000` 改成 `127.0.0.1:8000`，因为不再有 Docker 网络）。

### 第 6 步：日常更新（已经部署过的服务器）

使用带日期的更新包，完整操作与回滚步骤见 [UPDATE.md](UPDATE.md)。将包和校验文件上传到独立临时目录，解压后运行安装脚本：

```bash
cd /tmp/douyin-update-20261008
sha256sum -c douyin-downloader-update-20261008-r2.tar.gz.sha256
tar -xzf douyin-downloader-update-20261008-r2.tar.gz
cd douyin-downloader-update-20261008-r2
sudo bash deploy/apply-update.sh /opt/douyin-downloader  # 改成旧项目实际路径
```

脚本先备份代码、配置与运行镜像，再更新后端和前端。服务器原有 `.env`、Compose、Nginx 配置和证书均保留；只替换 `web` 容器，并在需要时重载项目自带 Nginx。构建失败时旧容器继续运行；新版启动检查失败时尝试恢复旧镜像，代码回滚见 UPDATE.md。

需要重试构建时，在服务器原项目目录执行：

```bash
bash deploy/update.sh
bash deploy/update.sh --clean  # 必要时忽略构建缓存
```

生成更新包：在开发机项目目录运行 `python deploy/build-update.py`。更新包是源码包，Docker 构建仍需下载镜像和依赖。

### 常见问题

1. **构建时报 `load metadata ... i/o timeout` / `DeadlineExceeded`？**
   服务器连不上 Docker Hub（国内服务器最常见）。跑本项目自带的加速脚本，它会逐个测试候选源、只写入实测可用的：
   ```bash
   sudo bash deploy/fix-docker-mirror.sh
   sudo bash deploy/deploy.sh --proxy
   ```
   所有加速源都不可用时，改用兜底通道（绕开 Docker Hub，直接从国内镜像源拉基础镜像）——在 `.env` 里取消注释：
   ```ini
   BASE_IMAGE=docker.m.daocloud.io/library/python:3.12-slim
   NGINX_IMAGE=docker.m.daocloud.io/library/nginx:1.27-alpine
   PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple
   ```
   再不行就走第 5 步的裸机部署（不用 Docker，自然没有镜像拉取问题）。

2. **服务器连不上抖音？** 解析完全依赖服务器能访问 `douyin.com`。部分海外机房会被地域限制，国内机房 IP 也可能触发风控。判断方法：在服务器上执行 `curl -I https://www.douyin.com/`，返回 200/302 即为正常；超时或被拒就要换机房或走代理出口。

3. **哔哩哔哩只能下到 720P？（问得最多的一条）** 未登录时 durl 上限就是 720P，不是 bug。解锁方式两种，**推荐第一种**：
   - **访客扫码登录**：解析后点结果卡片里的「扫码登录」，用哔哩哔哩 App 扫码即可。每个访客登自己的号、互不影响，凭据只存内存不落盘；
   - **服务器统一账号**：把浏览器登录后的 Cookie 填进 `.env`，所有访客共用这个账号：
     ```ini
     BILI_COOKIE=SESSDATA=你在浏览器里拿到的值
     ```
     然后 `docker compose up -d` 重启。Cookie 等同账号凭据，别外泄、别提交到 Git（另见第 13 条）。

   > 普通登录账号也可获取标准 1080P。MP4 接口降到 720P 不代表账号上限，DASH 可能仍提供更高清流。看不到高清档位时，先用画质诊断检查实际流，再确认 ffmpeg 可运行、账号有对应权益、片源提供该档位。完整说明见「画质与账号权益」。

   扫码登录后画质没变化的话，点结果卡片里的「**画质诊断**」按钮——它会逐档探测，直接告诉你凭据是否生效、账号是不是大会员、视频本身上限是多少。

4. **B 站多分P视频只下到第一P？** 解析结果里会出现选集按钮（`P1 / P2 / …`），点一下即可切换；也可以直接在链接里带 `?p=3`。

5. **解析提示「请求过于频繁」？** 说明在共用公共 demo 实例，按第 3 步自托管即可解决。哔哩哔哩走官方接口，不受此限。

6. **下载大视频卡住 / 服务器内存暴涨？** 检查 Nginx 是否漏了 `proxy_buffering off`（本项目自带配置已加好）。

7. **下载很慢或流量费很高？** 视频经服务器中转，4K 单条可达 24MB。按流量计费的云服务器要注意成本，或改用按带宽计费。

8. **视频下载到一半失败？** 两个平台的直链都有时效性（通常几小时），重新解析一次即可。

9. **下载接口返回 400「直链域名 … 不在下载白名单内」？** B 站会把部分直链下发到**第三方 PCDN 边缘节点**，这类域名默认刻意不放通——`/api/download` 是开放接口，白名单太松会被当成免费代理滥用。代码已在解析阶段用 `prefer_official_cdn` 把官方 CDN 排到第一位，所以**重新解析**一次通常就好。若平台真换了官方 CDN 域名，往 `.env` 的 `EXTRA_BILIBILI_HOSTS` 或 `EXTRA_DOUYIN_HOSTS` 追加完整域名即可，报错信息里会直接告诉你被拦的是哪个域名。

10. **域名能访问但页面打不开？** 云厂商**安全组**没放通 80/443，在控制台加规则即可。

11. **端口被占用？** 改 `.env` 里的 `APP_PORT`，或调整上游引擎的端口映射。

12. **扫码提示「二维码已失效」？** 二维码有效期 180 秒，过期后点弹窗里的「刷新二维码」重新生成即可。提示「不属于当前会话」说明页面刷新或换了浏览器，重新扫码即可。

13. **能不能让访客共用我的 B 站账号？** 不建议。本项目默认按访客会话隔离，A 扫码不会让 B 用 A 的账号。确实想统一就用 `.env` 里的 `BILI_COOKIE`（服务器级），但要清楚这等于把所有访客的下载都记在你的账号上。

14. **下载高清档位很慢 / 卡住？** DASH 档位要服务端先合并音视频，大文件需数十秒，按钮会变成「取回中…」。慢得异常时优先排查**出网方式**——代理与直连的差异极大（见「DASH 高清档位与 ffmpeg」）。

15. **更新时构建报 `ResolutionImpossible` / `Cannot install ... conflicting dependencies`？** 这是 **pip 装 Python 依赖失败，跟 ffmpeg 无关**（apt 那层已经装好了，别去折腾 ffmpeg）。

   **根因**：`fastapi` 过去写成无上界的 `>=0.110`，pip 从最新版一路向下试探（约 60 个版本），会向镜像站发出几百次元数据请求、耗时数分钟；期间只要 `pydantic` 的索引页被限流或超时，pip 就认定「pydantic 一个可用版本都没有」，于是把所有 `fastapi` 版本判为冲突——**报错指向 pydantic，真正的原因是解析爆炸**。

   **本项目已通过精确锁定版本修掉**（见 `backend/requirements.txt`，全部改成 `==`），正常不会再出现。仍然遇到就按顺序做：
   ```bash
   # ① 加一个备用源，主源抖动时自动兜底
   echo 'PIP_EXTRA_INDEX_URL=https://mirrors.aliyun.com/pypi/simple' >> .env
   bash deploy/update.sh

   # ② 还是不行？单独跑一次解析，几秒钟看清到底是哪个包缺了（不重建镜像）
   docker run --rm -v "$PWD/backend:/w:ro" python:3.12-slim \
     pip install --dry-run -i "$(grep -E '^PIP_INDEX_URL=' .env | cut -d= -f2)" -r /w/requirements.txt
   ```

16. **解析 B 站提示 `HTTP 412`（风控拦截）？** B 站**风控网关**拒绝了请求，不是接口挂了、也不是链接有问题。最常见的原因是**服务器出口 IP 被批量风控**（机房 IP 段是重灾区）。代码已自动做四层防护（设备指纹 / 浏览器请求头 / 请求节流 / 412 换指纹重试），正常情况下碰不到。**持续**出现时按顺序试：
   ```bash
   # ① 先确认指纹到底拿到没有（第一件要查的事）
   curl -s 'http://127.0.0.1:8000/api/bili/debug?url=https://www.bilibili.com/video/BV1GJ411x7h7' \
     | python3 -m json.tool | grep -A6 fingerprint

   # ② 指纹齐全（buvid3 / b_nut）却仍 412 → 基本可确定是 IP 被限，
   #    配一个登录态能显著提升请求信誉度（BILI_COOKIE 获取方式见第 3 条）
   echo 'BILI_COOKIE=SESSDATA=你的值' >> .env && bash deploy/update.sh

   # ③ 仍然不行 → 换服务器出口 IP，或给容器配一个 HTTP 代理出口
   ```
   也可以先调大节流间隔观察：`.env` 里设 `BILI_MIN_INTERVAL=0.6`（更保险但更慢），然后 `bash deploy/update.sh`。

17. **番剧（动漫）怎么下？** 直接把番剧链接粘进同一个输入框，无需切换模式：`bangumi/play/ep741735`（单集）、`ss44904`（整季）、`media/md20144639`（作品页），也支持裸编号 `ep741735` / `ss44904` / `md20144639`。

   解析后出现**选集网格**：正片按集数排列，PV / 花絮单独分组；付费集右上角有金色「会员」角标；长篇番剧（如柯南）可用下方的横滑条切换其他季。点某一集即重新解析该集——**画质只探当前这一集**，因为一季可能上百集，全探一遍会发出成百上千次请求、必然触发 412。

   - **会员集提示「本集暂不可下载」，但界面没报错？** 这是**有意设计**：B 站在未登录 / 非大会员时访问会员集，接口报成功、却只给一段约 3 分钟的试看片段。若直接放行，用户会下到「以为是正片、其实是试看」的文件，所以代码识别到试看后**拒绝列出任何档位**，只把下载区换成一句说明（标题 / 封面 / 选集照常展示，不做欺骗性展示）。登录**大会员**账号后重新解析即可下载完整正片，点「画质诊断」可看明细。识别原理见「番剧（PGC）」。
   - **免费集也只有 480P / 360P？** 与普通视频同理，受账号权益与视频本身上限限制。番剧的画质普遍低于普通投稿（很多老番正片源本身就是 480P）。

18. **抖音图文帖（plog）怎么下载？为什么有两个下载按钮？** 图文链接粘进同一个输入框，解析后卡片会变成**图片网格**，每张图下挂「原图」，整帖下有两个出口：
   - **打包下载全部（zip）**——图文帖常有 9~30 张图，逐张点太累。zip 里含全部无水印原图 + 一份**文案 Word**；
   - **下载成视频**——把整组图合成为一个幻灯片 MP4（每张停留 3 秒）。

   两种形式互不影响。图片取的是**无水印原图**。

19. **zip 里的 `文案.docx` 是什么？** 图文帖的文案，含**标题、作者、来源链接、互动数据、字数和完整正文**（按原换行分段），结尾带一行导出落款。

   为什么放 Word：图文帖的价值常常在文案上——尤其**长图文**动辄几千字，只存图片等于把内容丢掉一半；纯文本 docx 也便于二次编辑和分享。

   这份 docx 是**手写 OOXML** 生成的，没有引入 `python-docx`（原因见第 15 条：本项目依赖精确锁定，不为一个导出小功能新增依赖、再跟着重建镜像）。docx 本质就是 zip + 三个固定 XML 部件，够用，已实测 Word / WPS / python-docx 均可正常打开。

20. **「下载成视频」出来的视频没有声音？** 预期行为。抖音不给图文帖下发原始背景音乐（接口里 `music.play_url` 为 null，要登录态才有），所以服务端合成的是**无声幻灯片**，界面上也已标注。

21. **为什么图文帖的「下载成视频」按钮有时不显示？** 合成依赖服务端 ffmpeg。后端会在解析结果里回报 `server_ffmpeg`，前端据此**不显示**该按钮——不给你列点了必然失败的入口（与 DASH 档位同样的处理）。本机跑要指定真实 ffmpeg：`FFMPEG_BIN=/真实路径/ffmpeg bash run-local.sh`，详见「本地跑最容易踩的坑」。此时打包下载图片仍然可用。

22. **抖音的纯文字帖 / 长图文（文章）能解析吗？** 都能。纯文字帖以 `kind="text"` 返回，卡片直接展示**完整正文**（没有媒体可下，所以没有下载按钮）；长图文的**完整正文就在 `desc` 字段里**，卡片上会给一个**「全文（N 字）」折叠块**（正文 ≥ 200 字才出现，默认收起）。详见「长图文（「文章」）：正文就在 `desc` 里」。

23. **解析图文帖时提示「分享页暂时只返回了空壳」？** 抖音分享页的响应是非确定的，第一次常常只给布局壳。代码已内置按模板 + 轮次重试，偶发失败时再点一次解析即可。

24. **输入框旁边的「粘贴」按钮不见了？** 这是**按能力降级**，不是坏了。`navigator.clipboard.readText()` 只在**安全上下文**下存在——用 `http://IP:端口` 这种**纯 HTTP** 方式访问时，`navigator.clipboard` 直接是 `undefined`。

   所以前端在页面加载时先探测一次能力，读不到就**不渲染这个按钮**（摆一个点了必然报错的按钮比不摆更糟）。此时手动粘贴（Ctrl / ⌘ + V）照旧可用，其余功能完全不受影响。

   要让它出现，二选一：走 **HTTPS**（见「第 4 步」），或用 `http://localhost:8000` / `http://127.0.0.1:8000` 访问（localhost 也算安全上下文）。

   另外两种「按钮在、但点了没填上」的情况也都有提示，不会静默失败：剪贴板是空的会提示「剪贴板里没有文字」；浏览器拒绝授权（或页面失焦）会提示手动粘贴。

### 排障速查

部署卡住时，先跑一遍自检拿到底层状态：

```bash
bash deploy/preflight.sh          # 环境 + 网络 + 端口 体检
bash deploy/deploy.sh --status    # 容器状态 + 健康检查
bash deploy/deploy.sh --logs      # 跟踪日志
docker compose ps                 # 容器列表
docker compose logs web --tail=50 # 只看应用日志
```

## 接口说明

| 接口                             | 方法   | 说明                                                                                                                                                                                          |
| ------------------------------ | ---- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `/api/parse?url=&p=&ep=`       | GET  | 解析分享链接（**自动识别平台与内容类型**，无需前端指定），返回 `platform`、`kind`、`title`、`summary`、`cover`、`author`、`duration_text`、`qualities[]`（label / note / size_text / codec / urls / format / needs_merge / qn）、`server_ffmpeg`（服务端有无 ffmpeg）。B 站多分P时另返回 `pages[]` 与 `current_page`（`p` 指定分P）；**番剧**返回 `kind="bangumi"` + `episodes[]`（含 `locked` 会员角标）/ `ep_id` / `season_id` / `seasons[]` / `unavailable`（`ep` 指定集号）；**抖音图文**返回 `kind="images"` + `images[]`（每张含 `url` / `urls` / `ext` / `width` / `height`） |
| `/api/download?url=&filename=` | GET  | 流式代理下载视频 / **图片**直链，触发浏览器保存                                                                                                    |
| `/api/images/zip`              | POST | **把图文帖的图片打包成一个 zip** 下载。zip 内含全部无水印原图（`01.jpg`、`02.jpg`…）+ 一份**文案 Word**（`文案.docx`）。JSON 体：`{images:[{url,urls,width,height,ext}], filename, title, author, desc, video_id, platform, stats_text}`                                                                    |
| `/api/slideshow`               | POST | **把图文帖的图片合成为一个幻灯片视频**（服务端 ffmpeg）。JSON 体同上 + `per_image_sec`（默认 3）。未装 ffmpeg 返回 503                                                                            |
| `/api/dash?bvid=&cid=&qn=&codecid=&filename=&ep_id=` | GET  | **下载 DASH 高清档位**：服务端拉取音视频流并用 ffmpeg 无损合并（`-c copy`）成完整 MP4 后返回。未装 ffmpeg 时返回 503。普通视频传 `bvid`，**番剧传 `ep_id`**（二选一，都缺则 400）                                                                                            |
| `/api/cover?url=`              | GET  | 代理封面图（绕过图床 Referer 限制 / 修正 http 封面）                                                                                                                                                         |
| `/api/bili/login`              | GET  | 查询当前会话的 B 站登录状态，返回 `logged_in` / `source`（`session` 访客扫码 / `env` 服务器账号 / `none`）/ `user` / `can_logout`                                                                                     |
| `/api/bili/login/qr`           | POST | 申请登录二维码，返回 `qrcode_key` 与待编码 `url`（前端自行渲染 SVG）                                                                                                                                              |
| `/api/bili/login/qr/poll?key=` | GET  | 轮询扫码状态，返回 `waiting` / `scanned` / `expired` / `success`；成功即把凭据写入本会话。非本会话的 key 返回 409 `stale`                                                                                                |
| `/api/bili/logout`             | POST | 退出登录，**只清当前会话**的凭据，不影响其他访客                                                                                                                                                                  |
| `/api/bili/debug?url=&p=&ep=`  | GET  | **画质 / 权限诊断**：逐档探测 MP4 并额外问一次 DASH，回报「凭据是否生效 / 是否大会员 / 视频本身上限」，并给出人话结论。番剧链接会自动切换为番剧诊断，额外回报 `is_preview` 与「返回时长 vs 官方时长」以识别试看片段。**不含凭据明文**，可安全贴出排查                                    |

`platform` 取值 `douyin` 或 `bilibili`，前端据此显示来源标记。`/api/parse` 的响应还带 `logged_in`，供前端同步登录态显示。

`kind` 表示**内容类型**，前端据此切换渲染方式：

| `kind` | 含义 | 前端表现 |
| --- | --- | --- |
| （无）/ `video` | 普通视频 | 清晰度档位列表 |
| `bangumi` | B 站番剧 | 剧集网格 + 季切换（替代分P） |
| `images` | 抖音图文帖（plog） | 图片网格 + 原图 / 打包 / 合成视频 |
| `text` | 抖音纯文字帖 | 完整正文（无媒体可下） |

**图文帖解析示例**（`GET /api/parse?url=https://www.douyin.com/note/7469411074119322899`）：

```jsonc
{
  "ok": true, "platform": "douyin", "kind": "images",
  "video_id": "7469411074119322899", "title": "不老实的Mortis.#mygo …",
  "author": "kamada", "duration_text": "",
  "qualities": [],
  "server_ffmpeg": true,
  "images": [
    { "index": 1, "ext": "jpeg", "width": 960, "height": 1353,
      "url": "https://p3-pc-sign.douyinpic.com/….jpeg?…",
      "urls": [ "….jpeg?…", "….webp?…" ] }
  ],
  "stats": { "digg_count": 3056, "collect_count": 362, "comment_count": 136, "share_count": 1338 }
}
```

> `duration_text` 为空表示"本内容没有时长"，前端会整块隐藏（而不是显示 `--:--`）。
> `server_ffmpeg` 为 false 时前端不显示「下载成视频」按钮——与 DASH 档位同理，**不列点了必然失败的入口**。

**番剧解析示例**（`GET /api/parse?url=https://www.bilibili.com/bangumi/play/ep741736`）：

```jsonc
{
  "ok": true, "platform": "bilibili", "kind": "bangumi",
  "video_id": "ep741736", "ep_id": 741736, "cid": 1085034158, "season_id": 44904,
  "title": "名侦探柯南：犯人犯泽先生（中配）",
  "episode_title": "第2话 相遇便是缘",
  "current_page": 2, "total_pages": 13,
  "episodes": [ /* 每项：ep_id / cid / part / short / duration_text / locked / section */ ],
  "seasons":  [ /* 同作品其他季，前端渲染成横滑切换条 */ ],
  "qualities": [],
  "unavailable": {
    "reason": "vip",
    "message": "本集为大会员专享，当前账号只能获取试看片段，因此不提供下载。登录大会员账号后可下载完整正片。"
  }
}
```

> `unavailable` 非空时 `qualities` 必定为空。前端会照常展示标题 / 封面 / 选集，只把「选择清晰度下载」区块换成一条说明——**不做欺骗性展示**（不列档位、更不给试看片段）。

B 站登录相关接口全部通过 HttpOnly Cookie（`vd_sid`）识别会话，凭据只存服务端内存。

完整交互式文档：`http://127.0.0.1:8000/docs`（FastAPI 自带 Swagger UI）。

## 解析原理

### 抖音（双引擎）

**第 0 步 — 链接规范化（关键前置）**：用户粘贴的内容先做清洗与还原，再交给解析引擎：

1. **从任意文本中提取链接** —— 抖音「复制链接」实际复制出的常是「口令 + 链接 + 提示语」的整段文字，服务端用正则把其中的 URL 抽出来；
2. **短链还原** —— `v.douyin.com/xxxx/` 跟随 301/302 重定向，取出跳转目标里的视频 ID，统一转成 `www.douyin.com/video/{id}` 规范长链。

> 这一步是必需的：解析引擎只接受规范长链，直接传短链会返回 `400 INVALID_PARAM (missing content_id)`。  
> 支持形态：短链、`/video/{id}`、`/note/{id}`、`?modal_id={id}`、`?object_id={id}`、无协议裸链接、整段分享文字。

**引擎 A（主）— Evil0ctal API**：调用 [Douyin\_TikTok\_Download\_API](https://github.com/Evil0ctal/Douyin_TikTok_Download_API) 的 API 实例（默认公共实例 `api.douyin.wtf`，demo 账号有限流）。原因：2025 年后抖音分享页不再内嵌视频数据（`_ROUTER_DATA` 里 `videoInfoRes` 恒为空），官方 detail 接口又需要 `a_bogus` 签名，直接调用该项目 API 是最稳的路径。

**引擎 B（备用）— 本地分享页解析**：短链 301 → iesdouyin 分享页 → 提取 `window._ROUTER_DATA` → `item_list[0]`。若抖音未来恢复 SSR 数据可独立工作；引擎 A 失败时自动降级。

两个引擎都取平台发布的干净视频流（540P ~ 4K 多码率），不做二次转码，画质无损。

#### 内容类型：视频 / 图文帖 / 纯文字帖

早先只认视频——**没有视频流就一律报「未获取到可用的视频流地址」**，所以图文帖（plog）和纯文字帖都解析不了。现在两个引擎都用同一条判据，各自带上 `kind`：

| 内容 | 判据 | `kind` |
| --- | --- | --- |
| 视频 | 有可下载的视频流（`bit_rate` / `streams`） | `video`（默认） |
| 图文帖 | 有 `images[]` **且**没有可用视频流 | `images` |
| 纯文字帖 | 既无视频流也无图片，但有文案 | `text` |

> **为什么是「有图 **且** 没有视频流」，而不是「有图就算图文」**：图文帖里同样带一个 `video` 对象，它是**占位符**——`play_addr.uri` 指向 `images_no_sound_volume_audio_file.mp3`（一段空音频），`duration` 为 0、`bit_rate` 为空。这类占位地址不会产生任何档位（见 `_FAKE_PLAY_URI`），所以「没有可用视频流」这一条就足以认出图文帖。反过来，若只看「有没有图」，**带 `images` 的视频帖会被误判成图文**。

##### ⚠️ 图片地址有两套，字段名会骗人

抖音图文帖的每张图同时下发两套地址，**实测结论与字段名给人的直觉正好相反**：

| 字段                        | 实测                                |
| ------------------------- | --------------------------------- |
| `url_list` / `url`        | **干净原图** ← 本项目只取这一套               |
| `download_url_list`       | 模板名带 `-water`，图上压 **「抖音号 xxxxx」水印** |

所以 `_image_entry` 明确只接受干净的那一套。另外会把 **JPEG 排到 webp 前面**（同尺寸同质量，格式更通用），下载文件名序号补零，按文件名排序即帖内顺序。

##### 引擎 B 的分享页响应是「非确定」的

同一个分享页 URL，**第一次常常只返回「布局壳」**（`loaderData` 里全是 `*_layout: null`，没有详情节点）。早期实现只要 HTML 里出现 `_ROUTER_DATA` 就当作成功，于是拿到壳继续解析、必然报错，表现为「偶发解析失败」。现在按模板 + 轮次重试直到真正解析出作品；`_ShareSkeleton` 专门标记这种「值得重试」的失败，与「作品已删除」这类重试也无用的失败区分开。

另外，`loaderData` 的**第一个键是布局节点且恒为 `null`**（视频页是 `video_layout`，图文页是 `note_layout`），数据在 `*_(id)/page` 里。早期用 `loader[next(iter(loader))]` 盲取第一个键，恰好取到那个 null —— 这是「图文帖一律报解析失败」的直接原因。

##### 长图文（「文章」）：正文就在 `desc` 里

抖音 2025-12 上线了长图文（最多 8000 字 + 30 图）。实测它的**完整正文直接放在 `desc`**，
没有单独的 article / chapter 字段（`chapter_list`、`video_text` 等均为 `null`）。
实测样本：一条 2498 字的随笔，`desc` 长度 2498、结尾带作者署名，是全文而非截断。
短链会重定向到 `/video/{id}` 路径（不是 `/note/`），但内容仍按图文帖处理。

前端的 `summary` 固定截到 100 字，所以卡片额外给一个**「全文（N 字）」折叠块**：
正文 ≥ 200 字才出现（更短的截掉部分很少，不值得多一个按钮），默认收起，点「展开全文」读完整篇。

##### 「下载成视频」是服务端自己合成的

抖音**不给图文帖提供可用的视频**（引擎 A 的 `media.video` 为 null；引擎 B 的 `play_addr` 指向空音频占位符），所以「下载成视频」由服务端用 ffmpeg 合成幻灯片：

1. 下载原图 → 2. 按所有图的**最大宽高**建统一画布（上限 1080×1920，宽高取偶）→ 3. 等比缩放 + 居中补白（`force_original_aspect_ratio=decrease` + `pad`，混排的横竖图才能被 concat 解复用器当成同一条流）→ 4. 输出 H.264 MP4。

合成产物是**无声**的：原始 BGM 拿不到（`music.play_url` 为 null，需登录态才下发），前端会明确标注。

> **一个反直觉的实测**：concat 清单末尾**不要**再重复最后一个文件。网上常见写法是「末尾把最后一张再写一遍」（那是针对**视频**输入的惯用法），但对**图片**输入实测相反——重复会实打实多播一张的时长（3 张 × 2s 期望 6.00s，重复后变 7.97s）。见 `_write_concat_list`。
>
> 合成用的 ffmpeg 与 B 站 DASH 合并是同一套（`ffmpeg_tool.py`），包括那套「真实执行探测」的坑。

### 哔哩哔哩（官方接口）

统一入口是 `bilibili.parse()`，它先判断这是**普通投稿（UGC）**还是**番剧（PGC）**，再分别走两条接口链。对外的数据结构保持一致（前端复用同一张卡片），番剧多一个 `kind="bangumi"` 字段用于切换选集 UI。

> 之所以把「怎么区分」放在 `bilibili.py` 而不是 `main.py`：前端实时提示、后端真正路由若有各自的判定规则，迟早会不一致（历史上就吃过这个亏）。`main.py` 只需判断到「B 站」这一层。

#### 普通投稿（UGC）

全部走 B 站官方 web 接口，**不需要 WBI 签名、不需要登录**：

1. **链接规范化** —— `b23.tv` 短链跟随 301/302 拿到含 BV 号的长链；`av` 号通过接口换成 BV 号。
   > 注意：b23.tv 对**无效短码**返回的是 **HTTP 200 + `{"code":-404}`**，不是 302。代码对两种情况都做了处理。
   > 另外，**番剧短链跳转到的是 `/bangumi/play/ep…`，里面没有 BV 号**。早期版本只认 BV/av 就会一路跟到底、最后报「短链已失效」，所以 `_resolve_short` 必须把番剧路径也算作「跟到了目的地」。
2. **元信息** —— `x/web-interface/view`，拿到标题 / 简介 / 封面（`http` 链接会改写成 `https`）/ UP 主 / 时长 / 分P列表。
3. **播放地址（双格式，取长补短）** —— 同时问两种格式，再合并结果：
   - **MP4（`fnval=1`，返回 `durl`）** —— 一个**自带音轨的完整 MP4**，正好和抖音「一个清晰度一个成品文件」的体验对齐。实测 51 分钟 / 655MB 的视频仍是单段，直接整体下载，**不需要 ffmpeg 合并**。
   - **DASH（`fnval=4048`）** —— 音视频分离的流。它的价值在于**能拿到 MP4 拿不到的高阶档位**（1080P60 / 4K / HDR 只在 DASH 下提供）。
4. **画质枚举** —— MP4 逐档请求，DASH 一次请求返回全部档位；**只保留接口真实返回了该档位的那些**。
5. **直链排序** —— B 站有时把 `url` 指到**第三方 PCDN 边缘节点**，官方 CDN 只放在 `backup_url` 里。而前端只取列表第一个地址去请求 `/api/download`，那个接口有域名白名单（防开放代理），PCDN 域名不在其中就会 400。所以解析时用 `prefer_official_cdn` 把 `upos-*.bilivideo.com` 这类官方地址排到最前，其余地址保留在末尾作为服务端合并下载的兜底。

#### 番剧（PGC）

番剧走的是**另一套接口**，与 UGC 有几处关键差异，改代码前务必留意：

| 差异点 | UGC | 番剧（PGC） |
| --- | --- | --- |
| 数据节点 | 顶层 `data` | 顶层 **`result`**（照抄 `body["data"]` 会拿到 `None`，表现成「接口报成功却什么都没解析出来」） |
| 标识体系 | `bvid` / `aid` + `cid` | `ep_id`（单集）/ `season_id`（整季）/ `media_id`（作品）+ `cid` |
| 元信息接口 | `x/web-interface/view` | `pgc/view/web/season`（**只认 `ep_id` / `season_id`**） |
| 播放接口 | `x/player/playurl` | `pgc/player/web/playurl` |
| Referer | `/video/BV…/` | `/bangumi/play/ep…` |
| 会员限制的表现 | 拿不到更高档位 | **静默只给 3 分钟试看片段**（见下） |
| DASH 时长单位 | 与官方一致 | `dash.duration` 是**秒**，而 `episode.duration` 是毫秒 |

三个入口的解析路径：

```
ep741735  → pgc/view/web/season?ep_id=      → 已有集号，直接查
ss44904   → pgc/view/web/season?season_id=  → 整季
md20144639→ pgc/view/web/media?media_id=    → 换成 season_id → 再查 season
```

> `md` 入口最容易卡住：`pgc/view/web/media` 返回的是**整部作品**的元信息（标题、封面、评分、以及 `seasons` 全部季），**但它不含任何剧集**——看到 `media` 里没有 `episodes` 会误以为接口有问题。剧集必须再查一次 `season`。另外，给 `season` 接口传 `media_id` 会直接 **-404**。

**⚠️ 会员集的失败方式是「静默」的——这是整个番剧功能最关键的一点。**

未登录（或非大会员）访问会员集时，`code` 依然是 `0`，HTTP 也是 `200`，但 `durl` 指向的其实是一段约 3 分钟的试看。实测数据：

| 集 | `is_preview` | 官方时长 | 接口返回时长 | durl 体积 |
| --- | --- | --- | --- | --- |
| 免费集（正片） | `0` | 546s | 545621 ms | 23.5 MB |
| 会员集（试看） | `1` | 545s | **180137 ms** | 18.7 MB |

不专门识别，用户就会下到一个「以为是正片、其实是试看」的文件——这直接违背本项目「**只列真实可下的档位，不做欺骗性展示**」的原则。所以 `_is_preview_payload` 用**两条独立证据**判定，任一成立即认定是试看：

1. `is_preview == 1`（接口自己承认）—— 最直接；
2. 返回时长明显短于官方标称时长（**短于 90%**）—— 兜底，防止接口哪天不填 `is_preview`。

> 第 2 条用 90% 而不是「严格相等」：编码封装会让时长有零点几秒的偏差（545621 vs 546000 就是这种情况），卡死在相等会误判正常视频。

命中试看后抛 `ContentLocked`（`ParseError` 的子类）。**它不算解析失败**：标题、封面、剧集列表都是好的，只是当前账号下不到完整片子。所以 `parse_bangumi` 把它转成「卡片正常展示 + 明确告知为什么不能下」（`unavailable` 字段），而不是弹错误框把结果全部丢掉。

**会员集的判定线索**（用于选集网格上的角标）：

- `badge` 含「会员」二字，或 `status != 2`（实测 `2` = 免费可看，`13` = 付费专享）；
- 但 `locked` 只是**参考信息，不是判决**——用户如果登录了大会员，这一集其实是能下的。真正的可用性一律由**带凭据的 playurl 实测**决定，所以角标存在时按钮不禁用。

**画质只探当前这一集**：一季可能几十上百集，把每一集的画质都探一遍会发出成百上千次请求，必然触发风控。所以服务端只探当前集，切集由前端重新发一次解析（带 `&ep=<ep_id>`）。

**PV / 花絮必须单独分组**：`season.episodes` 只含正片，PV 和花絮在 `season.section[]` 里，且**编号与正片各自独立**——混成一条列表会出现两个「第1话」。前端用 `ep.section` 渲染分组标题。

**合并策略（`merge_formats`）**：同一档位优先用 MP4（自带音轨，少一次服务端合并）；DASH 只用来**补齐 MP4 拿不到的档位**，且要求该档位**高于 MP4 的最高档**——否则会出现「720P 已有 MP4，却还列一个需要合并的 480P」这种荒唐选项。这样普通视频（MP4 已覆盖全部档位）完全不引入合并开销，只有真正需要高阶画质时才走 ffmpeg。

> **关于画质上限**：B 站未登录时 durl 最高 720P。接口返回的 `support_formats` / `accept_quality` 会列出**「理论上支持」**的档位（含 1080P+），但那是账号权益层面的能力，未必能拿到——如果照着它列清单，用户点了 1080P 实际下载到 720P，等于欺骗。所以这里按真实返回的 `quality` 出清单，拿不到的不列。想解锁更高画质，让访客**扫码登录**（见下节），也可配 `BILI_COOKIE` 作为服务器级兜底。番剧同理：会员集未登录时不列任何档位，并明确说明原因。

### 风控（HTTP 412）与四层防护

B 站 `api.bilibili.com` 前面有一层风控网关，它不看业务参数，只看**这个请求像不像真实浏览器发出来的**。命中就直接返回 `HTTP 412 Precondition Failed`，连 JSON 都不给——界面上表现为「哔哩哔哩接口返回 HTTP 412」。

实测触发条件按影响从大到小：

| 触发条件 | 说明 |
| --- | --- |
| **请求不带任何 Cookie** | 最致命。同一台机器同一接口，不带 `buvid3` 必 412，带上就正常。未登录用户天然没有 Cookie，所以「裸请求」是最常被拦的形态 |
| 请求头残缺 | 只有 `UA + Referer` 会被判成脚本。真实 Chrome 还会带 `Origin` / `Accept` / `sec-ch-ua` / `sec-fetch-*` |
| 零间隔连发 | 一次解析要发近十个请求（view + 逐档 playurl + dash），机房 IP 上突发很容易被限 |
| IP 已被标记 | 机房 IP 段被批量风控。这条改代码解决不了，只能配 `BILI_COOKIE` 或换出口 IP |

对应地，`backend/bilibili.py` 做了**四层防护**，全部对调用方透明：

1. **设备指纹（`fingerprint()`）** —— 按需抓一套 `buvid3` / `buvid4` / `b_nut` 并缓存（默认 6 小时），**所有**接口请求都带上，包括未登录用户。两个来源互补：
   - 站点首页的 `Set-Cookie` → `buvid3` + `b_nut`（`b_nut` 是「首次访问时间」，只有这条路径能拿到）；
   - `x/frontend/finger/spi` → 补 `buvid4`（新版风控会校验）。
   - 账号凭据（扫码登录 / `BILI_COOKIE`）与指纹合并时**账号优先**，避免换指纹导致登录态和新设备对不上。
2. **浏览器化请求头（`_browser_headers()`）** —— 补齐 `Accept` / `Origin` / `sec-ch-ua` / `sec-fetch-*`；`Referer` 精确到**视频页**（`https://www.bilibili.com/video/BV…/`）而不是站点首页。
3. **请求节流（`_throttle()`）** —— 全局最小间隔 0.2s，把突发摊平。
4. **412 自动换指纹重试（`_api_get()`）** —— 命中 412 时清空指纹缓存重新抓一套（等价于「换台设备」）并退避重试，最多 2 次；仍失败才回报用户，且给的是可操作的建议而不是裸状态码。

配套地，**画质枚举做了跳档优化**：接口把 `qn=120` 降到 `64` 时，说明 `(64, 120]` 这一段账号全拿不到，中间那些档位直接跳过。未登录用户一次解析的 playurl 请求数从 **7 次降到 3 次**（120→64、32、16）——少发请求本身就是最有效的防 412 手段。

> 四个参数都可用环境变量微调（一般无需改动）：`BILI_FP_TTL` / `BILI_412_RETRY` / `BILI_MIN_INTERVAL`，见 `.env.example`。
> 排查 412 请用 `GET /api/bili/debug`——它会直接回报**指纹到底拿到没有**（`fingerprint.has_buvid3` / `has_buvid4` / `has_b_nut`），第一件要确认的就是这个。

### DASH 高清档位与 ffmpeg

1080P60 / 4K / HDR 这类档位**只以 DASH 形式提供**（视频流、音轨两条独立文件），浏览器没法自己合并。所以需要**服务端用 ffmpeg 无损封装**（`-c copy`，不重新编码，很快）：

```
GET /api/dash?bvid=&cid=&qn=&codecid=&filename=
```

传 `bvid/cid/qn` 而**不是直链**：DASH 直链带时效签名，存下来再传回来可能已过期，让服务端在下载那一刻重新申请，天然规避过期问题。

**实测：DASH 不只是「高阶补充」，普通用户也常受益。** 匿名状态下 durl（MP4）能拿到什么，**因视频而异**——同一批视频实测：

```
BV1GJ411x7h7  mp4=[720P, 360P]   dash=[480P, 360P]  -> 最终 [720P, 360P]
BV1uv411q7Mv  mp4=[360P]         dash=[480P, 360P]  -> 最终 [480P(合并), 360P]
BV1fK4y1t7hj  mp4=[360P]         dash=[480P, 360P]  -> 最终 [480P(合并), 360P]
```

也就是说有些视频 durl 只给到 360P，而 DASH 能给到 480P，**双格式并用后这些视频的画质上限反而提高了**。只走 durl 的老做法会白白丢掉这一档。

几个实现要点：

- **走代理/直连差异极大**：实测同一段流，走本地代理 **12.57 MB/s**，强制直连（`NO_PROXY='*'`）会退化到几乎卡死。排查下载慢时先看这个。
- **只挑 `dash.audio`（AAC）**，**不碰** `dolby` / `flac`——杜比全景声（EC-3）和无损需要特殊解码器，多数播放器放不了，选了会让用户下到「能下不能放」的文件。
- **同一档位多编码时优先 H.264**（`7 < 12 < 13 < 14`），兼容性最好。
- 合并产物加 `-movflags +faststart`（moov 前置），边下边播更流畅。
- **临时文件必须清理**：合并产物动辄上百 MB，直接下载在响应结束后清理临时目录；生成地址对应的文件在有效期结束后清理。
- **没装 ffmpeg 时自动隐藏 DASH 档位**（而不是列出来点了必然失败），MP4 档位不受影响，功能优雅降级。

> `ffmpeg` 路径可用 `FFMPEG_BIN` 指定；不设则从 `PATH` 找，Windows 上若 PATH 条目不能执行，会继续查找 WinGet 安装目录中的真身。
>
> **可用性检测是「真实执行」（跑一次 `ffmpeg -version`，结果缓存 10 分钟），不是「看文件在不在」。** 这个区别很关键：只做存在性检查的话，Windows + WinGet 那个 0 字节 shim 会被当成可用，用户就会看到 480P「音视频合并」档位、点下去、等十几秒，最后拿到报错——恰恰是本节第一条要避免的反面。详见「本地跑最容易踩的坑」。
>
> 启动时若检测到「有候选但都跑不起来」，日志里会打印一行 `[bilibili] 检测到 ffmpeg 候选 ... 但均无法执行`，方便定位。

### 画质与账号权益

标准 1080P 与 1080P+ 是不同档位，普通账号登录后也可获取平台提供的标准 1080P。B 站的[官方画质说明](https://www.bilibili.com/blackboard/activity-quality2pc.html)明确区分了普通 1080P 与大会员专享的 1080P+、60 帧档位。具体可下载档位取决于账号、片源及接口实际返回。

| 画质 | qn | 说明 |
|---|---|---|
| 1080P 标准 | 80 | 普通登录账号可用，以平台实际返回为准 |
| 1080P+ 高码率 | 112 | 与标准 1080P 不同，按大会员权益提供 |
| 1080P60 / 4K / HDR | 116 / 120 / 125 | 依账号权益与片源提供，需要合并音视频 |
| 720P / 480P / 360P | 64 / 32 / 16 | 实际可用档位因片源、登录状态和格式而异 |

MP4（`fnval=1`）请求返回 720P，并不表示账号最高只能获取 720P；同一视频的 DASH 响应可能提供 1080P。项目会合并两种格式的实际可用档位。只有 `accept_quality` 的声明、没有对应视频流时，不列为可下载档位。

DASH 下载需要可运行的 ffmpeg。缺少它时高清档位会被隐藏，页面会明确提示，画质诊断也会区分“平台已提供高清流，但服务缺少 ffmpeg”和“平台没有下发该档位”。Windows 版会自动尝试 WinGet 安装目录中的真实可执行文件，避开 PATH 中不能执行的 shim。

### 扫码登录（解锁更高画质）

前端在解析出 B 站视频后，结果卡片里会出现一条登录状态条，点「扫码登录」弹出二维码，用**哔哩哔哩 App** 扫码确认即可（登录后能拿到什么画质取决于账号权益，见上表）。

流程全部走 B 站官方扫码接口，服务端只做转发：

1. `POST /api/bili/login/qr` → 调 `passport.bilibili.com/.../qrcode/generate` 拿到 `qrcode_key` 和待编码 url；
2. 前端用**本地内置**的 `qrcode-generator`（`static/vendor/qrcode.js`）把 url 画成 SVG，无需后端出图、也不依赖任何外部 CDN；
3. `GET /api/bili/login/qr/poll?key=` → 每 2 秒轮询一次 `.../qrcode/poll`，状态码含义：`86101` 待扫码 / `86090` 已扫码待确认 / `86038` 已失效 / `0` 成功；
4. 轮询成功时，B 站通过 `Set-Cookie` 下发凭据，服务端取出后**存入该访客的会话**，随后自动重新解析，画质列表立刻变高。

**凭据归属（重要设计）**：凭据按**浏览器会话**隔离，A 扫码不会让 B 也用 A 的账号。

- 会话 id 是一个随机串，放在 `HttpOnly` Cookie（`vd_sid`）里，前端 JS 读不到，凭据本身**只存服务端内存**，从不下发给浏览器；
- 轮询接口会校验 `qrcode_key` 是否属于当前会话（`qr_is_current`），拿别人的 key 来轮询会返回 **409 `stale`**，防止把别人的登录结果写进自己的会话；
- 凭据**只存内存、不落盘**，服务重启即失效，需要重新扫码。

凭据优先级：**访客扫码（会话级）> `BILI_COOKIE`（`.env` 里的服务器级账号）**。两者都配了也没关系，访客用自己的；访客没扫码才回落到服务器账号。

### 下载中转

两个平台的 CDN 都有防盗链（Referer 校验）且直链带时效，所以下载统一经服务端中转，按平台附带对应请求头（抖音用移动端 UA + 抖音 Referer，B 站用桌面端 UA + `https://www.bilibili.com/`）。为避免被当成开放代理，`/api/download` 只放通抖音 / B 站的 CDN 域名。

## 注意事项

- **番剧受地区限制（`area_limit`）时无法观看**，这是 B 站的版权地域策略，与登录无关；
- `BILI_COOKIE` 等同账号凭据，**不要提交到 Git、不要外泄**；
- 视频直链有**时效性**（通常数小时），过期后重新解析即可；
- 简介生成目前基于标题 / 描述规则拼接，预留了 `make_summary()` 入口，后期可接入 AI 摘要接口；
- **仅供个人学习与备份使用**，请尊重原创作者版权，请勿用于商业用途或二次分发。

> 这里只列不重复的内容。其余运营层面的结论（未登录最高 720P、会员集只能拿到试看、凭据按访客隔离、公共实例限流等）见「常见问题」与上文各对应章节。
