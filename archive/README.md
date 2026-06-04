# 归档索引 · 泛健康营销多 Agent 协同平台 Demo

> 本目录存放各版本 Demo 的只读快照（前端原型 HTML + 后端真链路）。
> 每个版本含「展开目录 + 同名 `.zip`」，详细变更见各自的 `VERSION.md` / `CHANGES.md`。
> **请勿在归档目录内直接修改/开发**；当前开发版本在仓库根的 `prototype/`（前端）与 `backend/`（后端）。

---

## 版本总览

| 版本 | 归档时间 | 类型 | 一句话概括 |
|------|----------|------|-----------|
| **v0.5** | 2026-06-04 | 前端快照 + 后端真链路 | **B站官方投稿真实闭环** + 6 医生专属动作视频强化 + 下线"话术受众侧重" + 阻断性修复 |
| v0.4 | 2026-06-03 | 前端快照 + 后端真链路（**首次引入后端**） | 话术 LLM 真出 + 短视频制作真链路（腾讯云 TTS / VOD 动作迁移 / 字幕烧录） |
| v0.3 | 2026-06-02 | 纯前端原型 | 三个排序/估算类 Agent 第 4 步精排：候选**全部可点击选用**，支持人工改选下传 |
| v0.2 | 2026-06-02 | 纯前端原型 | 新增 `app.html` 流程视图外壳（6 Agent 导航 + iframe）+ 短视频分发 Agent |
| v0.1 | 2026-06-02 | 纯前端原型 | 六环节下游联动链路打通（localStorage 传递 + 上游门禁 + 合规打回重跑） |

> 主线：**选品 → 选医生 → 目标人群 → 话术 → 短视频制作 → 短视频分发**

---

## 目录结构

```
archive/
├─ README.md                       ← 本索引
├─ prototype_demo_v0.1/  + .zip    # 纯前端原型
├─ prototype_demo_v0.2/  + .zip
├─ prototype_demo_v0.3/  + .zip
├─ prototype_demo_v0.4/  + .zip    # 含 VERSION.md（后端真链路说明，源码仍在仓库 backend/）
└─ v0.5/                           # v0.5 起前后端分包归拢
   ├─ prototype_demo_v0.5/  + .zip # 前端 8 个 HTML + VERSION.md
   └─ backend_v0.5_changes/ + .zip # 完整 backend/ 快照（脱敏，无 .env 密钥）+ CHANGES.md
```

---

## 各版本要点

### v0.5（最新）
- **短视频分发 Agent 接入 B站官方 Web 投稿 API（真实发布）**：`preupload → 分片上传 → 合片 → x/vu/web/add/v3 提交`，返回真实 BV 号；其余 4 平台（抖音/快手/视频号/小红书）保持模拟。前端 B站面板为独立常亮区块。
- **短视频制作强化**：6 位医生各配专属参考动作视频，motion_control 按所选医生自动匹配（`<医生中文名>.mp4`）；`/api/doctors` 增 `has_motion_ref`。
- **话术 Agent 收敛**：下线"话术受众侧重"，目标人群直接取自上游人群 Agent 结果。
- **阻断性修复**：话术第 4 步 SSE 的 CRLF 解析、生视频"卡在 75%"（去 `--reload` + 进度爬升）、分发页 B站面板不可见（移出置灰 stage + no-cache）。
- 归档形态变化：v0.5 起前端原型与后端补丁分包，统一归拢到 `v0.5/` 子目录。

### v0.4
- 首个引入后端的版本：**话术第 4 步 + 短视频制作** 从演示动画升级为真实链路。
- 话术接腾讯云 hy3-preview（SSE 流式 + 后端违禁词清洗兜底）；短视频接 TTS（6 医生 6 音色）+ VOD motion_control 动作迁移 + ffmpeg 字幕烧录。
- 三种视频后端模式（`local`/`motion`/`aigc`，`VIDEO_BACKEND` 切换，默认 `motion`）。

### v0.3
- 选品 / 选医生 / 目标人群三个 Agent 的第 4 步精排：从"只能用系统 Top-1"升级为"所有策略下所有候选均可点击选用并下传"，支持运营人工干预改选。

### v0.2
- 新增 `app.html` 流程视图外壳（顶部 6 Agent 流程导航 + 下方 iframe 功能区，切换/高亮/完成态徽标）。
- 新增短视频分发 Agent（`distribute.html`）。

### v0.1
- 六环节闭环链路首版：统一 `.stage` 步骤式 UI、`localStorage` 跨 Agent 传递、上游门禁、话术合规打回重跑、成片多模态合规复审。

---

## 回溯/运行方式

- **仅看前端 UI（任意版本）**：进入对应 `prototype_demo_vX/` 目录，`python3 -m http.server 8848`，浏览 `http://localhost:8848/app.html`（无后端时真实生成/投稿不可用，其余可浏览）。
- **完整真链路（v0.4 起）**：还原 `backend/`（v0.5 用 `v0.5/backend_v0.5_changes/backend/`），按其 `CHANGES.md` / `VERSION.md` 准备 `.env`（复制 `.env.example` 填密钥）后启动后端，再起前端静态服务。详见各版本 `VERSION.md` 的「完整真链路」一节。

> 安全：所有归档均**不含** `.env` 真实密钥（B站 SESSDATA / bili_jct、腾讯云 SecretId 等），仅附脱敏 `.env.example`。
