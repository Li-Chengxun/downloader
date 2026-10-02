# 抖音 / 哔哩哔哩 视频下载站

粘贴分享链接，自动识别平台与内容类型，解析出封面、标题、简介与全部可用清晰度，一键下载原画质视频（不二次转码）。

**两个平台已合并为一条通道**：界面只有一个输入框，**不需要选择平台，也不需要选择内容类型**——粘贴链接后自动识别并走对应解析通道。

抖音侧三类内容都能解析：**视频**、**图文帖（plog）**、**纯文字帖**。图文帖有两种下载形式：逐张下载无水印原图（可一键打包成 zip），或由服务端合成为一个幻灯片视频。

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

> 第 4 步刻意放在抖音域名之后：抖音的分享文案里经常夹带无关字符，若先认裸号，会把「抖音分享文字里恰好出现 `BV…` 字样」误判成 B 站。只要文案里有明确的抖音链接，就以抖音为准。
>
> 第 5 步兜底抖音而不是统一报「无法识别」，是为了让报错更有指导性（抖音侧会明确提示「这不是抖音链接」）。前端此时会提示「未能识别平台，将按抖音尝试解析」，让用户心里有数。

> **番剧编号为什么必须严格匹配**：`md123` 这类裸编号采用的是「**整个输入**就是编号」的整串匹配，而不是「在文本里搜」。宽松匹配有真实误判风险——抖音文案里出现 `md5` 就会被 `md\d+` 命中而误判成番剧。用户从 App 分享出来的番剧一定是完整 URL（走域名规则），裸编号只可能是手输，格式必然干净，所以收紧不会漏掉正常输入。
>
> 平台判定只需到「B 站」这一层，**不再细分普通视频 / 番剧**——那是 `bilibili.parse` 内部的事。前端提示与后端路由共用同一份实现，避免两边规则不一致。

## 项目结构

```
douyin-downloader/
├── backend/
│   ├── main.py              # FastAPI 服务：/api/parse（含平台自动识别）、/api/download、/api/dash、/api/cover、B站登录、静态托管
│   ├── parser.py            # 抖音解析：链接规范化 + 双引擎解析 + 内容类型分发（视频/图文/文字）
│   ├── bilibili.py          # 哔哩哔哩解析：统一入口 parse() 内分普通投稿(UGC)与番剧(PGC)
│   │                        #   + 链接规范化 + 官方接口 + 画质枚举 + DASH 合并 + 扫码登录
│   ├── slideshow.py         # 抖音图文帖：图片打包 zip / ffmpeg 合成幻灯片视频
│   ├── ffmpeg_tool.py       # ffmpeg 定位与探测（B站 DASH 合并与图文合成视频共用）
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
19. **番剧（动漫）怎么下？** 直接把番剧链接粘进同一个输入框即可，无需切换模式：
   - `https://www.bilibili.com/bangumi/play/ep741735`（单集）
   - `https://www.bilibili.com/bangumi/play/ss44904`（整季，默认从第 1 集开始）
   - `https://www.bilibili.com/bangumi/media/md20144639`（作品页）
   - 也支持裸编号 `ep741735` / `ss44904` / `md20144639`
   解析后会出现**选集网格**：正片按集数排列，PV / 花絮单独分组；付费集的右上角有金色「会员」角标；长篇番剧（如柯南）还可以用下方的横滑条切换其他季。点某一集即重新解析该集。
20. **番剧会员集提示「本集暂不可下载」，但界面没报错？** 这是**有意设计**，不是 bug。
   B 站在未登录 / 非大会员时访问会员集，`code` 仍是 `0`、HTTP 仍是 `200`，但只给一段约 **3 分钟试看片段**（`is_preview=1`）。如果直接放行，用户会下到一个「以为是正片、其实是试看」的文件——所以代码识别到试看后**拒绝列出任何档位**，并把卡片其余部分（标题、封面、选集）照常展示，只把下载区换成一句说明。
   想下载完整正片，点「扫码登录」登录**大会员**账号后重新解析即可。也可以点「画质诊断」看明细——番剧诊断会逐档列出 `is_preview` 与「返回时长 vs 官方时长」，一眼就能看出是不是试看片段。
21. **番剧免费集只有 480P / 360P？** 与普通视频同理，受账号权益与视频本身上限限制。番剧的画质普遍低于普通投稿（很多老番正片源本身就是 480P）。
22. **番剧画质只探当前这一集，切集为什么要重新解析？** 一季可能几十上百集（柯南有 1000+ 集），把每一集的画质都探一遍会发出成百上千次请求，必然触发风控（412）。所以服务端只探当前集，切集时前端再发一次请求（带 `&ep=<ep_id>`）。
23. **抖音图文帖（plog）怎么下载？为什么有两个下载按钮？** 直接把图文链接粘进同一个输入框，解析后卡片会变成**图片网格**，每张图下面有「原图」，整帖下面有两个出口：
    - **打包下载全部（zip）** —— 一个帖子常有 9~30 张图，逐张点太累，直接打成一个 zip；
    - **下载成视频** —— 把整组图合成为一个幻灯片 MP4（每张停留 3 秒）。

    两种形式可以都要，互不影响。图片取的是**无水印原图**。
24. **「下载成视频」出来的视频没有声音？** 是预期行为。抖音不给图文帖下发原始背景音乐（接口里 `music.play_url` 为 null，要登录态才有），所以服务端合成的是**无声幻灯片**。界面上也已经标注了这一点。
25. **为什么图文帖的「下载成视频」按钮有时不显示？** 合成依赖服务端 ffmpeg。容器镜像里已内置；本机跑如果没装（或只有 WinGet 的 0 字节假 shim），后端会在解析结果里回报 `server_ffmpeg: false`，前端据此**不显示**该按钮——不给你列点了必然失败的入口（与 DASH 高清档位同样的处理）。此时打包下载图片仍然可用。
    > 本地调试要指定真实 ffmpeg：`FFMPEG_BIN=/真实路径/ffmpeg bash run-local.sh`，详见「本地跑最容易踩的坑」。
26. **抖音的纯文字帖 / 长图文（文章）能解析吗？** 纯文字帖可以：解析后会以 `kind="text"` 返回，卡片直接把**完整正文**展示出来（这类内容没有媒体文件可下，所以没有下载按钮）。
    长图文（抖音 2025-12 上线的「文章」，最多 8000 字 + 30 图）走的是同一套分享页/接口链路，目前按「有图当图文帖、无图当文字帖」处理；如果正文出现在独立的字段里（而不是 `desc`），可能只拿到简介而非全文。**遇到解析不完整的长图文，请把链接发来**，补上对应的字段映射。
27. **解析图文帖时提示「分享页暂时只返回了空壳」？** 抖音分享页的响应是非确定的，第一次常常只给布局壳。代码已内置按模板 + 轮次重试（`_SHARE_RETRY`），偶发失败时再点一次解析即可。

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
| `/api/images/zip`              | POST | **把图文帖的图片打包成一个 zip** 下载。JSON 体：`{images:[{url,urls,width,height,ext}], filename}`                                                                  |
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

早先只认视频——**没有视频流就一律报「未获取到可用的视频流地址」**，所以图文帖（plog）和纯文字帖都解析不了。现在两个引擎都按同一顺序判定，并各自带上 `kind`：

| 内容    | 判定依据                             | 结果                |
| ----- | -------------------------------- | ----------------- |
| 视频    | 有可下载的视频流（`bit_rate` / `streams`） | `kind="video"`（默认） |
| 图文帖    | `images[]` 非空                     | `kind="images"`   |
| 纯文字帖  | 既无视频流也无图片，但有文案                    | `kind="text"`     |

> **判定顺序必须「先图后视频」**：图文帖里同样有一个 `video` 对象，但它是**占位符**——`play_addr.uri` 指向 `images_no_sound_volume_audio_file.mp3`（一段空音频），`duration` 为 0。若先判视频，图文帖会被当成「有一个 0 秒的视频」，用户下到的是坏文件。识别见 `_FAKE_PLAY_URI`。

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

##### 「下载成视频」是服务端自己合成的

抖音**不给图文帖提供可用的视频**（引擎 A 的 `media.video` 为 null；引擎 B 的 `play_addr` 指向空音频占位符），所以「下载成视频」由服务端用 ffmpeg 合成幻灯片：

1. 下载原图 → 2. 按所有图的**最大宽高**建统一画布（上限 1080×1920，宽高取偶）→ 3. 等比缩放 + 居中补白（`force_original_aspect_ratio=decrease` + `pad`，混排的横竖图才能被 concat 解复用器当成同一条流）→ 4. 输出 H.264 MP4。

合成产物是**无声**的：原始 BGM 拿不到（`music.play_url` 为 null，需登录态才下发），前端会明确标注。

> **一个反直觉的实测**：concat 清单末尾**不要**再重复最后一个文件。网上常见写法是「末尾把最后一张再写一遍」（那是针对**视频**输入的惯用法），但对**图片**输入实测相反——重复会实打实多播一张的时长（3 张 × 2s 期望 6.00s，重复后变 7.97s）。见 `_write_concat_list`。
>
> 合成用的 ffmpeg 与 B 站 DASH 合并是同一套（`ffmpeg_tool.py`），包括那套「真实执行探测」的坑。

**自托管引擎 A（推荐生产环境）**：把 Douyin_TikTok_Download_API 用 Docker 自部署后配置环境变量即可，无需改代码：

```bash
DTK_BASE_URL=https://你的实例地址
DTK_USERNAME=你的账号
DTK_PASSWORD=你的密码
```

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
- **番剧的会员集未登录时只能拿到 3 分钟试看片段**。本项目识别到试看会**拒绝列出档位**（绝不把试看当正片给用户），登录大会员账号后可下载完整正片；
- **番剧受地区限制（`area_limit`）时无法观看**，这是 B 站的版权地域策略，与登录无关；
- **扫码登录凭据按访客会话隔离且只存内存**：不落盘、不下发前端、服务重启即失效；A 扫码不会让 B 用 A 的账号；
- `BILI_COOKIE` 等同账号凭据，**不要提交到 Git、不要外泄**；
- 视频直链有**时效性**（通常数小时），过期后重新解析即可；
- 简介生成目前基于标题/描述规则拼接，预留了 `make_summary()` 入口，后期可接入 AI 摘要接口；
- **仅供个人学习与备份使用**，请尊重原创作者版权，请勿用于商业用途或二次分发。
