<!-- 短视频分发 Agent · B站真实投稿 见文末「B站投稿」一节 -->
# 短视频制作 Agent · 后端（M1 真实生成通道）

把原型里「短视频制作 Agent」的阶段③从 Mock 升级为**真实调用腾讯云点播 AIGC 生视频**
（`CreateAigcVideoTask`，`Kling · avatar_i2v` 数字人），打通一条端到端真实链路。

> 对应决策：(a) demo 先打通一条真实成片；模型 `Kling/avatar_i2v`；音频用 API 内置
> `AudioGeneration`；合规先用 API 内置；架构沿用 Runtime 文档「FastAPI + LangGraph 单进程，
> 生视频走 Worker」。

## 架构（形态 A：单进程内编排）

```
前端 video.html
   │ POST /api/video/jobs          ┌──────────── FastAPI 进程 ────────────┐
   │ SSE  /api/video/jobs/{id}/events     LangGraph 编排：                  │
   ▼                                 storyboard → generate → compliance →  │
ScriptInput(话术终稿)                              │(长任务)        handoff │
                                                   ▼                        │
                                       Worker 线程：CreateAigcVideoTask     │
                                       → 轮询 DescribeTaskDetail → 成片     │
                                   └──────────────────────────────────────┘
                                                   │
                                          腾讯云点播 VOD (vod.tencentcloudapi.com)
```

- 逻辑解耦：每个阶段是独立模块（`agents/`、`orchestrator/graph.py`）。
- 生视频是分钟级长任务 → 放 `asyncio.to_thread` 的 Worker 线程内轮询，不阻塞事件循环。
- 升级形态 B（独立 GPU Worker/队列）时，只需替换 `generate` 节点实现，编排不变。

## 目录

```
backend/
├─ app/
│  ├─ config.py          # 从 .env 读密钥/参数（禁止硬编码）
│  ├─ schemas.py         # Pydantic 契约：ScriptInput/Storyboard/VideoJob...
│  ├─ store.py           # SQLite 任务存储
│  ├─ vod_client.py      # CreateAigcVideoTask + DescribeTaskDetail(容错解析)
│  ├─ vod_upload.py      # 医生形象图 → 首帧 FileId（本地直传 / URL 拉取）
│  ├─ doctors.py         # 医生形象库（6 人设）+ 本地直传 + FileId 缓存
│  ├─ agents/storyboard.py   # 阶段②分镜拆解（规则法，可换 LLM）
│  ├─ orchestrator/graph.py  # LangGraph 状态机
│  ├─ worker.py          # 异步 Worker + SSE 事件总线
│  └─ main.py            # FastAPI 路由
├─ assets/doctors/       # 6 张医生形象图（avatar_i2v 首帧素材）
├─ requirements.txt
├─ .env.example          # 配置模板
└─ run.sh                # 一键启动
```

## 启动

```bash
cd backend
cp .env.example .env       # 首次：填入密钥（本地 .env 已被 gitignore）
./run.sh                   # 建 venv + 装依赖 + 起服务 http://127.0.0.1:8000
```

健康检查：`curl http://127.0.0.1:8000/api/health`

前端：另开终端 `cd prototype && python3 -m http.server 8848`，打开
`http://localhost:8848/video.html`，点 **⚡ 真实生成（腾讯云 AIGC）**。

## API

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/health` | 配置/密钥/形象库就绪状态（`doctors_ready`、`default_doctor`） |
| GET | `/api/doctors` | 医生形象库清单（key/姓名/性别/年龄/缩略图地址/是否已缓存 FileId） |
| GET | `/api/doctors/{key}/image` | 医生形象缩略图（PNG，供前端选择器渲染） |
| POST | `/api/video/jobs` | body `{script, doctor_key?, doctor_image_fileid?, doctor_image_url?}`，返回 `job_id` |
| GET | `/api/video/jobs/{id}` | 任务快照 |
| GET | `/api/video/jobs/{id}/events` | SSE 进度（驱动前端 st1..st5） |
| GET | `/api/video/jobs` | 最近任务列表 |

## 医生形象库（首帧素材）

`avatar_i2v` 需要医生正面形象图作首帧。本项目已内置 **6 个人设**（年长/中年/青年 × 男/女）
于 `assets/doctors/`，开箱即用：

- 选择优先级：请求 `doctor_key` > 配置 `DEFAULT_DOCTOR` > `DOCTOR_IMAGE_FILEID` > `DOCTOR_IMAGE_URL`。
- 形象库 key：`senior_male / senior_female / middle_male / middle_female / young_male / young_female`。
- 首次用到某形象时，自动把本地图直传 VOD 换 `FileId` 并**缓存**到 `.cache/doctor_fileids.json`，
  之后复用，避免重复上传产生冗余媒资。
- 前端 `video.html` 的「出镜医生」卡片会拉取 `/api/doctors` 渲染缩略选择器，点击即切换出镜形象。
- 如需用外部素材兜底，仍可在 `.env` 填 `DOCTOR_IMAGE_FILEID/URL`，或在创建任务时传 `doctor_image_fileid/url`。

## 安全

- 密钥仅存于 `backend/.env`（已 gitignore），代码只从环境变量读取，**不硬编码、不下发前端**。
- ⚠️ 当前这对密钥曾在沟通中明文出现，**demo 验证后请到控制台轮换**。

## 已知待对齐项（字段以官方数据结构为准）

- `DescribeTaskDetail` 的 `AigcVideoTask.Output` 成片字段命名官网未完整公开，
  `vod_client.parse_task_detail` 已对 `FileId/Url/MediaUrl/CoverUrl/Duration` 做容错解析；
  首次真跑后按真实返回收敛字段。
- `ModelVersion` 默认 `2.1`，若该版本不支持 `avatar_i2v` 按控制台/文档调整。
- 口播文本注入：当前随整体 `Prompt` 下发并 `AudioGeneration=Enabled`；若接口提供
  专用口播文本字段（或 `ExtInfo`/`SubjectInfos`），首跑后切换以确保口播=话术终稿。

---

## 短视频分发 Agent · B站真实投稿

把原型「短视频分发 Agent」的 B站平台从 Mock 升级为**真实调用 B站官方 Web 投稿 API**，
其余平台（抖音/视频号/快手/小红书）仍为模拟（依调研结论：仅 B站等少数平台有可直发的官方/可用接口）。

### 链路

```
前端 distribute.html
  │ GET  /api/distribute/bilibili/status   能力就绪检测（是否配凭证/有成片）
  │ POST /api/distribute/bilibili          SSE：stage→progress→done(bvid)
  ▼
agents/bilibili_agent.publish_stream():
  preupload → POST ?uploads(分片会话) → PUT 分片 → POST complete(合片) → x/vu/web/add/v3(提交)
  ▼
member.bilibili.com（Cookie 鉴权：SESSDATA + bili_jct）→ 返回 aid / bvid
```

- 成片来源优先级：`video_path` > `job_id` 对应成片 > 最近一条本地成片 `.cache/jobs/*/out.mp4`。
- 凭证只在后端 `.env`，**前端不接触、不入版本库**。默认 `BILI_ONLY_SELF=1`（仅自己可见），避免误发公开。

### 配置（backend/.env）

```
BILI_SESSDATA=...      # 浏览器登录 bilibili.com → F12 → Application → Cookies 复制
BILI_JCT=...           # 同上（CSRF token）
BILI_DEFAULT_TID=201   # 默认分区：知识区→科学科普
BILI_DEFAULT_TAG=健康科普,科普,养生
BILI_ONLY_SELF=1       # 1 仅自己可见(安全默认) / 0 公开
```

配置后后端自动热重载，刷新分发页 → 第④步「B站·真实投稿」面板出现按钮 → 点击即真实投稿，返回 BV 号与链接。
