# 泛健康营销多 Agent 协同平台 · 原型 Demo

## 版本信息
- **版本号**：demo v0.4
- **归档时间**：2026-06-03 23:44
- **类型**：前端静态 HTML 快照 + 后端真链路（首次引入后端）
- **当前开发版本目录**：`../../prototype/`（前端）+ `../../backend/`（后端）
- **上一版本**：demo v0.3（`../prototype_demo_v0.3/`）

## 本版本范围（v0.4）
在 v0.3「六环节闭环 + 候选全可选用」基础上，**把"话术 Agent 第 4 步生成话术"与"短视频制作 Agent"从演示动画升级为真实链路**：接入腾讯云 LLM/TTS/VOD/动作迁移，打通端到端"真实文本 → 真实口播 → 真实成片"。这是首个引入后端的版本。

**选品 → 选医生 → 目标人群 → 话术（LLM 真出文案）→ 短视频制作（真实 TTS+动作迁移）→ 短视频分发**

### 页面清单
| 文件 | Agent | 说明 |
|------|-------|------|
| `app.html` | 流程视图外壳（主入口） | 顶部按流程顺序排列 6 个 Agent + 下方 iframe 承载功能页 |
| `index.html` | 工作台 / 只读汇总 | 侧栏导航（置顶「流程视图」入口）+ 发起任务确认页 |
| `product.html` | 选品 Agent | 意图构建 → 召回 → 向量匹配 → 多因子打分 → 策略精排（候选全可选用）→ 下传选医生 |
| `doctor.html` | 选医生 Agent | 检索构建 → 召回 → 向量打分 → LLM 精排（候选全可选用）→ 策略实时重排 → 下传人群 |
| `audience.html` | 目标人群 Agent | 信号融合 → 画像 → 平台标签 → 人群包估算（档位全可选用）→ 联动下游话术 Agent |
| `script.html` | 话术 Agent | 上游融合 → RAG → Prompt → **真实 LLM 生成（hy3-preview，SSE 流式）** → 合规审查 → 联动短视频 |
| `video.html` | 短视频制作 Agent | 接收话术 → 真实 TTS（腾讯云）+ 真实动作迁移（Kling motion_control）+ ffmpeg 拼合 → 真实成片 |
| `distribute.html` | 短视频分发 Agent | 接收合规成片 → 多平台规格适配 → 人群定向+发布排期 → 多平台发布执行 → 数据回流闭环 |

### v0.4 关键特性（相对 v0.3 的变化）

#### 1. 话术 Agent · 真实 LLM 接入（`script.html`）
- 第 4 步「LLM 生成话术」从 **mock 写死文案** 升级为 **真实调腾讯云 hy3-preview**（OpenAI 兼容端点 `lkeap/plan/v3`）
- 后端 `POST /api/script/generate` 提供 SSE 流式接口：边出 token 边推前端，打字机效果保留
- 5 要素融合 Prompt：产品受控口径 + 医生口播风格 + 目标人群画像 + 话术结构/目标 + 受众侧重
- 后端硬约束清洗：正则违禁词替换（治愈/根治/疗效承诺/绝对化用语/医生代言）作为合规兜底
- 第 1 轮仍演示 mock V1（含违规）→ 合规打回；第 2 轮起调真实 LLM 出合规终稿
- 时序由"固定 5.2s"改为"LLM done 事件触发"，避免 LLM 慢于动画
- 失败优雅降级：后端不通 / 密钥未配 → 红色错误提示 + 可重试
- 安全：API Key 仅在 `backend/.env`，前端零暴露

#### 2. 短视频制作 Agent · 真实成片链路（`video.html`）
- 接入腾讯云 **VOD AIGC `motion_control`**：医生形象图 + 参考动作视频 → 医生有微表情/微动作
- 接入腾讯云 **TTS（超自然大模型音色）**：6 个医生形象映射 6 种音色，按句切分合成
- 字幕烧录：`ffmpeg-full` + libass + Hiragino Sans GB，按 storyboard 时间精确显隐
- 三种后端模式可切换（`backend/.env` 的 `VIDEO_BACKEND`）：
  - `local`：静帧 + TTS 口播 + 字幕（最快、无需 Kling 计费）
  - `motion`：motion_control 动效 + TTS + 字幕（**v0.4 默认**）
  - `aigc`：预留 avatar_i2v 白名单接入点
- 任务目录自动清理（`LOCAL_KEEP_JOBS=20`）
- 成片走 `GET /api/video/jobs/{id}/file` 直接 `<video>` 流式播放

#### 3. 后端架构（新增 `backend/`）
```
backend/
├── app/
│   ├── main.py              FastAPI 入口
│   ├── config.py            pydantic-settings 统一配置（.env）
│   ├── llm.py               OpenAI 兼容封装 + 流式 + 指数退避重试
│   ├── tts.py               腾讯云 TextToVoice 分句合成
│   ├── composer.py          ffmpeg 拼图/拼视频/字幕烧录
│   ├── vod_client.py        VOD AIGC: avatar_i2v / motion_control
│   ├── vod_upload.py        本地图/视频直传换 FileId
│   ├── doctors.py           6 医生形象库 + TTS 音色映射 + FileId 缓存
│   ├── motion_ref.py        参考动作视频 FileId 缓存（mtime+size 签名）
│   ├── schemas.py           入参/出参 Pydantic 契约
│   ├── store.py             任务快照内存存储
│   ├── worker.py            SSE 事件总线 + 异步任务调度
│   ├── orchestrator/graph.py LangGraph 状态机（storyboard→generate→compliance→handoff）
│   └── agents/
│       ├── storyboard.py    分镜拆解 + overall_prompt 组装
│       └── script_agent.py  5 要素话术 Prompt + sanitize 兜底
├── assets/
│   ├── doctors/             6 张医生形象图（PNG）
│   └── motion_ref/          motion_control 参考视频（mp4）
├── .env                     凭证/密钥（不入仓）
├── .env.example             模板
├── requirements.txt
├── run.sh                   启动脚本（支持 --warmup 预热形象库 FileId）
└── warmup.py                独立预热脚本
```

### 沿用 v0.3 / v0.2 / v0.1 的核心能力
- `app.html` 流程视图外壳 + 6 Agent 切换/高亮/完成态徽标
- 统一 `.stage` 纵向步骤式 UI + 顶栏运行按钮 + 上游门禁
- `localStorage` 跨 Agent 传递选定结果（`sv_selected_product/doctor/audience/script/video`）
- 话术 Agent 合规打回重跑闭环
- 选品/选医生/人群 三页第 4 步候选全可选用 + 实时联动下游

## 本地预览（v0.4 起需配后端才有真链路）

### 仅看前端 UI（mock 兜底）
```bash
cd archive/prototype_demo_v0.4
python3 -m http.server 8848
# 主入口：http://localhost:8848/app.html
```
此时 `script.html` 调真实 LLM 会因 backend 不可达而显示错误；`video.html` 的"真实生成"同样不可用。其他纯展示功能（选品/选医生/人群/分发）可正常浏览。

### 完整真链路（需后端）
1. 进入 `backend/` 目录，准备 `.env`（参考 `.env.example`），填入腾讯云密钥（VOD + TTS + LLM）；
2. 把参考动作视频放到 `backend/assets/motion_ref/ref.mp4`；
3. 启动后端：
```bash
cd backend
./run.sh --warmup   # 首次启动建议带 --warmup 预热医生形象库 FileId
```
4. 启动前端静态服务：
```bash
cd archive/prototype_demo_v0.4
python3 -m http.server 8848
# 浏览 http://localhost:8848/app.html
```
后端默认 `127.0.0.1:8000`，前端 `script.html` / `video.html` 写死调这个地址。

## 依赖外部服务
| 能力 | 服务 | 计费 |
|---|---|---|
| LLM 话术生成 | 腾讯云 lkeap / hy3-preview | 按 token |
| 口播合成 (TTS) | 腾讯云语音合成（超自然大模型音色）| 按字符 |
| 动作迁移 (motion_control) | 腾讯云 VOD AIGC (Kling/2.1) | 按视频秒 |
| 视频存储/直传 | 腾讯云 VOD | 按存储/流量 |

## 已知限制
- `prototype/script.html` 第 2 轮（真实 LLM）会重新生成话术；第 1 轮仍是固定 mock V1（演示打回）
- motion_control 输出 8 秒短视频，话术口播 ~17 秒，ffmpeg `-stream_loop` 循环画面以对齐时长
- 字幕烧录依赖 `ffmpeg-full`（含 libass）；默认 `ffmpeg` 自动降级为无字幕画面
- LLM API Key 与 VOD/TTS 密钥**不共用**，需分别在腾讯云控制台申请
