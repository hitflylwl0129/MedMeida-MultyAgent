# backend v0.5 变更补丁包

> 对应前端归档：`../prototype_demo_v0.5/`
> 快照时间：2026-06-04 08:30
> 内容：`backend/` 完整源码快照（**已排除** `.env`(密钥)、`.venv`、`.cache`、`__pycache__`、`*.pyc`、`data/*.db`、日志等运行时/敏感文件）
> 凭证：不含任何真实密钥；新增/已有配置项见脱敏的 `backend/.env.example`

---

## 一句话概述
v0.5 后端新增 **B站官方 Web 投稿真实链路**、**6 医生专属动作视频强化 motion_control**，并**下线话术受众侧重**、修复**话术第 4 步 SSE 卡死**与**生视频"卡在 75%"**。

---

## 变更文件清单（相对 v0.4）

| 文件 | 类型 | 变更 |
|------|------|------|
| `app/agents/bilibili_agent.py` | **新增** | B站 Web 投稿全流程：preupload → ?uploads → PUT 分片 → complete → x/vu/web/add/v3，流式产出 stage/progress/done(bvid) |
| `app/config.py` | 改 | 新增 `BILI_*` 配置（SESSDATA/JCT/BUVID3/TID/TAG/ONLY_SELF/PROFILE/TIMEOUT） |
| `app/schemas.py` | 改 | 新增 `BiliPublishRequest`；`audience_key` 标记弃用 |
| `app/main.py` | 改 | 新增 `GET /api/distribute/bilibili/status` + `POST /api/distribute/bilibili`(SSE)；`/api/doctors` 增 `has_motion_ref`；话术接口移除 audience_key 使用 |
| `app/motion_ref.py` | 改 | 新增 `ref_filename_for_doctor()` / `has_per_doctor_ref()`：按医生匹配专属参考视频 |
| `app/orchestrator/graph.py` | 改 | `_generate_motion` 用医生专属参考视频；PROCESSING 进度爬升+计时（消除 75% 观感） |
| `app/agents/script_agent.py` | 改 | 移除内置 `AUDIENCE_PROFILES`；目标人群改由上游 `audience` 字段构造 |
| `.env.example` | 改 | 追加 `BILI_*` 脱敏占位 |
| 其余 `app/*.py`、`assets/`、`requirements.txt` 等 | 未变 | 随快照一并归档以便整体还原 |

---

## 关键改动详情

### 1. B站真实投稿（新增 `app/agents/bilibili_agent.py`）
- 协议：`preupload`(拿 auth/biz_id/chunk_size/endpoint/upos_uri) → `POST ?uploads`(建分片会话拿 upload_id) → `PUT` 逐片上传(拿 eTag) → `POST complete`(合片) → `POST x/vu/web/add/v3`(提交，拿 aid/bvid)。
- 鉴权：Cookie(SESSDATA + bili_jct)；成片来源优先级 `video_path` > `job_id` > 最近一条 `.cache/jobs/*/out.mp4`。
- 对外：`credentials_ready()` / `latest_local_video()` / `publish_stream(...)`。
- 安全：凭证仅后端 `.env`；默认 `BILI_ONLY_SELF=1`（仅自己可见）。

### 2. 6 医生专属动作视频强化（`motion_ref.py` + `graph.py`）
- `assets/motion_ref/<医生中文名>.mp4` 与 6 张医生照片一一对应。
- `ref_filename_for_doctor(doctor_name)`：命中专属视频则用之，否则回退默认 `ref.mp4`。
- `_generate_motion` 据所选医生解析参考视频 FileId，进度消息标注「医生专属·已强化」。

### 3. 话术受众侧重下线（`script_agent.py` + 前端 `script.html`）
- 后端删除 `AUDIENCE_PROFILES`；`build_messages` 的目标人群由上游 `audience`（mainAge/topInterest/tier/reach）构造。
- `audience_key` 入参保留兼容但不再用于画像查找。

### 4. 修复
- **话术第 4 步 SSE 卡死**：前端按 `\n\n` 切帧而后端发 `\r\n\r\n`(CRLF)→ 帧切不出（前端已修，详见 `prototype/script.html`）。
- **生视频卡在 75%**：① `--reload` 重启杀死在飞轮询线程→任务被遗弃；② PROCESSING 进度封顶 75% 看似卡死。后端进度改为爬升+计时；运行**建议不带 `--reload`**。

---

## 还原 / 运行
1. 解压本包，将 `backend/` 覆盖回项目（或对照清单逐文件回贴）。
2. 复制 `.env.example` 为 `.env` 并填入真实密钥（腾讯云 VOD/TTS/LLM + B站 SESSDATA/JCT）。
3. 安装依赖并启动（**不带 --reload，避免重启中断生视频任务**）：
```bash
cd backend
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8000
```

## 排除内容（不在本包内）
- `.env`（真实密钥）—— 仅提供脱敏 `.env.example`
- `.venv/`（416M，依赖按 requirements.txt 重建）
- `.cache/`（101M，VOD FileId 缓存 + 成片产物，运行时自动生成）
- `data/*.db`（任务运行时状态）、`*.pyc`、`__pycache__/`、日志、`.DS_Store`
