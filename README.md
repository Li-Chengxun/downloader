# 抖音 / 哔哩哔哩 视频下载站

粘贴分享链接，自动识别平台，解析出视频封面、标题、简介与全部可用清晰度，一键下载原画质视频（不二次转码）。

**两个平台已合并为一条通道**：界面只有一个输入框，**不需要选择平台**——粘贴链接后自动识别并走对应解析通道。用哪个平台完全由链接决定。

| 平台   | 支持的输入形态                                                                    |
| ---- | -------------------------------------------------------------------------- |
| 抖音   | `v.douyin.com` 短链、`www.douyin.com/video/{id}` 长链、`?modal_id=` 网页版链接、整段分享文字 |
| 哔哩哔哩 | `b23.tv` 短链、`www.bilibili.com/video/BV…` 长链、`av` 号、整段分享文字；多分P视频可切换选集       |

### 平台识别规则

识别逻辑在前端（实时提示）和后端（真正路由）各有一份，**两边规则必须保持一致**，否则会出现「界面提示哔哩哔哩、实际却按抖音解析」的错位。判定顺序（越靠前越明确）：

1. 输入里有链接就**先抽出链接**再判定（分享文案里可能混着无关域名）；
2. 链接含 B 站域名（`bilibili.com` / `b23.tv`）→ **哔哩哔哩**；
3. 链接含抖音域名（`douyin.com` / `iesdouyin.com`）→ **抖音**；
4. 没有链接时才可能是**裸视频号**（`BV…` / `av…`）→ **哔哩哔哩**；
5. 都识别不出 → 按**抖音**兜底。

> 第 4 步刻意放在抖音域名之后：抖音的分享文案里经常夹带无关字符，若先认裸号，会把「抖音分享文字里恰好出现 `BV…` 字样」误判成 B 站。只要文案里有明确的抖音链接，就以抖音为准。
>
> 第 5 步兜底抖音而不是统一报「无法识别」，是为了让报错更有指导性（抖音侧会明确提示「这不是抖音链接」）。前端此时会提示「未能识别平台，将按抖音尝试解析」，让用户心里有数。

## 项目结构

```
douyin-downloader/
├── backend/
│   ├── main.py              # FastAPI 服务：/api/parse（含平台自动识别）、/api/download、/api/dash、/api/cover、B站登录、静态托管
│   ├── parser.py            # 抖音解析：链接规范化 + 双引擎解析 + 多码率整理
│   ├── bilibili.py          # 哔哩哔哩解析：链接规范化 + 官方接口 + 画质枚举 + DASH 合并 + 扫码登录
│   ├── sessions.py          # 会话表（内存）：按访客隔离扫码登录凭据
│   ├── requirements.txt     # Python 依赖（精确锁定版本，避免解析爆炸，见「常见问题 17」）
│   ├── Dockerfile           # 生产镜像（非 root 用户 + 健康检查 + 内置 ffmpeg）
│   ├── .dockerignore
│   └── static/
│       ├── index.html       # Vue 3 前端（Vue 已本地化到 vendor/，无需构建；含平台自动识别提示）
│       └── vendor/
│           ├── vue.global.prod.js    # Vue 3 运行时
│           └── qrcode.js             # 二维码生成（qrcode-generator，前端本地渲染 SVG）
├── deploy/
│   ├── preflight.sh         # 部署前环境自检（只读，不修改系统）
│   ├── deploy.sh            # 一键部署脚本（含状态查询、日志跟踪）
│   ├── fix-docker-mirror.sh # 自动挑选可用镜像加速源（解决拉镜像超时）
│   ├── check-progress.sh    # 构建卡住时判断「正在下载」还是「真卡死」
│   ├── nginx.conf           # Nginx 反向代理（HTTP 版，首次部署用）
│   ├── nginx-https.conf     # Nginx 反向代理（HTTPS 版模板）
│   ├── douyin-downloader.service  # 裸机部署的 systemd 服务
│   └── certs/               # HTTPS 证书存放目录（gitignore）
├── run-local.sh             # 本地开发启动脚本（自动挑 Python 与 ffmpeg，规避 WinGet 坏 shim）
├── .env.example             # 环境变量模板
├── docker-compose.yml       # 容器编排（web + 可选 nginx）
├── .gitignore
└── README.md
```

## 快速开始

### 方式一：本地运行

**最省事：用自带的启动脚本**（自动挑 Python 与 ffmpeg，见下方说明）

```bash
bash run-local.sh
# 换端口：PORT=9000 bash run-local.sh
```

脚本只做三件事，都是本地开发最容易卡住的地方：

1. **挑 Python** —— 按 `.venv/` → 托管环境 → 系统 Python 的顺序，找**第一个真能 `import fastapi, uvicorn, httpx`** 的；
2. **挑 ffmpeg** —— 关键在它会**用 Python 真实执行** `ffmpeg -version` 来验证（原因见下）；
3. 起 `uvicorn`。

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

Windows 上用 **WinGet** 装的 ffmpeg，`PATH` 里那个 `ffmpeg.exe` 是一个
**0 字节的 reparse point（shim）**，真身在 `WinGet\Packages\Gyan.FFmpeg_*\...\bin\` 下。
它的坏处是**看起来一切正常，直到用户点了高清档位才失败**：

| 检查方式 | 结果 | 说明 |
|---|---|---|
| `Path.exists()` | ✅ 通过 | 文件确实在 |
| `shutil.which()` | ✅ 通过 | 找得到 |
| Python `subprocess` 执行 | ❌ `[WinError 193]` | **应用实际走的路径，会失败** |
| Git Bash 直接执行 | ✅ 通过 | bash 会跟随 reparse point |

最后两行的差异是重点：**bash 能跑不代表应用能跑**。所以 `run-local.sh` 用 `$PY`（应用实际使用的解释器）去验证 ffmpeg，而不是用 bash 的 `-version`——否则会把坏路径当成可用的选出来。

`backend/bilibili.py` 里的 `ffmpeg_path()` 也做了同样的真实执行探测，所以**手动启动时**选到坏路径的表现是：DASH 档位直接不出现（正确行为），日志里会有一行 `[bilibili] 检测到 ffmpeg 候选 ... 但均无法执行`。

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

脚本会自动：检查 Docker → 生成 `.env` → 构建镜像 → 启动服务 → 等待健康检查通过 → 打印访问地址。

此时浏览器打开 `http://服务器IP:8000`（或 `http://服务器IP` 如果用了 `--proxy`）就能用了。

其他常用命令：

```bash
bash deploy/deploy.sh --status   # 查看运行状态 + 健康检查
bash deploy/deploy.sh --logs     # 跟踪日志
docker compose down              # 停止服务
docker compose up -d             # 启动服务
```

### 第 3 步：自托管解析引擎（生产环境建议做）

默认走公共实例 `api.douyin.wtf` 的 demo 账号，**有频率限制**——请求稍快就会返回「解析请求过于频繁」。自用够，公开访问一定要换掉。

上游项目提供了一键安装脚本，部署它自己的服务：

```bash
# 用官方脚本安装（会自动识别系统、检查 Docker、生成密码）
curl -fsSL https://raw.githubusercontent.com/Evil0ctal/Douyin_TikTok_Download_API/main/install/install.sh -o install.sh
less install.sh        # 建议先看一眼脚本内容
bash install.sh

# 或用已发布镜像手动部署
git clone https://github.com/Evil0ctal/Douyin_TikTok_Download_API.git
cd Douyin_TikTok_Download_API
# 按仓库说明生成 .env（DTK_SECRET_KEY / POSTGRES_PASSWORD / REDIS_PASSWORD 等）
docker compose -p dtk -f docker/compose.yml pull
docker compose -p dtk -f docker/compose.yml up -d
docker compose -p dtk -f docker/compose.yml logs api    # 读出初始化 token
```

上游 v5 默认监听 **8000** 端口。启动后用日志里的 setup token 创建第一个管理员账号，然后把账号密码填进本项目的 `.env`：

```bash
# /opt/douyin-downloader/.env
DTK_BASE_URL=http://127.0.0.1:8000    # 上游引擎地址（同机部署就是本机端口）
DTK_USERNAME=你的管理员账号
DTK_PASSWORD=你的管理员密码

# 改完重启本项目
docker compose up -d
```

> 如果上游引擎和其它服务端口冲突，改它的端口映射即可，`DTK_BASE_URL` 跟着改。

### 第 4 步：绑定域名 + 开启 HTTPS

```bash
# 1) 把域名的 A 记录解析到服务器 IP（在域名服务商后台操作）
# 2) 修改 Nginx 配置里的 server_name，把 _ 换成你的域名
vim deploy/nginx.conf

# 3) 申请证书（请把 your-domain.com / 邮箱 换成你的）
#    卷名随项目目录名变化，先用下面命令查一下真实卷名：
#       docker volume ls | grep certbot-webroot
docker run --rm \
  -v "$PWD/deploy/certs:/etc/letsencrypt" \
  -v "$(docker volume ls --format '{{.Name}}' | grep -E 'certbot-webroot$' | head -1 | xargs -I{} docker volume inspect {} -f '{{.Mountpoint}}'):/var/www/certbot" \
  certbot/certbot certonly --webroot -w /var/www/certbot \
  -d your-domain.com --email you@example.com --agree-tos --no-eff-email

# 4) 启用 HTTPS 配置
cp deploy/nginx-https.conf deploy/nginx.conf
docker compose exec nginx nginx -s reload
```

证书 90 天到期，建议加个定时续期任务：

```bash
# crontab -e 添加（每月 1 号凌晨续期并重载）
0 3 1 * * docker run --rm -v "$PWD/deploy/certs:/etc/letsencrypt" -v "$(docker volume ls --format '{{.Name}}' | grep -E 'certbot-webroot$' | head -1 | xargs -I{} docker volume inspect {} -f '{{.Mountpoint}}'):/var/www/certbot" certbot/certbot renew --webroot -w /var/www/certbot --quiet && docker compose exec nginx nginx -s reload
```

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

代码有新版本时，**不要**重新跑 `deploy.sh`（那是首次部署用的，会重新生成 `.env`、动防火墙）。用增量更新脚本：

```bash
# 在你自己电脑上：把新代码传上去（.env 不在包内，服务器上的配置不会被覆盖）
scp douyin-downloader-update.tar.gz root@你的服务器IP:/tmp/
```

```bash
# 在服务器上：解包覆盖代码，再执行更新
cd /opt/douyin-downloader          # 换成你的实际项目目录
tar -xzf /tmp/douyin-downloader-update.tar.gz --strip-components=1 -C .
bash deploy/update.sh              # 重建镜像 + 重启，自动沿用现有 Nginx 配置
```

> `--strip-components=1` 不能省：包内顶层是 `douyin-downloader/` 目录，
> 不加会让代码解到 `/opt/douyin-downloader/douyin-downloader/` 里去，
> 执行 `update.sh` 就会报「请在项目根目录执行」。

`update.sh` 做的事与安全保证：

| 行为 | 说明 |
| --- | --- |
| 保留 `.env` | 不覆盖、不重写，你的 Cookie / 端口 / `DTK_*` 原样生效 |
| 备份旧镜像 | 自动打 `douyin-downloader:backup-<时间戳>`，一条命令可回滚 |
| 自动识别 Nginx | 检测到 `nginx` 容器就带 `--profile proxy` 一起更新，不用手动加参数 |
| 构建失败即停 | 旧容器继续运行、服务不中断 |
| 验证 ffmpeg | 更新后进容器跑 `ffmpeg -version`，确认 DASH 高清档位真的解锁了 |

> **为什么必须重建镜像而不是 `docker compose restart`？**
> 本次更新改动了 `Dockerfile`（新增安装 ffmpeg）。重启容器只是把旧镜像再跑一遍，ffmpeg 不会被装进去，1080P60 / 4K / HDR 这些需要合并音视频的档位就依然是隐藏状态。只有 `docker compose build` 重新构建才会带上 ffmpeg。

其它常用参数：

```bash
bash deploy/update.sh --clean     # 忽略构建缓存全量重建（apt 装 ffmpeg 失败时先试这个）
bash deploy/update.sh --help      # 查看用法
```

构建报 `load metadata ... i/o timeout` → 先 `sudo bash deploy/fix-docker-mirror.sh`；
装 ffmpeg 时 apt 报错 → 在 `.env` 里改 `APT_MIRROR=mirrors.aliyun.com`（默认清华源）后重跑。

### 常见问题

1. **构建时报 `load metadata ... i/o timeout` / `DeadlineExceeded`？**  
   服务器连不上 Docker Hub（国内服务器最常见）。执行本项目自带的加速脚本，它会逐个测试候选源、只写入实测可用的、并做一次真实拉取验证：
   ```bash
   sudo bash deploy/fix-docker-mirror.sh
   sudo bash deploy/deploy.sh --proxy
   ```
   如果所有加速源都不可用，改用兜底通道（绕开 Docker Hub，直接从国内镜像源拉基础镜像）——在 `.env` 里取消注释：
   ```ini
   BASE_IMAGE=docker.m.daocloud.io/library/python:3.12-slim
   NGINX_IMAGE=docker.m.daocloud.io/library/nginx:1.27-alpine
   PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple
   ```
   再不行就走第 5 步的裸机部署（不用 Docker，自然没有镜像拉取问题）。
2. **服务器连不上抖音？** 解析完全依赖服务器能访问 `douyin.com`。部分海外机房会被地域限制，国内机房 IP 也可能触发风控。判断方法：在服务器上执行 `curl -I https://www.douyin.com/`，返回 200/302 即为正常；超时或被拒就要换机房或走代理出口。
3. **哔哩哔哩只能下到 720P？** 这是 B 站未登录状态下的画质上限，不是 bug。解锁方式有两种，**推荐第一种**：
   - **访客扫码登录（推荐）**：解析后点结果卡片里的「扫码登录」，用哔哩哔哩 App 扫码即可，每个访客登自己的号、互不影响，凭据只存内存不落盘；
   - **服务器统一账号**：把浏览器登录后的 Cookie 填进 `.env`，所有访客共用这个账号：
     ```ini
     BILI_COOKIE=SESSDATA=你在浏览器里拿到的值
     ```
   然后 `docker compose up -d` 重启。注意 Cookie 等同账号凭据，别外泄、别提交到 Git。

   即使登录了，1080P+ / 4K 还取决于该视频本身是否提供该档位、以及账号是否有对应权益（如大会员）。**1080P60 / 4K / HDR 还需要服务器装 ffmpeg**（这些档位以 DASH 形式提供，要服务端合并音视频），Docker 镜像已内置，裸机部署见「快速开始」。
4. **B 站多分P视频只下到第一P？** 解析结果里会出现选集按钮（`P1 / P2 / …`），点一下即可切换；也可以直接在链接里带 `?p=3`。
5. **解析提示「请求过于频繁」？** 说明在共用公共 demo 实例，按第 3 步自托管即可解决。哔哩哔哩走官方接口，不受此限。
6. **下载大视频卡住 / 服务器内存暴涨？** 检查 Nginx 是否漏了 `proxy_buffering off`（本项目自带配置已加好）。
7. **下载很慢或流量费很高？** 视频经服务器中转，4K 视频单条可达 24MB。按流量计费的云服务器要注意成本，或改用按带宽计费。
8. **视频下载到一半失败？** 两个平台的直链都有时效性（通常几小时），重新解析一次即可。
9. **下载接口返回 400「直链域名 … 不在下载白名单内」？** 说明直链域名不在白名单里。B 站会把部分直链下发到**第三方 PCDN 边缘节点**（实测如 `xxx.edge.mountaintoys.cn:4483`），这类域名默认刻意不放通——`/api/download` 是开放接口，白名单太松会被当成免费代理滥用。代码已经在解析阶段用 `prefer_official_cdn` 把官方 CDN（`upos-*.bilivideo.com` 等）排到第一位，所以**重新解析**一次通常就好了。若平台真的换了官方 CDN 域名，在 `.env` 里往 `EXTRA_BILIBILI_HOSTS` 或 `EXTRA_DOUYIN_HOSTS` 追加域名关键字即可，报错信息里会直接告诉你被拦的是哪个域名。
10. **域名能访问但页面打不开？** 云厂商**安全组**没放通 80/443 端口，在控制台加规则即可。
11. **端口被占用？** 改 `.env` 里的 `APP_PORT`，或调整上游引擎的端口映射。
12. **扫码登录后还是 720P？** 最可能的原因是**账号不是大会员**。B 站对非大会员账号封顶 720P，1080P / 1080P60 / 4K / HDR 全部需要大会员——登录本身不改变这一点，详见上面的「画质与账号权益对照」表。点结果卡片里的「**画质诊断**」按钮可以直接看清原因：它会逐档探测并告诉你凭据是否生效、账号是不是大会员、视频本身上限是多少。
13. **扫码提示「二维码已失效」？** 二维码有 180 秒有效期，过期后点弹窗里的「刷新二维码」重新生成即可。若提示「不属于当前会话」，说明页面刷新或换了浏览器，重新扫码即可。
14. **能不能让访客共用我的 B 站账号？** 不建议。本项目默认按访客会话隔离，A 扫码不会让 B 用 A 的账号。若确实想统一账号，用 `.env` 里的 `BILI_COOKIE`（服务器级），但要清楚这等于把所有访客的下载都记在你的账号上。
15. **看不到 1080P60 / 4K / HDR 这些档位？** 三个条件缺一不可：① 服务器装了 ffmpeg（没装会直接隐藏，不列出来让你点了失败）；② 账号有对应权益（大多需大会员）；③ 视频本身提供了该档位。注意这些档位**只在 DASH 格式下提供**，本项目会为它们单独显示「音视频合并」角标和「合并并下载」按钮。
16. **下载高清档位很慢 / 卡住？** DASH 档位要服务端先合并音视频，大文件需数十秒，界面上按钮会变成「⏳ 合并中…」。如果慢得异常，优先排查**出网方式**：实测同一段流走代理 **12.57 MB/s**，而强制直连会退化到几乎卡死——如果你给服务设了 `NO_PROXY='*'` 之类的变量，把它去掉试试。
17. **更新时构建报 `ResolutionImpossible` / `Cannot install ... because these package versions have conflicting dependencies`？**
   这是 **pip 装 Python 依赖失败，跟 ffmpeg 无关**（apt 那层已经装好了，所以别去折腾 ffmpeg）。
   典型报错长这样，注意看它列到最后会把锅甩给 pydantic：
   ```
   The conflict is caused by:
       fastapi 0.110.0 depends on pydantic!=1.8, !=1.8.1, ..., <3.0.0 and >=1.7.4
   ERROR: ResolutionImpossible
   ```
   **根因**：`fastapi` 过去写成无上界的 `>=0.110`，pip 从最新版一路向下试探（约 60 个版本），会向镜像站发出几百次元数据请求、耗时数分钟；期间只要 `pydantic` 的索引页被限流或超时，pip 就认定「pydantic 一个可用版本都没有」，于是把所有 `fastapi` 版本全判为冲突——报错指向 pydantic，真正的原因是**解析爆炸**。
   **本项目已通过锁定版本修掉**（见 `backend/requirements.txt`，全部改成 `==`），正常情况下不会再出现。
   如果你仍然遇到，按顺序做：
   ```bash
   # ① 加一个备用源，主源抖动时自动兜底
   echo 'PIP_EXTRA_INDEX_URL=https://mirrors.aliyun.com/pypi/simple' >> .env
   bash deploy/update.sh

   # ② 还是不行？单独跑一次解析，几秒钟看清到底是哪个包缺了（不重建镜像）
   docker run --rm -v "$PWD/backend:/w:ro" python:3.12-slim \
     pip install --dry-run -i "$(grep -E '^PIP_INDEX_URL=' .env | cut -d= -f2)" -r /w/requirements.txt
   ```
   想彻底确认装进去了什么，构建完成后可以查：
   ```bash
   docker compose exec web cat installed-versions.txt
   ```
18. **解析 B 站提示 `HTTP 412`（风控拦截）？**
   这是 B 站**风控网关**拒绝了请求，不是接口挂了，也不是链接有问题。最常见的原因是**服务器出口 IP 被批量风控**（机房 IP 段是重灾区）。
   代码已自动做了四层防护（设备指纹 / 浏览器请求头 / 请求节流 / 412 换指纹重试），正常情况下碰不到；如果**持续**出现，按顺序试：
   ```bash
   # ① 先确认指纹到底拿到没有（第一件要查的事）
   curl -s 'http://127.0.0.1:8000/api/bili/debug?url=https://www.bilibili.com/video/BV1GJ411x7h7' \
     | python3 -m json.tool | grep -A6 fingerprint

   # ② 指纹键齐全（buvid3 / b_nut）却仍 412 → 基本可以确定是 IP 被限，
   #    配一个登录态能显著提升请求信誉度（BILI_COOKIE 获取方式见「常见问题 3」）
   echo 'BILI_COOKIE=SESSDATA=你的值' >> .env && bash deploy/update.sh

   # ③ 仍然不行 → 换服务器出口 IP，或给容器配一个 HTTP 代理出口
   ```
   也可以先调大节流间隔观察：`.env` 里设 `BILI_MIN_INTERVAL=0.6`（更保险但更慢），然后 `bash deploy/update.sh`。

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
| `/api/parse?url=&p=`           | GET  | 解析分享链接（**自动识别平台**，无需前端指定），返回 `platform`、`title`、`summary`、`cover`、`author`、`duration_text`、`qualities[]`（label / note / size_text / codec / urls / format / needs_merge / qn）。B 站多分P时另返回 `pages[]` 与 `current_page`；`p` 用于指定分P |
| `/api/download?url=&filename=` | GET  | 流式代理下载视频直链，触发浏览器保存                                                                                                                                                                          |
| `/api/dash?bvid=&cid=&qn=&codecid=&filename=` | GET  | **下载 DASH 高清档位**：服务端拉取音视频流并用 ffmpeg 无损合并（`-c copy`）成完整 MP4 后返回。未装 ffmpeg 时返回 503                                                                                            |
| `/api/cover?url=`              | GET  | 代理封面图（绕过图床 Referer 限制 / 修正 http 封面）                                                                                                                                                         |
| `/api/bili/login`              | GET  | 查询当前会话的 B 站登录状态，返回 `logged_in` / `source`（`session` 访客扫码 / `env` 服务器账号 / `none`）/ `user` / `can_logout`                                                                                     |
| `/api/bili/login/qr`           | POST | 申请登录二维码，返回 `qrcode_key` 与待编码 `url`（前端自行渲染 SVG）                                                                                                                                              |
| `/api/bili/login/qr/poll?key=` | GET  | 轮询扫码状态，返回 `waiting` / `scanned` / `expired` / `success`；成功即把凭据写入本会话。非本会话的 key 返回 409 `stale`                                                                                                |
| `/api/bili/logout`             | POST | 退出登录，**只清当前会话**的凭据，不影响其他访客                                                                                                                                                                  |
| `/api/bili/debug?url=&p=`      | GET  | **画质诊断**：逐档探测 MP4 并额外问一次 DASH，回报「凭据是否生效 / 是否大会员 / 视频本身上限」，并给出人话结论。**不含凭据明文**，可安全贴出排查                                                                                                        |

`platform` 取值 `douyin` 或 `bilibili`，前端据此显示来源标记。`/api/parse` 的响应还带 `logged_in`，供前端同步登录态显示。

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

**自托管引擎 A（推荐生产环境）**：把 Douyin_TikTok_Download_API 用 Docker 自部署后配置环境变量即可，无需改代码：

```bash
DTK_BASE_URL=https://你的实例地址
DTK_USERNAME=你的账号
DTK_PASSWORD=你的密码
```

### 哔哩哔哩（官方接口）

全部走 B 站官方 web 接口，**不需要 WBI 签名、不需要登录**：

1. **链接规范化** —— `b23.tv` 短链跟随 301/302 拿到含 BV 号的长链；`av` 号通过接口换成 BV 号。
   > 注意：b23.tv 对**无效短码**返回的是 **HTTP 200 + `{"code":-404}`**，不是 302。代码对两种情况都做了处理。
2. **元信息** —— `x/web-interface/view`，拿到标题 / 简介 / 封面（`http` 链接会改写成 `https`）/ UP 主 / 时长 / 分P列表。
3. **播放地址（双格式，取长补短）** —— 同时问两种格式，再合并结果：
   - **MP4（`fnval=1`，返回 `durl`）** —— 一个**自带音轨的完整 MP4**，正好和抖音「一个清晰度一个成品文件」的体验对齐。实测 51 分钟 / 655MB 的视频仍是单段，直接整体下载，**不需要 ffmpeg 合并**。
   - **DASH（`fnval=4048`）** —— 音视频分离的流。它的价值在于**能拿到 MP4 拿不到的高阶档位**（1080P60 / 4K / HDR 只在 DASH 下提供）。
4. **画质枚举** —— MP4 逐档请求，DASH 一次请求返回全部档位；**只保留接口真实返回了该档位的那些**。
5. **直链排序** —— B 站有时把 `url` 指到**第三方 PCDN 边缘节点**，官方 CDN 只放在 `backup_url` 里。而前端只取列表第一个地址去请求 `/api/download`，那个接口有域名白名单（防开放代理），PCDN 域名不在其中就会 400。所以解析时用 `prefer_official_cdn` 把 `upos-*.bilivideo.com` 这类官方地址排到最前，其余地址保留在末尾作为服务端合并下载的兜底。

**合并策略（`merge_formats`）**：同一档位优先用 MP4（自带音轨，少一次服务端合并）；DASH 只用来**补齐 MP4 拿不到的档位**，且要求该档位**高于 MP4 的最高档**——否则会出现「720P 已有 MP4，却还列一个需要合并的 480P」这种荒唐选项。这样普通视频（MP4 已覆盖全部档位）完全不引入合并开销，只有真正需要高阶画质时才走 ffmpeg。

> **关于画质上限**：B 站未登录时 durl 最高 720P。接口返回的 `support_formats` / `accept_quality` 会列出**「理论上支持」**的档位（含 1080P+），但那是账号权益层面的能力，未必能拿到——如果照着它列清单，用户点了 1080P 实际下载到 720P，等于欺骗。所以这里按真实返回的 `quality` 出清单，拿不到的不列。想解锁更高画质，让访客**扫码登录**（见下节），也可配 `BILI_COOKIE` 作为服务器级兜底。

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
- **临时文件必须清理**：合并产物动辄上百 MB，下载接口在响应结束后用 `BackgroundTask` 删除临时目录。
- **没装 ffmpeg 时自动隐藏 DASH 档位**（而不是列出来点了必然失败），MP4 档位不受影响，功能优雅降级。

> `ffmpeg` 路径可用环境变量 `FFMPEG_BIN` 指定；不设则从 `PATH` 找。
>
> **可用性检测是「真实执行」而非「看文件在不在」**：程序会跑一次 `ffmpeg -version` 确认它真的能启动（结果缓存 10 分钟）。
> 这个区别很关键——Windows 上用 WinGet 安装 ffmpeg 时，`WinGet\Links` 目录里那个 `ffmpeg.exe` 是一个
> **0 字节的 reparse point（shim）**：`shutil.which()` 找得到、`Path.exists()` 是 `True`，
> 但一执行就报 `[WinError 193] %1 不是有效的 Win32 应用程序`。
> 若只做存在性检查，用户就会看到 480P「音视频合并」档位、点下去、等十几秒，最后拿到报错——
> 恰恰是本节第一条要实现的效果的反面。
>
> 遇到这种情况，把 `FFMPEG_BIN` 指向真实路径即可：
> ```ini
> FFMPEG_BIN=C:/Users/你/AppData/Local/Microsoft/WinGet/Packages/Gyan.FFmpeg_xxx/ffmpeg-9.0.2-full_build/bin/ffmpeg.exe
> ```
> 启动时若检测到「有候选但都跑不起来」，日志里会打印一行 `[bilibili] 检测到 ffmpeg 候选 ... 但均无法执行`，方便定位。

### 画质与账号权益对照（实测）

**关键结论：普通账号（非大会员）登录后仍然封顶 720P。** 1080P 及以上必须大会员。

| 画质 | qn | 需要什么 | 备注 |
|---|---|---|---|
| 1080P+ 高码率 | 112 | **大会员** | 非会员看到这个选项也拿不到 |
| 1080P | 80 | **大会员** | 接口声明「登录即可」，但实测非会员仍回落 720P |
| 720P / 720P60 | 64 / 74 | 登录 | 未登录时 720P 也可能拿到（见下） |
| 360P / 480P | 16 / 32 | 无 | 游客可用 |

实测数据（同一视频，未登录 / 非大会员）：

```
问 qn=120 / 112 / 116 / 80 / 74 / 64  →  全部只返回 quality=64（720P）
问 qn=32 / 16                          →  返回 quality=16（360P）
DASH 的 accept_quality 声明 [112, 80, 64, 32, 16]，但实际只下发 [32, 16]
```

所以界面文案写的是「**登录后按账号权益解锁更高画质（1080P 及以上需大会员）**」，而不是笼统的「登录即可解锁 1080P」——后者会让非会员用户登录后白期待一场。

> 注意：另有 1080P60 / 4K / HDR 等档位，**只在 DASH 格式下提供**（音视频分离）。这些档位本项目**已支持**——服务端会用 ffmpeg 无损合并音视频后输出完整 MP4，详见上面的「DASH 高清档位与 ffmpeg」。前提是服务器装了 ffmpeg，且账号确实有对应权益（大多需大会员）。

### 扫码登录（解锁更高画质）

未登录时 durl 最高 720P；登录后能拿到什么画质**取决于账号权益**（1080P 及以上需大会员，见上表）。前端在解析出 B 站视频后，结果卡片里会出现一条登录状态条，点击「扫码登录」弹出二维码，用**哔哩哔哩 App** 扫码确认即可。

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

- 公共演示接口（api.douyin.wtf demo 账号）**有限流**，高频使用请自托管引擎 A 并配置 `DTK_BASE_URL` 等环境变量；哔哩哔哩走官方接口，无此限制；
- **哔哩哔哩未登录时最高 720P**，可用「扫码登录」让访客登自己的账号解锁更高画质（推荐），也可配 `BILI_COOKIE`（见「常见问题」第 3 条）；
- **扫码登录凭据按访客会话隔离且只存内存**：不落盘、不下发前端、服务重启即失效；A 扫码不会让 B 用 A 的账号；
- `BILI_COOKIE` 等同账号凭据，**不要提交到 Git、不要外泄**；
- 视频直链有**时效性**（通常数小时），过期后重新解析即可；
- 简介生成目前基于标题/描述规则拼接，预留了 `make_summary()` 入口，后期可接入 AI 摘要接口；
- **仅供个人学习与备份使用**，请尊重原创作者版权，请勿用于商业用途或二次分发。
