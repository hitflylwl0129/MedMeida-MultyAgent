# 泛健康营销多 Agent 协同平台 · 原型 Demo

## 版本信息
- **版本号**：demo v0.5
- **归档时间**：2026-06-04 07:39
- **类型**：前端静态 HTML 快照 + 后端真链路
- **当前开发版本目录**：`../../../prototype/`（前端）+ `../../../backend/`（后端）
- **上一版本**：demo v0.4（`../../prototype_demo_v0.4/`）
- **配套后端补丁**：`../backend_v0.5_changes/`（同目录 `v0.5/` 下）

## 本版本范围（v0.5）
在 v0.4「话术 LLM 真出 + 短视频制作真链路」基础上，本版聚焦**真实分发闭环**与**生成效果强化**：

1. **短视频分发 Agent 接入 B站官方 Web 投稿 API（真实发布，非模拟）** —— 全链路打通「预上传 → 分片上传 → 合片 → 提交稿件」，返回真实 BV 号。
2. **短视频制作强化**：为 6 位医生各配专属参考动作视频，motion_control 按所选医生自动匹配，动作更贴合本人。
3. **话术 Agent 收敛**：下线"话术受众侧重"，目标人群直接取自上游人群 Agent 结果。
4. 修复若干阻断性问题（话术第 4 步 SSE 解析、生视频"卡在 75%"、分发页 B站面板不可见）。

**选品 → 选医生 → 目标人群 → 话术（LLM）→ 短视频制作（TTS+医生专属动作迁移）→ 短视频分发（B站真实投稿 + 多平台模拟）**

### 页面清单
| 文件 | Agent | 说明 |
|------|-------|------|
| `app.html` | 流程视图外壳（主入口） | 6 Agent 顺序导航 + iframe 承载功能页（**v0.5 加 iframe 防缓存时间戳**） |
| `index.html` | 工作台 / 只读汇总 | 侧栏导航 + 发起任务确认页 |
| `product.html` | 选品 Agent | 意图构建 → 召回 → 向量匹配 → 多因子打分 → 策略精排 → 下传 |
| `doctor.html` | 选医生 Agent | 检索 → 召回 → 向量打分 → LLM 精排 → 策略重排 → 下传 |
| `audience.html` | 目标人群 Agent | 信号融合 → 画像 → 平台标签 → 人群包估算 → 联动话术 |
| `script.html` | 话术 Agent | 上游融合 → RAG → Prompt → 真实 LLM 流式 → 合规审查 → 联动短视频（**v0.5 下线受众侧重**） |
| `video.html` | 短视频制作 Agent | 接收话术 → 真实 TTS + **医生专属动作迁移** + ffmpeg 拼合 → 真实成片 |
| `distribute.html` | 短视频分发 Agent | 接收成片 → 规格适配 → 定向排期 → **B站真实投稿** + 多平台模拟 → 数据回流 |

### v0.5 关键特性（相对 v0.4 的变化）

#### 1. 短视频分发 Agent · B站真实投稿（`distribute.html` + 后端）
- 依调研结论：抖音/快手/B站有官方直发能力，视频号/小红书需 RPA。本版先把 **B站做成真实链路**，其余 4 平台保持模拟。
- 新增后端 `agents/bilibili_agent.py`：完整复刻 B站 Web 投稿协议
  `preupload 预上传 → POST ?uploads 建分片会话 → PUT 分片上传 → POST complete 合片 → x/vu/web/add/v3 提交稿件`，返回真实 `aid/bvid`。
- 新增接口：`GET /api/distribute/bilibili/status`（能力检测）+ `POST /api/distribute/bilibili`（SSE：stage→progress→done(bvid)）。
- 成片来源优先级：`video_path` > `job_id` 对应成片 > 最近一条本地成片 `.cache/jobs/*/out.mp4`。
- 前端：B站投稿面板为**独立常亮区块**（不随 stage 置灰），实时显示分片进度 + BV 链接；未配置凭证给出 `.env` 引导。
- 安全：`SESSDATA/bili_jct` 仅在后端 `.env`，前端零接触；默认 `BILI_ONLY_SELF=1`（仅自己可见）。

#### 2. 短视频制作 · 6 医生专属动作视频强化（`backend`）
- `assets/motion_ref/` 放入 6 个按医生中文名命名的参考动作视频（中年女医生.mp4 …）。
- `motion_ref.ref_filename_for_doctor()`：按所选医生自动匹配同名参考视频，缺失才回退默认 `ref.mp4`。
- `graph._generate_motion` 用医生专属参考视频驱动 motion_control，进度消息标注「医生专属·已强化」。
- `/api/doctors` 新增 `has_motion_ref` 字段。
- motion 轮询进度优化：PROCESSING 阶段缓慢爬升至 76% + 显示已用时，消除"卡在 75%"观感。

#### 3. 话术 Agent · 下线"话术受众侧重"（`script.html` + 后端）
- 隐藏第 1 步"话术受众侧重"选择区，移除相关逻辑（AUDIENCES/curAud/segment 选择）。
- 第 3 步 Prompt 的「# 输入 目标人群」改为直接取上游人群 Agent 结果（`sv_selected_audience`：mainAge/topInterest/tier/reach）。
- 后端 `script_agent` 移除内置受众画像，目标人群由上游 `audience` 字段构造。

#### 4. 阻断性问题修复
- **话术第 4 步卡住**：`script.html` SSE 解析按 `\n\n` 切帧，但后端用 `\r\n\r\n`（CRLF）→ 帧永远切不出。已改为先归一化 CRLF + `split(/\r?\n/)`。
- **生视频卡在 75%**：① `--reload` 重启会杀死在飞的轮询线程→任务被遗弃（已改为不带 --reload 运行）；② PROCESSING 进度封顶 75% 看似卡死（已改为爬升+计时）。
- **分发页看不到 B站面板**：面板原在 `opacity:.4` 的 stage 内（父透明度限制子元素）→ 移出为独立常亮区块 + 加 no-cache。

### 沿用 v0.4 及更早的核心能力
- `app.html` 流程视图外壳 + 6 Agent 切换/高亮/完成态徽标
- 统一 `.stage` 步骤式 UI + 上游门禁 + `localStorage` 跨 Agent 传递
- 话术真实 LLM 流式（hy3-preview）+ 合规打回重跑闭环
- 短视频制作真实 TTS + motion_control 动作迁移 + 字幕烧录
- 三种视频后端模式（local / motion / aigc，`VIDEO_BACKEND` 切换，v0.5 默认 motion）

## 本地预览

### 仅看前端 UI（mock 兜底）
```bash
cd archive/v0.5/prototype_demo_v0.5
python3 -m http.server 8848
# 主入口：http://localhost:8848/app.html
```
此时话术真实生成、短视频真实成片、B站真实投稿均因 backend 不可达而不可用；其余展示功能可正常浏览。

### 完整真链路（需后端）
1. 进入 `backend/`，准备 `.env`（参考 `.env.example`），填腾讯云密钥（VOD + TTS + LLM）；
2. 配 B站投稿凭证（仅后端）：`BILI_SESSDATA` / `BILI_JCT`（浏览器登录 bilibili.com → F12 → Application → Cookies）；
3. 把 6 个医生专属参考视频放到 `backend/assets/motion_ref/<医生中文名>.mp4`；
4. 启动后端（**建议不带 --reload，避免重启杀死在飞的生视频任务**）：
```bash
cd backend
.venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8000
```
5. 启动前端静态服务：
```bash
cd archive/v0.5/prototype_demo_v0.5
python3 -m http.server 8848
# 浏览 http://localhost:8848/app.html
```

## 依赖外部服务
| 能力 | 服务 | 计费 |
|---|---|---|
| LLM 话术生成 | 腾讯云 lkeap / hy3-preview | 按 token |
| 口播合成 (TTS) | 腾讯云语音合成（超自然大模型音色）| 按字符 |
| 动作迁移 (motion_control) | 腾讯云 VOD AIGC (Kling/2.1) | 按视频秒 |
| 视频存储/直传 | 腾讯云 VOD | 按存储/流量 |
| B站投稿 | 哔哩哔哩 Web 投稿 API（Cookie 鉴权）| 免费（账号级） |

## 已知限制
- B站为官方接口直发，其余 4 平台（抖音/快手/视频号/小红书）仍为模拟；投稿后需经 B站审核，API 成功 ≠ 已上线。
- B站投稿默认"仅自己可见"，公开需在 `.env` 设 `BILI_ONLY_SELF=0`；`SESSDATA` 约 5 天过期需更新。
- 医生专属动作视频建议 9:16、5–10s、≤10MB；与口播时长不一致时 ffmpeg `-stream_loop` 循环画面对齐。
- 后端不带 --reload 运行时，改后端代码需手动重启才生效。
- 字幕烧录依赖 `ffmpeg-full`（含 libass）；默认 `ffmpeg` 自动降级为无字幕。
