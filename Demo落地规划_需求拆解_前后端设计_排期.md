# 医药/泛健康营销 多Agent平台 — 可运行 Demo 落地规划

> 版本：v0.1 | 日期：2026-06-01
> 配套文档：《医药营销多Agent协同平台_技术路线方案.md》
> 本文目标：把技术路线落到一个**可运行、可演示、可迭代**的 Demo，含需求拆解、前端原型、后端设计、工作计划与排期。

---

## 0. Demo 定位与边界（先对齐，避免做大）

**一句话定位**：用真实可跑的代码，演示「输入一个产品 → 自动匹配医生 → 圈定人群 → 生成合规话术 → 产出短视频脚本/数字人成片 → 人审通过 → 模拟分发」的**端到端多 Agent 流水线**。

**Demo 做什么（In Scope）**
- 5 个 Agent + 编排器 + 合规审查全链路真实跑通。
- LLM 真实调用（话术/匹配理由/人群画像）。
- 数据用**种子数据**（预置产品库、医生库、人群标签库，JSON/SQLite）。
- 前端控制台：可视化流水线、查看每个 Agent 产出、人工确认卡点。
- Human-in-the-loop：选医生确认、话术终审、发布前终审。

**Demo 不做什么（Out of Scope，用 Mock 替代）**
- ❌ 真实对接巨量/磁力/微信投放 API（用 Mock 返回人群包/投放结果，预留接口）。
- ❌ 真实数字人视频渲染（默认产出**脚本+分镜+TTS 音频**；视频生成做成可选开关，接 1 个真实数字人 API 或用占位视频）。
- ❌ 真实发布到抖音/快手（Mock 发布，返回模拟链接与数据）。
- ❌ 完整权限/多租户/计费。

> 原则：**真链路、假数据、可替换**。所有外部依赖走适配器接口，Demo 用 Mock 实现，未来换真实实现不动主流程。

---

## 1. 需求分析与功能拆解

### 1.1 用户角色

| 角色 | 诉求 |
|---|---|
| 营销运营 | 输入产品，一键生成营销内容方案，审核确认 |
| 合规审核员 | 审核话术/成片，放行或打回 |
| 演示者/决策者 | 直观看到多 Agent 协作的全过程与产出 |

### 1.2 核心用户故事（按优先级）

| 编号 | 用户故事 | 优先级 |
|---|---|---|
| US-1 | 作为运营，我能录入/选择一个产品并发起一次营销任务 | P0 |
| US-2 | 系统自动匹配 Top-N 医生并给出匹配理由，我可确认/调整 | P0 |
| US-3 | 系统自动生成目标人群画像与（模拟）人群包 | P0 |
| US-4 | 系统为"医生×人群"生成营销话术，并标注合规风险 | P0 |
| US-5 | 合规 Agent 对话术做检测，不通过自动打回重写 | P0 |
| US-6 | 系统把话术转成短视频脚本+分镜，并产出 TTS/数字人成片 | P0 |
| US-7 | 成片经人工终审后，模拟一键分发到多平台并返回数据 | P1 |
| US-8 | 我能在一个控制台看到整条流水线的实时状态与每步产出 | P0 |
| US-9 | 任务可断点续跑、单节点重跑、查看历史版本 | P1 |
| US-10 | 效果数据回流，给医生/人群/话术打分（模拟） | P2 |

### 1.3 功能模块拆解

```
Demo
├── 任务编排（Pipeline Orchestrator）           [P0]
│   ├── 状态机流转 / 节点重试 / 人审关卡
│   └── 任务上下文 TaskContext 传递
├── Agent ①选医生                               [P0]
│   ├── 召回（标签过滤+向量检索）
│   └── LLM 精排 + 匹配理由
├── Agent ②目标人群                             [P0]
│   ├── 画像生成（LLM）
│   └── 标签映射 → Mock 人群包
├── Agent ③话术                                 [P0]
│   ├── RAG 检索宣称口径
│   └── 分人群话术生成
├── Agent ④短视频制作                           [P0]
│   ├── 脚本结构化 → 分镜
│   ├── TTS 配音（真实/可选）
│   └── 数字人合成（Mock/可选接 1 个 API）
├── Agent ⑤合规审查（横切）                     [P0]
│   ├── 违禁词规则引擎
│   └── LLM 语义审核 + 分级处置
├── 分发（Mock）                                 [P1]
└── 数据中台（种子数据）                         [P0]
    └── 产品/医生/人群标签/话术/素材/效果
```

### 1.4 关键非功能需求

- **可演示性**：流水线状态实时可见（WebSocket/SSE 推送进度）。
- **可替换性**：外部能力（视频生成、投放、发布）走适配器接口。
- **合规优先**：违禁词库可配置、审核可追溯。
- **可重跑**：任务/节点状态持久化（SQLite）。

---

## 2. 前端原型设计

### 2.1 技术选型

| 项 | 选型 | 理由 |
|---|---|---|
| 框架 | **React + Vite + TypeScript** | 生态成熟、起项目快 |
| UI 库 | Ant Design | 表单/表格/步骤条/抽屉齐全，适合控制台 |
| 状态管理 | Zustand / React Query | 轻量；React Query 管服务端状态 |
| 流程可视化 | React Flow | 直观展示流水线 DAG 与节点状态 |
| 实时推送 | SSE（EventSource） | 推送 Agent 进度，比 WS 简单 |

### 2.2 页面结构（4 个核心页面）

```
1. 任务发起页 /tasks/new
   - 产品选择/录入表单（品类、卖点、合规宣称口径、目标平台）
   - "发起营销任务"按钮 → 创建 task，跳转流水线页

2. 流水线工作台 /tasks/:id   ★核心页面★
   - 顶部：步骤条（选医生→人群→话术→审核→视频→发布）
   - 左侧：React Flow 流水线图，节点实时变色（待处理/进行中/完成/打回）
   - 右侧：当前节点详情面板（Tab 切换查看各 Agent 产出）
     · 选医生：候选医生卡片列表 + 匹配分 + 理由 +【确认】
     · 人群：画像图表 + 标签列表 + 模拟人群包规模
     · 话术：分人群话术卡片 + 合规风险高亮标注
     · 审核：合规结果（通过/警告/打回）+ 违规点列表 +【放行/打回】
     · 视频：脚本+分镜表 + 音频播放器 + 成片预览
     · 发布：平台勾选 + 模拟发布结果与数据
   - 底部：实时日志流（Agent 执行 Trace）

3. 资源管理页 /resources
   - 产品库 / 医生库 / 人群标签库 / 话术库（表格 CRUD，种子数据）

4. 任务列表页 /tasks
   - 历史任务、状态、可进入续跑/查看
```

### 2.3 关键交互（流水线工作台）

```
[发起任务]
   │ SSE 连接建立
   ▼
节点①进行中(蓝) ──完成──► 弹出"确认医生"抽屉 ──用户确认──►
节点②进行中 ──完成──► 自动继续
节点③进行中 ──完成──► 节点⑤审核
   ├─ 通过(绿) ──► 继续
   └─ 打回(红) ──► 节点③重跑（动画回退）
节点④进行中 ──完成──► 节点⑤审核 ──► 人工终审抽屉
   └─ 放行 ──► 节点[发布] ──► 展示模拟数据
```

### 2.4 原型组件清单

- `TaskCreateForm`、`PipelineGraph`(React Flow)、`StepBar`、`AgentOutputPanel`
- `DoctorCard`、`AudienceProfile`(图表)、`ScriptCard`(含风险高亮)、`ComplianceResult`
- `StoryboardTable`、`VideoPreview`、`LogStream`、`ConfirmDrawer`

> P0 阶段可先用 **低保真**（线框）跑通交互，再补样式。本文交付线框级原型说明，可据此直接开发。

---

## 3. 后端服务开发内容设计

### 3.1 技术选型

| 项 | 选型 | 理由 |
|---|---|---|
| 语言/框架 | **Python + FastAPI** | 异步、Agent 生态最好、起服务快 |
| Agent 编排 | **LangGraph** | 状态机、人审关卡、回环、可观测 |
| LLM 接入 | 统一 LLM 网关（OpenAI 兼容/通义/混元） | 可切换、Demo 用一个即可 |
| 向量检索 | **ChromaDB**（嵌入式，免运维）/ pgvector | Demo 轻量优先 Chroma |
| 数据库 | **SQLite**（Demo）→ PostgreSQL（生产） | 零部署 |
| 任务/实时 | FastAPI BackgroundTask + SSE | Demo 够用；复杂用 Temporal |
| TTS/视频 | 适配器接口 + 1 个真实实现 + Mock | 可替换 |

### 3.2 服务模块与目录结构

```
backend/
├── main.py                      # FastAPI 入口 + 路由
├── orchestrator/
│   ├── graph.py                 # LangGraph 流水线定义（DAG）
│   ├── state.py                 # TaskContext 状态模型
│   └── nodes.py                 # 节点=Agent 调用封装
├── agents/
│   ├── doctor_matcher.py        # ①选医生：召回+精排
│   ├── audience_selector.py     # ②目标人群
│   ├── script_writer.py         # ③话术
│   ├── video_producer.py        # ④短视频（脚本/分镜/TTS/合成）
│   └── compliance_checker.py    # ⑤合规审查（横切）
├── adapters/                    # 外部能力适配器（可替换）
│   ├── llm.py                   # LLM 网关
│   ├── tts.py                   # TTS：真实 + Mock
│   ├── video_gen.py             # 数字人：真实 + Mock
│   ├── audience_dmp.py          # 人群包：Mock（预留巨量/磁力/微信）
│   └── publisher.py             # 发布：Mock（预留各平台）
├── core/
│   ├── compliance_rules.py      # 违禁词库 + 规则引擎
│   ├── rag.py                   # 向量检索
│   └── events.py                # SSE 事件总线
├── data/
│   ├── seed/                    # 种子数据 JSON（产品/医生/标签/话术）
│   └── db.sqlite
└── models/                      # Pydantic 数据模型
```

### 3.3 编排状态模型（TaskContext）

```python
class TaskContext(BaseModel):
    task_id: str
    product: Product
    status: Literal["created","matching","audience","scripting",
                    "reviewing","producing","review_final","publishing","done","failed"]
    candidate_doctors: list[DoctorMatch] = []
    selected_doctor: Doctor | None = None
    audience: AudienceProfile | None = None
    scripts: list[Script] = []           # 分人群话术（含版本）
    compliance: list[ComplianceResult] = []
    storyboard: Storyboard | None = None
    video_assets: VideoAsset | None = None
    publish_result: PublishResult | None = None
    human_gates: dict[str, bool] = {}    # 各人审关卡状态
    retries: dict[str, int] = {}
```

### 3.4 LangGraph 流水线（节点与边）

```
START
 → doctor_match        (产出候选 → 人审 gate: select_doctor)
 → audience_select
 → script_write
 → compliance_check(script)
      ├─ pass  → video_produce
      └─ fail  → script_write   (条件边，回环，retries++)
 → compliance_check(video)
      → human gate: final_review
 → publish (mock)
 → END
```
- 人审关卡用 LangGraph `interrupt`/检查点实现：流程暂停，等前端确认后 resume。
- 每个节点执行前后通过 `events.py` 推送 SSE 进度。

### 3.5 各 Agent 实现要点（Demo 级）

| Agent | Demo 实现 | 真实化预留 |
|---|---|---|
| ①选医生 | 标签过滤 + Chroma 向量相似度召回 → LLM 精排打分+理由 | 接真实医生 CRM、效果回流模型 |
| ②人群 | LLM 生成画像 → 映射到内置标签字典 → Mock 人群包规模 | 接巨量/磁力/微信 DMP |
| ③话术 | RAG 检索宣称口径 + 医生风格 few-shot → 分人群生成 | 接真实话术库、风格模型 |
| ④视频 | 话术→脚本→分镜(LLM) → TTS 真实配音 → 视频 Mock/可选API | 接数字人渲染、云剪辑 |
| ⑤合规 | 违禁词正则 + LLM 语义审核 → 分级(pass/warn/block) | 多模态审核(ASR+OCR)、规则热更新 |

### 3.6 核心 API（REST + SSE）

```
POST  /api/tasks                 创建任务（传产品）→ 返回 task_id
GET   /api/tasks                 任务列表
GET   /api/tasks/{id}            任务详情（含各 Agent 产出）
GET   /api/tasks/{id}/stream     SSE：流水线实时进度推送
POST  /api/tasks/{id}/confirm-doctor   确认选定医生（人审 gate）
POST  /api/tasks/{id}/review     话术/成片放行或打回
POST  /api/tasks/{id}/rerun/{node}     单节点重跑
POST  /api/tasks/{id}/publish    模拟分发

# 资源管理
GET/POST/PUT  /api/products | /api/doctors | /api/audience-tags
GET   /api/compliance/rules      违禁词规则查看/配置
```

### 3.7 种子数据（Demo 必备）

- **产品**：3-5 个（如某益生菌、某护眼保健品、某口腔护理——选监管较松品类，规避高风险）。
- **医生**：15-20 个，覆盖多科室+标签+风格+粉丝画像。
- **人群标签字典**：模拟平台标签体系（年龄/性别/地域/兴趣/行为）。
- **合规宣称口径库**：每个产品的"可说/不可说"清单。
- **违禁词库**：《广告法》绝对化用语 + 三品一械违禁宣称。

---

## 4. 工作计划与排期

### 4.1 里程碑

| 里程碑 | 内容 | 验收标准 |
|---|---|---|
| **M1 骨架可跑** | 项目脚手架 + 数据/种子 + 1 个 Agent 通 | 前后端联调，能创建任务并看到①产出 |
| **M2 链路打通** | 5 Agent + 编排 + 合规回环（后端为主） | 命令行/接口能跑完整 pipeline |
| **M3 控制台可视** | 前端流水线工作台 + SSE + 人审关卡 | 浏览器端完整演示 ①→⑤ |
| **M4 视频与分发** | TTS/数字人 + Mock 发布 + 数据回流 | 产出成片并模拟分发 |
| **M5 打磨演示** | 样式/案例/话术/异常处理 | 可对外完整 Demo |

### 4.2 详细任务与工时（按 1 人全栈估算；2 人可并行约减半）

| # | 任务 | 模块 | 工时(人天) | 依赖 | 里程碑 |
|---|---|---|---|---|---|
| T1 | 项目脚手架（前后端、CI、目录） | 全栈 | 1.5 | - | M1 |
| T2 | 数据模型 + SQLite + 种子数据 | 后端 | 2 | T1 | M1 |
| T3 | LLM 网关 + 适配器接口骨架 | 后端 | 1.5 | T1 | M1 |
| T4 | ①选医生 Agent（召回+精排+RAG） | 后端 | 2.5 | T2,T3 | M1 |
| T5 | ②人群 Agent（画像+标签映射+Mock包） | 后端 | 2 | T3 | M2 |
| T6 | ③话术 Agent（RAG+分人群生成） | 后端 | 2.5 | T3 | M2 |
| T7 | ⑤合规 Agent（规则引擎+LLM审核） | 后端 | 2.5 | T3 | M2 |
| T8 | ④视频 Agent（脚本/分镜+TTS+视频Mock） | 后端 | 3 | T6 | M2/M4 |
| T9 | LangGraph 编排 + 人审关卡 + SSE 事件 | 后端 | 3 | T4-T8 | M2 |
| T10 | 前端脚手架 + 路由 + 任务发起页 | 前端 | 2 | T1 | M3 |
| T11 | 流水线工作台（React Flow + 步骤条 + 详情面板） | 前端 | 4 | T9,T10 | M3 |
| T12 | SSE 实时进度 + 人审抽屉交互 | 前端 | 2.5 | T11 | M3 |
| T13 | 各 Agent 产出展示组件（医生卡/画像/话术/审核/分镜/视频） | 前端 | 4 | T11 | M3/M4 |
| T14 | 资源管理页（CRUD） | 前端 | 2 | T10 | M4 |
| T15 | Mock 发布 + 效果数据回流展示 | 全栈 | 2 | T9 | M4 |
| T16 | （可选）接 1 个真实数字人/视频 API | 后端 | 2 | T8 | M4 |
| T17 | 联调、异常处理、演示话术、样式打磨 | 全栈 | 3 | 全部 | M5 |
| T18 | 文档（运行说明/演示脚本） | 全栈 | 1 | 全部 | M5 |

**合计：约 47 人天**（含可选项 T16）。

### 4.3 排期（甘特概览）

**方案 A：1 人全栈，约 6-7 周**
```
周1  M1: T1 T2 T3 T4                骨架+数据+选医生
周2  M2: T5 T6 T7                   人群+话术+合规
周3  M2: T8 T9                      视频+编排打通(后端跑通全链路)
周4  M3: T10 T11                    前端工作台
周5  M3: T12 T13                    实时+产出展示
周6  M4: T14 T15 T16                资源页+分发+(可选真实视频)
周7  M5: T17 T18                    打磨+文档+演示
```

**方案 B：2 人（1 后端 + 1 前端），约 3.5-4 周**
```
周1  后端 T1-T4 | 前端 T1(配合)+T10
周2  后端 T5-T8 | 前端 T11
周3  后端 T9+T15 | 前端 T12 T13
周4  后端 T16 | 前端 T14 + 联调 T17 T18
```

### 4.4 关键路径与风险

| 风险 | 影响 | 对策 |
|---|---|---|
| LangGraph 人审关卡(interrupt+resume)首次踩坑 | 中 | 第 3 周预留 0.5 天调研；先用简化状态机兜底 |
| 真实视频 API 接入耗时/费用 | 中 | 设为可选(T16)，默认 Mock+TTS，不阻塞主链路 |
| 合规规则覆盖不全 | 中 | Demo 先覆盖典型违禁词，规则做成可配置 |
| LLM 输出不稳定 | 低 | 强结构化输出(JSON Schema)+ 重试 + 兜底模板 |

---

## 5. 验收 Demo 演示剧本（M5 产出）

1. 运营录入产品「某益生菌」→ 发起任务。
2. 工作台流水线开始流转，①选出 3 位匹配医生 + 理由 → 运营确认 1 位。
3. ②自动生成目标人群画像（如"30-45岁、关注肠道健康的女性"）+ 模拟人群包 50万。
4. ③生成 2 套分人群话术，合规风险点高亮。
5. ⑤合规审查：第 1 版含违禁词被打回 → 自动重写 → 第 2 版通过。
6. ④生成脚本+分镜+TTS 配音（+可选数字人成片）。
7. 人工终审放行 → 模拟分发到抖音/视频号 → 返回模拟播放/互动数据。
8. 全程在控制台可见实时进度与每步产出。

---

## 6. 下一步

我可以立刻开始落地，建议从 **M1（项目脚手架 + 种子数据 + ①选医生 Agent + 最小前端）** 开始。
请确认：
1. **Demo 产品品类**用哪几个（建议先用监管较松的：益生菌/护眼/口腔护理）？
2. **LLM** 用哪个（通义/混元/DeepSeek/OpenAI 兼容）？需提供可用的 API Key/网关。
3. 视频生成 **T16 是否纳入首版**（否则默认 TTS + 占位视频）。
4. 推进节奏按 **方案 A（1人）还是 B（2人）**？

确认后我直接搭骨架、跑通 M1。
