# 选品 Agent × Agent Runtime 技术路线分析

> 版本：v0.1 / 2026-06-06
> 基线：v1.1.0（短视频制作 Agent 已切换至可灵 Kling 原厂高级对口型）
> 参考：[腾讯云 Agent Runtime 文档（产品 ID 1814）](https://cloud.tencent.com/document/product/1814)
> 配套：《Agent_Runtime_部署与管理_设计说明.md》（同仓库已有的"先合后分"原则）

---

## 0. TL;DR（一页结论）

| 维度 | 结论 |
|---|---|
| **能不能上？** | **可以，且选品 Agent 是最合适的"开胃菜"**：当前还只有前端原型（`prototype/product.html`），后端绿地，不会动到 v1.1 的视频/话术/分发链路。 |
| **要不要全量上？** | **不要。** 推荐**双轨**：业务编排（LangGraph）仍跑在我们自己 FastAPI 进程里，**只把"工具调用"侧的代码执行/浏览器等不可信操作**托管到腾讯云 Agent Runtime 的 **Sandbox**。 |
| **核心匹配点是什么？** | 选品 Agent 要做：① 跑用户上传的 Excel/CSV 做销量分析 ② 抓第三方公开行情/政策（轻量 Web）③ 多步推理 + 工具调用。**②③ 必须沙箱化**，否则在我们 8001 主进程里跑用户脚本/外部浏览器=安全雷区。 |
| **要花钱吗？** | **公测期免费**（截至 2025-11-30，已延期至当前）。商业化后按 Sandbox 实例时长计费，未续费 30 天后**个人数据删除**——这条要写进风险登记。 |
| **要做哪些事？** | 7 件（详见 §6）：申请内测 → 拉 AK/SK → 抽 ToolRegistry → 实现 SandboxAgentExecutor → 选品 Agent 业务节点 → 选品 UI 联调 → 可观测打通。预估 **4–6 个工作日**。 |
| **不做哪些事？** | ❌ 不把整个 Agent 进程搬上去（会绑定厂商、丢失编排灵活性）。❌ 不把 v1.1 的视频/话术链路迁过去（没必要、风险大）。❌ 不依赖它做长期持久化（公测期会删数据）。 |

---

## 1. 腾讯云 Agent Runtime 是什么（必读，否则后面看不懂）

### 1.1 它**不是**什么
- **不是**"托管的 LangGraph"——它**不替你做编排**（决定下一步该调哪个 Agent、是否回环、是否人审）。
- **不是**像 Bedrock AgentCore / Dify 那种把"完整 Agent 应用"打包托管的平台。
- **不是**模型服务（模型走 TokenHub / 我们自己的 LLM 网关）。

### 1.2 它**是**什么——精确定位
官方 FAQ 的原话最准：

> - **Agent 沙箱服务**：基础设施层，提供隔离、安全、可控的任务运行环境（沙箱）。
> - **Agent 运行时**：托管与执行层，为 Agent 提供完整的部署、运维和管理能力。

**翻译成人话**：
- **Sandbox（沙箱）**：一个**毫秒级启动、内核级隔离的微虚机**，里面装好了 Python/Browser/Computer/Ubuntu Desktop 等环境。给它一段代码、一个网址、一条命令，它跑完返回结果，跑完即销毁。
- **Runtime（运行时）**：在 Sandbox 之上多一层"实例编排"——你可以把 Tool（沙箱模板）注册进去，Runtime 帮你做实例池、超时回收、快照暂停/恢复、API Key 管理、审计日志。

> 当前文档体系**绝大部分篇幅都在讲 Sandbox**，Runtime 这一层主要承担"实例生命周期托管"。可以理解为：**Sandbox = 货柜，Runtime = 港口调度系统**。

### 1.3 关键能力清单（与我们选品 Agent 直接相关的）

| 能力 | 选品 Agent 用得上吗 | 我们的用法 |
|---|---|---|
| **Code Interpreter 沙箱**（Python/JS） | ✅✅✅ 核心 | 跑用户 Excel 解析、销量/库存计算、画 matplotlib 图表 |
| **Browser 沙箱** | ✅✅ 高价值 | 抓药监局/集采公开页、米内网/丁香园等开源行情 |
| **Computer / OSWorld 沙箱** | ⏸ Phase 2 | 操作老旧药企 ERP / 经销商系统（无 API）时再上 |
| **文件系统操作 + 外挂存储** | ✅ 必备 | 用户上传的销量表中转、产出报告中转（可挂 COS） |
| **进程级快照（暂停/恢复）** | ⏸ 暂用不到 | 选品场景通常一次跑完，不需要跨小时恢复 |
| **细粒度角色权限** | ✅ 安全基线 | 限制沙箱只能访问我们指定的 COS 路径，禁止其它腾讯云资源 |
| **MCP / E2B SDK 兼容** | ✅ 大幅省事 | 不用学私有协议，**可直接复用 E2B SDK 客户端**，未来换厂商成本低 |
| **100ms 启动 / 数万并发** | ✅ Demo 演示亮点 | 每个用户一次选品对话起一个独立沙箱，跑完销毁 |
| **内核级强隔离（Cube）** | ✅✅✅ **关键安全基石** | 见 §2 |

### 1.4 部署形态——这点最容易被误解
- **不是把 FastAPI 业务进程整体部署到 Runtime**——Runtime 不接管你的 Web 服务。
- **不是 K8s/容器/FaaS**——它是更上层的"沙箱即服务"。
- **正确理解**：你的业务代码（FastAPI + LangGraph）继续跑在自己服务器/容器上（我们就是 162.14.76.209），**只有"需要执行不可信代码或外部浏览操作"的瞬间，调它的 API 借一个沙箱跑一下，结果回收**。

---

## 2. 为什么选品 Agent 是"非用 Agent Runtime 不可"的场景

短视频/话术/分发链路里，我们调用的是**自家熟知的能力**（VOD、Kling、TTS、B 站 API），输入输出都是可控的 JSON。**不需要执行用户提供的代码、也不需要操作浏览器。**

选品 Agent 完全不同。它的核心任务是"基于用户上传的销量数据 + 行业公开信息，推荐主推品 / 制定营销策略"，必然要做：

| 选品 Agent 的真实工作 | 不用沙箱的后果 |
|---|---|
| 1. 解析用户上传的 Excel/CSV/PDF | 用户表里嵌入恶意宏 / `=cmd|...` 公式注入，污染我们主进程 |
| 2. 跑 LLM 生成的 pandas/numpy 代码做销售分析 | LLM 幻觉写出 `os.system("rm -rf /")` —— Vibe Coding 的经典翻车场景 |
| 3. 抓药监局 / 米内网 / 丁香园等公开行情页 | 在我们 162.14.76.209 服务器上直接出网爬取 = 业务网段污染 + 容易被风控 |
| 4. 生成图表 / 中间报告文件 | 文件落在我们主机磁盘上，清理责任、配额、权限全要自己管 |
| 5. 多用户并发跑分析任务 | Web 进程被长任务阻塞，影响 v1.1 视频生产 |

**结论**：选品 Agent 的"工具调用"侧天然就是 Agent Runtime Sandbox 的目标场景（官方应用场景中的 **Vibe Coding + 数据处理/PPT 制作 + Browser Use Agent** 三件套全中），**这不是过度设计，是合规与稳定性的必选项**。

---

## 3. 与我们现有架构的衔接（不破坏 v1.1）

### 3.1 现状回顾
```
FastAPI(162.14.76.209)
 └─ app/
    ├─ orchestrator/graph.py        # LangGraph 编排（视频/分发已稳定）
    ├─ agents/
    │    ├─ script_agent.py         # 话术 Agent（LLM 流式）
    │    └─ bilibili_agent.py       # B 站投稿 Agent
    ├─ kling_avatar.py / kling_base.py
    ├─ tts.py / composer.py / vod_*.py
    └─ ...
prototype/product.html              # 选品 UI 原型（暂无后端对接）
```

### 3.2 引入选品 Agent 后的目标架构

```
┌────────────────── 162.14.76.209（我们自己的进程，不动） ──────────────────┐
│                                                                            │
│  FastAPI / LangGraph 编排器                                                │
│   ├─ 现有：script / video(kling) / bilibili Agent          （保持原样）    │
│   └─ 新增：product_agent.py                                                │
│              ├─ ① 需求理解（纯 LLM）             ← 不走沙箱               │
│              ├─ ② 数据解析（pandas）             ↘                         │
│              ├─ ③ 行情抓取（requests/browser）     借沙箱执行 → 调 AGR    │
│              ├─ ④ 候选产品排序（pandas + 业务规则） ↗                       │
│              └─ ⑤ 结论汇总（LLM）                ← 不走沙箱               │
│                                                                            │
│  新增模块：tool_registry.py / sandbox_executor.py                          │
│   └─ 封装腾讯云 AGR SDK（E2B 兼容）：创建/复用沙箱、传文件、跑代码、收结果 │
│                                                                            │
└─────────────────────┬─────────────────────────────────────────────────────┘
                      │  HTTPS（AK/SK）
                      ▼
        ┌────────── 腾讯云 Agent Runtime（托管） ──────────┐
        │                                                  │
        │  Tool: medmedia-product-py（code-interpreter）   │
        │  Tool: medmedia-product-browser（browser）       │
        │                                                  │
        │  Instance（按需启动，100ms，跑完销毁）            │
        │   ├─ 跑用户 Excel 解析代码                       │
        │   ├─ 跑 LLM 产出的 pandas 分析脚本               │
        │   └─ 跑公开行情数据抓取                          │
        └──────────────────────────────────────────────────┘
                      ▲
                      │  COS（可选，作为大文件中转）
                      ▼
        腾讯云 COS bucket: medmedia-product/{job_id}/...
        （用户上传销量表 / 沙箱产出图表）
```

### 3.3 关键设计约束
1. **编排不上云**：LangGraph 仍跑在我们进程里，避免厂商绑定 + 保留人审 / 回环 / 灰度灵活性（这条是《Agent_Runtime_部署与管理_设计说明.md》§3.D 已经定的原则）。
2. **沙箱实例无状态**：每个 job 起一个新沙箱，跑完销毁，**绝不依赖沙箱内文件做跨 job 持久化**——因为公测期商业化后 30 天会删数据。
3. **大文件走 COS 中转**：用户上传的 Excel、沙箱产出的图表/PDF，都落 COS，沙箱内只拿 presigned URL 读写。
4. **代码 = 数据**：LLM 生成的 pandas 代码 + 我们的固定 runner 模板，通过 API 投给沙箱执行；**业务代码不打镜像、不发布到 AGR**（公测期 Tool 自定义镜像还不够稳）。
5. **统一适配器**：沿用 `adapters/` 思想，写一个 `SandboxExecutor` 接口，先实现 `TencentAGRSandbox`，**保留 `LocalSandbox`（subprocess + tmpdir）作为本地开发回退**。这样断网 / 厂商故障 / 切换厂商时能无缝降级。

---

## 4. 选品 Agent 的内部流水线设计

按"先合后分"原则，**5 个子步骤都在我们 FastAPI 进程内同节点编排**，只有 ②③④ 中的"执行动作"借沙箱：

| 步骤 | 内容 | 在哪跑 | 输入 → 输出 |
|---|---|---|---|
| ① **需求理解** | 用户描述客群/季节/预算等，LLM 抽成结构化条件 | 我们进程（纯 LLM） | text → `BriefSpec(json)` |
| ② **数据解析** | 解析用户上传的销量表，输出标准化 DataFrame 概要 | **沙箱**（pandas） | xlsx/csv → `DataSummary(json)` + 预览图 |
| ③ **行情抓取** | 抓药监局/集采/米内网公开页（仅白名单域名） | **沙箱**（requests/browser） | 关键词 → `MarketSignals(json)` |
| ④ **候选打分** | 按业务规则 + LLM 加权打分，排出 Top N 主推品 | **沙箱**（pandas + 我们注入的规则模板） | ②③ → `Candidates[]` |
| ⑤ **结论汇总** | LLM 把 ④ 的候选品 + 营销建议组织成最终方案 | 我们进程（纯 LLM） | ④ → 最终方案 + Citation |

**为什么 ④ 也放沙箱**：候选打分涉及对用户私有销量表的二次计算 + LLM 生成的加权逻辑，同样有 RCE 风险。

**前端体验**（沿用 v1.1 SSE 模式）：
```
SSE 事件流：
  stage    : {"step":"understand", "msg":"理解需求中…"}
  stage    : {"step":"parse_data", "msg":"沙箱解析销量表…"}
  progress : {"phase":"sandbox", "instance":"sdi-xxx", "percent":40}
  stage    : {"step":"market_scan", "msg":"行情抓取（沙箱浏览器）…"}
  stage    : {"step":"score", "msg":"候选打分…"}
  done     : {"candidates":[…], "report_url":"/api/product/jobs/{id}/report"}
```

---

## 5. 前提条件清单（要先准备好的"票"）

### 5.1 账号与权限
- [ ] **腾讯云子账号**：建一个**专用子账号**`medmedia-agent`，不要复用 root（合规基线）。
- [ ] **AGR 内测申请**：通过文档里的[内测申请问卷](https://cloud.tencent.com/apply/p/fk8k0byfoh)提交，标注"医药营销 Agent · 选品场景 · 数据分析 + 浏览器沙箱"。
  - 公测期免费，但**必须申请才能拿到 API Key**——这是第一个阻塞项。
- [ ] **CAM 策略**：给子账号配置最小化策略：
  - `QcloudAGRFullAccess`（沙箱读写）
  - `QcloudCOSReadOnlyAccess` + 自定义策略限制到指定 bucket 路径
  - **禁止**其它腾讯云资源访问（防沙箱越权拉云硬盘/VPC）
- [ ] **API Key**：在 AGR 控制台创建一对 AK/SK，**只放在云端 `.env`，不入版本库**（沿用 v1.1 已经成熟的密钥管理）。

### 5.2 资源
- [ ] **COS bucket**：`medmedia-product`（华南广州，和我们 162 同 region 减少延迟）。
  - 子路径策略：`uploads/{user}/{date}/` 用户上传；`outputs/{job_id}/` 沙箱产出；7 天自动过期。
  - 启用服务端加密。
- [ ] **VPC / 出网**：沙箱默认 `NetworkMode=SANDBOX`（仅内网），抓行情时改 `NetworkMode=PUBLIC` 并配**白名单域名**（药监局/集采/米内网/丁香园）。
- [ ] **可观测**：CLS 日志服务接 AGR 的审计日志（公测期是否提供日志导出待确认，**第一周要 spike 验证一下**）。

### 5.3 代码层准备
- [ ] **依赖新增**：
  - `agr-sdk-python`（腾讯云官方 SDK，待发布版本号确认）**或**直接用 `e2b`（兼容协议，更成熟）
  - `pandas / openpyxl`（解析销量表，本地也要有，做 fallback）
  - `cos-python-sdk-v5`（COS 客户端）
- [ ] **本地回退实现**：写 `LocalSandbox`（基于 `subprocess + resource limits + tmpdir`），保证断网 / 内测申请没下来时本地能跑通调试。
- [ ] **沙箱代码模板库**：
  - `templates/parse_excel.py.j2` — 解析销量表的标准 runner
  - `templates/market_scan.py.j2` — 行情抓取
  - `templates/score.py.j2` — 候选打分
  - **原则**：模板写死骨架，LLM 只填特定槽位（如打分权重），不允许 LLM 写整段代码——降低注入面。

### 5.4 设计文档与流程
- [ ] **数据流图 + 安全模型**：写一份独立 markdown，标注每个边界上 PII / 销量数据怎么流转、谁加密、保留多久。
- [ ] **风险登记表**：至少登记 3 条：
  - 公测期免费但商业化后 30 天删数据 → 必须自己 COS 兜底，不依赖 AGR 持久化
  - SDK 还在快速迭代 → 用接口适配器隔离，不直调
  - 沙箱出网域名白名单需运维持续维护
- [ ] **预算上限**：商业化前没计费数据，但要在内部约定**单 job 沙箱时长上限 = 300s**（同官方 quickstart 示例），防止失控。

---

## 6. 落地任务拆解（4–6 工作日）

> 按"小步快跑、每天都有可验证产出"原则排：

### Day 1：账号 & 内测申请（半天等待）+ 本地 PoC（半天）
- 提交内测申请、申请 COS / 子账号
- 同时本地用 `e2b-code-interpreter`（开源，免费 100 次/月）跑通"上传 Excel → 沙箱里 pandas 解析 → 返回 JSON"——**先把 SDK 心智模型摸清，AGR API 出来直接换 endpoint**

### Day 2：抽象层（`tool_registry.py` + `sandbox_executor.py`）
- 定义 `SandboxExecutor` 接口：`run_code(code, files=[], net="sandbox"|"public") -> ExecResult`
- 实现 `LocalSandbox`（subprocess + 资源限制）
- 实现 `TencentAGRSandbox` 骨架（先 mock 返回，等 AK/SK 下来直接接通）
- 单元测试覆盖

### Day 3：选品 Agent 业务节点（`agents/product_agent.py`）
- 实现 ①需求理解 / ⑤结论汇总（纯 LLM，复用 `script_agent.py` 的流式套路）
- 实现 ②③④ 三个沙箱步骤的 runner 模板与编排
- 接入 LangGraph 作为新节点（**不影响**视频/分发现有 graph）

### Day 4：API + UI 联调
- 新 API：`POST /api/product/jobs` / SSE `events` / GET `/jobs/{id}/report`
- `prototype/product.html` 接 SSE，复用 v1.1 的进度展示组件
- **`/api/health`** 增加 `agent_runtime_ready` 字段（沿用 v1.1 引入的字段习惯）

### Day 5：可观测 + 安全
- 沙箱实例 ID / 执行耗时 / 失败原因写入 `TaskContext`，前端能看到"当前正在 sandbox sdi-xxx 跑"
- 出网域名白名单生效验证
- LLM 生成代码的**沙箱前预校验**（禁用 `os/socket/subprocess` 等模块的简单 AST 扫描，纵深防御）

### Day 6（缓冲）：端到端真实数据跑通 + 文档
- 找一份真实销量样表（脱敏后）跑端到端
- 更新 README / 写《选品 Agent 使用说明》
- **打 tag `v1.2.0-alpha`**

---

## 7. 关键不确定性 & 风险（要主动暴露）

| 风险 | 影响 | 缓解 |
|---|---|---|
| **内测申请审批节奏未知** | Day 1 就被卡 | 同步做 e2b 本地 PoC，AGR 不来不影响推进 |
| **AGR Python SDK 成熟度** | 接口可能变动 | 用 `SandboxExecutor` 接口隔离，**绝不在业务代码里直接 import 厂商 SDK** |
| **公测期 30 天删数据** | 真上线后丢用户文件 | COS 兜底；沙箱内不放任何不可重建的数据 |
| **出网白名单维护成本** | 行情抓取范围变化时要改配置 | 把白名单做成 `.env` 配置项 + 文档化变更流程 |
| **LLM 生成代码逃逸沙箱限制** | RCE 仍可能在沙箱内造成本 job 数据污染（虽然不影响主机） | AST 预校验 + 沙箱本身的内核级隔离双层保险，且每个 job 沙箱跑完即销毁 |
| **计费透明度** | 商业化后成本不可预测 | 设单 job 时长上限 / 月度配额上限 / 监控告警 |
| **同进程 Agent 与 Sandbox Agent 跨切换** | 编排器需要同时编排两种"风格"的节点 | 用统一 `Agent` Protocol 抽象，编排器看不出区别（《Agent_Runtime_部署与管理_设计说明.md》§5.1 已铺好底子） |

---

## 8. 决策建议（待你拍板）

需要你点头的事项：

1. **方向：是否按本方案推进？**（核心选择：业务编排留在我们进程 + 工具调用进 AGR 沙箱）
2. **范围：先做哪一档？**
   - **A. 完整选品 Agent**（①~⑤ 五步全做）— 4–6 天
   - **B. 最小可演示**（只做 ②数据解析这一步进沙箱，其余先 Mock）— 2 天即可见效
3. **内测申请走谁的账号？** 用我们部署 162 这台所属的腾讯云账号，还是另开账号？
4. **是否同步申请 COS bucket / CLS 日志？**（建议同步申请，免得后面卡）
5. **本次迭代是否冻结视频/分发链路？**（建议冻结 v1.1，所有改动只在新模块；如有 v1.1 bug，单独 hotfix 分支）

---

## 9. 附：与已有方案的关系

- 本文件**不取代**《Agent_Runtime_部署与管理_设计说明.md》，而是它的**腾讯云特定落地版**：
  - 那篇定的是"先合后分、按需拆分"的演进原则
  - 这篇定的是"选品 Agent 这一步具体怎么落到腾讯云 AGR"
- v1.1 视频/话术链路**完全不动**，本次迭代是**纯增量**。
- 后续若 v1.3 想把视频生成的 ffmpeg 烧字幕步骤也搬到沙箱（避免在主机上跑 ffmpeg），技术路径与本文一致——**接口抽象一次，多场景复用**。
