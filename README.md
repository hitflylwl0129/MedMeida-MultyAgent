# 医药营销多 Agent 协同平台

> 面向医药行业短视频营销的多 Agent 协同平台 Demo——把「话术撰写 → 分镜拆解 → 真实生视频 → 多平台分发」拆成可独立演化的 Agent，跑在 FastAPI + LangGraph 单进程编排上。

## 在线演示

| 入口 | 地址 |
|---|---|
| **应用首页** | http://162.14.76.209/ （自动跳转 `/app.html`） |
| 话术 Agent | http://162.14.76.209/script.html |
| 视频制作 Agent | http://162.14.76.209/video.html |
| 视频分发 Agent | http://162.14.76.209/distribute.html |
| 健康检查 | http://162.14.76.209/api/health |

## 架构

```
浏览器 (prototype/*.html，纯静态)
   │
   ├─ /         → nginx → /opt/video-agent/prototype/  （静态托管）
   └─ /api/*    → nginx → 127.0.0.1:8001 uvicorn (FastAPI)
                            │
                            ├─ LangGraph 编排：storyboard → generate → compliance → handoff
                            ├─ Worker 线程：CreateAigcVideoTask → 轮询 → 成片
                            └─ 外部依赖：
                                 - vod.tencentcloudapi.com (AIGC 生视频 / motion_control)
                                 - api.lkeap.cloud.tencent.com (LLM：话术 Agent)
                                 - member.bilibili.com (B 站投稿)
```

## 目录

```
.
├─ backend/                 后端（FastAPI + LangGraph + 腾讯云 VOD SDK）
│  ├─ app/
│  │   ├─ main.py           路由入口（/api/health, /api/video/jobs ...）
│  │   ├─ agents/           话术 / 分镜 / 合规 / B站投稿 各 Agent
│  │   ├─ orchestrator/     LangGraph 状态机（含 motion_control 与 avatar_i2v 两条路径）
│  │   ├─ vod_client.py     CreateAigcVideoTask + DescribeTaskDetail
│  │   ├─ composer.py       本地 ffmpeg 拼片 / 字幕烧录
│  │   ├─ tts.py            TTS 合成（口播音轨）
│  │   └─ ...
│  ├─ assets/doctors/       6 张医生形象（avatar_i2v 首帧素材）
│  ├─ assets/motion_ref/    motion_control 参考动作视频
│  ├─ requirements.txt
│  ├─ run.sh                本地一键启动
│  ├─ warmup.py             形象库预热（图片直传 VOD 换 FileId）
│  └─ .env.example          配置模板（密钥不入库）
├─ prototype/               前端原型（纯静态 HTML，调 /api/*）
│  ├─ app.html              入口
│  ├─ script.html           话术 Agent
│  ├─ video.html            视频制作 Agent
│  └─ distribute.html       视频分发 Agent
├─ archive/                 历史版本归档（v0.x）
├─ securevault_demo/        SecureVault PoC（独立 Demo）
├─ deploy.sh                一键发布到云服务器
├─ Demo落地规划_*.md         Demo 落地规划与排期
├─ Agent_Runtime_部署*.md    Agent Runtime 部署设计说明
├─ 医药营销多Agent*.md       平台技术路线方案
└─ 抖音方案.md               抖音分发方案
```

## 本地启动

```bash
cd backend
cp .env.example .env       # 首次：填腾讯云 SecretId/SecretKey、LLM_API_KEY 等
./run.sh                   # 自动建 venv + 装依赖 + 起 uvicorn http://127.0.0.1:8000

# 前端（另开终端）
cd ../prototype
python3 -m http.server 8848
# 浏览器打开 http://localhost:8848/app.html
```

`backend/run.sh --warmup` 会先把 6 张医生形象图直传 VOD 换 FileId 入缓存（避免每次任务上传冗余媒资）。

依赖：

- Python 3.10+（线上跑 3.12，本地实测 3.14 也可）
- ffmpeg ≥ 4.x（**字幕烧录需要 libass**：Ubuntu 默认带；macOS 上 `brew install ffmpeg-full` 后在 `.env` 设 `FFMPEG_BIN`）
- 腾讯云账号（VOD 已开通且申请 AIGC 白名单，或使用 motion_control / 本地 TTS 路径）

## 环境变量

详见 [`backend/.env.example`](backend/.env.example)。关键项：

| 变量 | 说明 |
|---|---|
| `TENCENTCLOUD_SECRET_ID` / `SECRET_KEY` | 腾讯云 API 密钥（仅后端持有，不入库） |
| `VOD_SUB_APP_ID` | VOD 子应用 ID |
| `VIDEO_BACKEND` | `local` / `motion` / `aigc` 三选一，控制视频生成路径 |
| `LLM_API_KEY` / `LLM_MODEL` | 话术 Agent 使用的 LLM（OpenAI 兼容端点） |
| `BILI_SESSDATA` / `BILI_JCT` | B 站 Web 投稿 Cookie（仅启用 B 站分发时需要） |
| `APP_HOST` / `APP_PORT` | 服务监听地址（线上配 `127.0.0.1:8001`，由 nginx 反代） |
| `FFMPEG_BIN` | macOS 用 `ffmpeg-full` 时指向其路径；Linux 留空走 PATH |

## 部署到云服务器

服务器侧已部署一份运行实例（Ubuntu 24.04 + nginx + systemd），更新代码用根目录 [`deploy.sh`](deploy.sh)：

```bash
./deploy.sh        # 默认目标 162.14.76.209，rsync 同步代码 + 重启 systemd 服务
./deploy.sh --tail # 同步 + 重启 + tail 日志（看启动是否正常）
```

`deploy.sh` 默认排除 `.venv` / `.cache` / `.env` / `__pycache__` / `*.log`，**不会覆盖云端 `.env`**。

服务器布局（参考）：

```
/opt/video-agent/backend/        ← 后端代码 + venv + .env
/opt/video-agent/prototype/      ← 前端静态
/etc/systemd/system/video-agent.service     systemd 单元
/etc/nginx/sites-available/video-agent      nginx 反代（含 SSE 配置）
/var/log/video-agent.log         后端日志
```

监听端口：`127.0.0.1:8001 (uvicorn)`，对外通过 `nginx :80`。

## API

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/health` | 配置/密钥/形象库就绪状态 |
| GET | `/api/doctors` | 医生形象库清单（key/姓名/缩略图地址） |
| GET | `/api/doctors/{key}/image` | 医生形象缩略图 PNG |
| POST | `/api/video/jobs` | 从话术终稿创建生视频任务，返回 `job_id` |
| GET | `/api/video/jobs/{id}/events` | SSE 进度流（驱动前端 st1..st5） |
| GET | `/api/video/jobs/{id}/file` | 回放本地链路成片 mp4 |
| POST | `/api/script/generate` | LLM 流式生成话术（SSE 逐 token） |
| GET | `/api/distribute/bilibili/status` | B 站投稿能力就绪检测 |
| POST | `/api/distribute/bilibili` | 执行 B 站真实投稿（SSE：上传分片→提交→返回 BVID） |

## 安全

- 所有密钥仅存于 `backend/.env`（已 gitignore），代码只从环境变量读取，**不硬编码、不下发前端**。
- 仓库密钥扫描已通过：无 AKID / SecretKey / SESSDATA 等明文凭证。
- 部署脚本默认 `--exclude='.env'`，本地修改不会覆盖云端配置。

## 文档

- [Demo 落地规划与排期](Demo落地规划_需求拆解_前后端设计_排期.md)
- [Agent Runtime 部署与管理设计](Agent_Runtime_部署与管理_设计说明.md)
- [医药营销多 Agent 协同平台技术路线方案](医药营销多Agent协同平台_技术路线方案.md)
- [抖音方案](抖音方案.md)
- [SecureVault PoC 启动规划](SecureVault_M1_PoC_启动规划.md) / [可行性验证报告](SecureVault_可行性Demo_验证报告.md)

## License

内部 Demo，未对外授权。
