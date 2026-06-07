# 开源 Sandbox 选型调研报告（替代腾讯云 AGR）

> 版本：v0.1 / 2026-06-06
> 触发：腾讯云 Agent Runtime 仍在内测，PoC 阶段不可控
> 目标：为「选品 Agent」找一个**今天就能本地+云端跑起来**的开源 Sandbox 方案
> 配套：《选品Agent_AgentRuntime技术路线分析.md》§5 SDK 章节的开源替代

---

## 0. TL;DR（一页结论）

| 维度 | 结论 |
|---|---|
| **第一选择（M1 PoC + 短期生产）** | **`vndee/llm-sandbox`**（MIT，Docker 后端）— 今天在 162 直接装能跑，跟我们 v1.1 已有的 Docker 完美契合 |
| **第二选择（同时要"浏览器抓行情"）** | **`agent-infra/sandbox`**（Apache-2.0，All-in-One Docker 镜像）— 浏览器+Jupyter+File 统一文件系统，**选品场景"抓+清洗"零搬运** |
| **未来可选（需更强隔离 + 已有 KVM 主机）** | **`microsandbox`**（Apache-2.0，microVM/libkrun）— 100ms 启动 + 内核级隔离，但需 KVM 嵌套虚拟化 |
| **暂不推荐** | **Daytona**（AGPL-3.0 对 SaaS 有传染风险）、**E2B 自托管**（需 Terraform + Supabase，门槛过高）、**SkyPilot Sandbox**（作者自己承认 POC、有会话间数据泄露隐患） |
| **PoC 落地策略** | **`llm-sandbox` 跑 ②④（pandas）+ `agent-infra/sandbox` 跑 ③（浏览器抓行情）**——两套都是 Docker，互不打架；统一在 `SandboxExecutor` 接口后面屏蔽差异 |
| **何时切回腾讯云 AGR** | 内测拿到 AK/SK 后做适配器实现，**编排代码零改动**——这就是为什么要走"接口抽象"的初衷 |

---

## 1. 调研范围 & 评分维度

按"选品 Agent"的真实需求（见 §2）筛选了 GitHub 上的主流候选，每个项目用同一组维度打分：

| 维度 | 权重 | 说明 |
|---|---|---|
| **许可证商用友好** | ⭐⭐⭐ | 优先 MIT / Apache-2.0；AGPL 慎选 |
| **可自托管** | ⭐⭐⭐ | M1 阶段必须能在 162 上跑，不能强依赖云 SaaS |
| **部署成本** | ⭐⭐⭐ | docker run 一行 vs 要 Terraform/K8s/Supabase 的差异 |
| **Python + pandas 原生** | ⭐⭐⭐ | 选品 ②④ 的核心 |
| **浏览器能力** | ⭐⭐ | 选品 ③ 抓药监局/集采/米内网公开行情 |
| **网络/资源隔离开关** | ⭐⭐⭐ | "用户文件代码"必须能 `network=none + read_only + cap_drop=ALL` |
| **多并发/容器池** | ⭐⭐ | 想做多用户演示就必须有 |
| **与 LangChain/LangGraph 集成** | ⭐⭐ | 与 v1.1 现有编排无缝衔接 |
| **活跃度** | ⭐⭐ | star + commit 频率 + release 节奏 |

---

## 2. 选品 Agent 对 Sandbox 的真实需求（复述）

| 子步骤 | 是否上沙箱 | 具体动作 | 对沙箱的硬要求 |
|---|---|---|---|
| ① 需求理解 | 否（纯 LLM） | — | — |
| ② 数据解析 | **是** | pandas/openpyxl 解析用户上传 Excel | Python + 文件双向传输 + 内存隔离 |
| ③ 行情抓取 | **是** | 抓药监局/集采/米内网公开页 | 出网控制（白名单）+ headless 浏览器优先 |
| ④ 候选打分 | **是** | LLM 生成的 pandas 加权计算 | Python + 状态保持（多步骤复用 DataFrame） |
| ⑤ 结论汇总 | 否（纯 LLM） | — | — |

**核心约束**：①PoC 阶段要"今天能跑"；②不能引入比 v1.1 当前部署（单台 162 + Docker + nginx）更重的运维栈；③许可证不能把我们将来对外服务/SaaS 化的路堵死。

---

## 3. 候选项目完整对比矩阵

### 3.1 开源可自托管项目（按推荐度排序）

| 项目 | License | Stars | 最近 Release | 后端 | 启动延迟 | Python | 浏览器 | 安全隔离 | 部署成本 | 推荐度 |
|---|---|---|---|---|---|---|---|---|---|---|
| **vndee/llm-sandbox** | **MIT** | 1.1k | 0.3.39 / 2026-04-20 | Docker / Podman / K8s | 数百 ms（池化 <100 ms） | ⭐⭐⭐⭐⭐（IPython kernel + 池化） | ❌ | ⭐⭐⭐⭐（network=none/read_only/cap_drop/mem/cpu 全开关） | ⭐⭐⭐⭐⭐（`pip install` 一行） | 🥇 **首选** |
| **agent-infra/sandbox** | **Apache-2.0** | 4.9k | v1.9.3 / 2026-05-29 | Docker / K8s | 数秒（全栈启动） | ⭐⭐⭐⭐（Jupyter） | ⭐⭐⭐⭐⭐（VNC + CDP + Playwright） | ⭐⭐⭐（要 `seccomp=unconfined`，多租户需 gVisor） | ⭐⭐⭐⭐（docker run 一行，但需 2Gi shm） | 🥈 **抓行情首选** |
| **microsandbox** | **Apache-2.0** | 6.4k | v0.5.5 / 2026-06-05 | **libkrun microVM** | **<100 ms** | ⭐⭐⭐⭐ | ❌ | ⭐⭐⭐⭐⭐（内核级强隔离） | ⭐⭐（需 KVM 嵌套虚拟化，云主机可能不支持） | 🥉 备选（看 162 是否有 KVM） |
| **Daytona** | **AGPL-3.0** ⚠️ | 72.5k | v0.184.0 / 2026-06-03 | OCI/Docker + Snapshot | **<90 ms** | ⭐⭐⭐⭐⭐ | ⭐⭐⭐⭐（Computer Use） | ⭐⭐⭐⭐⭐ | ⭐⭐（PG + Redis + 3 plane，重平台） | ⚠️ **AGPL 风险，不推荐对外 SaaS** |
| **E2B 自托管** | Apache-2.0 | 12.5k | e2b@2.28.0 / 2026-06-06 | Firecracker microVM | <100 ms（SaaS 端实测 0.7s） | ⭐⭐⭐⭐⭐ | ⭐⭐⭐⭐ | ⭐⭐⭐⭐⭐ | ❌（要 Terraform + AWS/GCP + Supabase + microVM 运维） | ⛔ PoC 门槛过高 |
| **SkyPilot Code Sandbox** | Apache-2.0（POC） | 几百 | — | SkyPilot + llm-sandbox | 0.28s（实测胜 E2B 2.6×） | ⭐⭐⭐⭐ | ❌ | ⭐⭐（**作者承认有会话间数据泄露**） | ⭐⭐⭐ | ⛔ 作者自己说不是生产就绪 |

### 3.2 闭源 / SaaS（仅作背景对比，不进我们选型）

| 项目 | 模式 | 备注 |
|---|---|---|
| **E2B 商业云** | SaaS | 行业标杆，但要钱、要出网；我们 PoC 不依赖 |
| **Modal Sandboxes** | SaaS | 闭源 |
| **Vercel Sandbox** / **Cloudflare Sandboxes** | SaaS | 闭源 + 边缘场景 |
| **Runloop / Sprites / Blaxel / Beam** | SaaS（部分有 BYOC） | 闭源主体 |
| **Northflank Sandboxes** | BYOC | 闭源但能跑在自己云上 |

---

## 4. Top 3 头部项目深度评估

### 4.1 🥇 vndee/llm-sandbox（首选）

**一句话**：MIT 许可、Python 一等公民、Docker 后端、容器池预热、安全开关齐全——和我们 162 上"FastAPI + Docker"的栈天然契合。

**关键能力**（直接对应选品需求）：
- `InteractiveSandboxSession(kernel_type="ipython")`：**IPython kernel 保留状态**，多步 pandas 分析复用 DataFrame，对应选品 ②④
- `ArtifactSandboxSession`：自动捕获 matplotlib 产物，base64 回传，对应"给候选画雷达图/销量趋势图"
- `ContainerPool`：预热 + 复用 + 健康检查，多用户并发演示无压力
- 网络隔离一行开关：`SANDBOX_NETWORK_MODE=none` + `read_only=true` + `cap_drop=ALL`
- LangChain `BaseTool` 示例齐全，与 v1.1 现有 LangGraph 编排无缝挂

**自托管资源建议**（基于官方示例）：
```python
from llm_sandbox.pool import create_pool_manager, PoolConfig
pool = create_pool_manager(
    backend="docker",
    config=PoolConfig(
        max_pool_size=8, min_pool_size=2,
        idle_timeout=300.0, max_container_uses=50,
        enable_prewarming=True,
    ),
    lang="python",
    libraries=["pandas", "openpyxl", "numpy", "matplotlib"],
)
```

**弱项**：
- ❌ 无浏览器能力——抓行情需要另一个工具
- ❌ 不是 microVM——隔离强度低于 microsandbox/E2B（但对**内部业务**已足够）
- ⚠️ K8s 后端时 `SANDBOX_*` 安全配置不生效，需用 `pod_manifest`

### 4.2 🥈 agent-infra/sandbox（补行情抓取这一刀）

**一句话**：Apache-2.0、All-in-One Docker 镜像、**浏览器+Jupyter+File 统一文件系统**——这是它区别于其它"单一用途沙箱"的核心卖点，刚好命中"抓→清洗"链路。

**关键能力**：
- **统一文件系统**：浏览器抓到的 HTML/CSV/JSON，下一行 `jupyter.execute_code` 里 pandas 直接 `pd.read_csv(...)`，零搬运
- **完整浏览器栈**：VNC 可视、CDP 程控、Playwright 兼容、`PROXY_SERVER` 代理池预留
- **Jupyter kernel**：状态保持的 Python 执行
- **MCP 原生**：browser/file/shell/markitdown 四个 server 内置
- **预置中国大陆镜像**（火山引擎 vefaas）：拉取快

**资源消耗**（基于官方推荐）：
- K8s 单 Pod：1 CPU / 2 Gi 内存
- Docker：`shm_size: 2gb`（浏览器需要）
- 4C8G 实测并发：仅 Jupyter 6–8 个 / 含浏览器 3–4 个 / 重 SPA 浏览器 2–3 个

**弱项**：
- ⚠️ 需要 `seccomp=unconfined`——单机用没问题，多租户需配 gVisor 加固
- ⚠️ 维护贡献者较少（4.9k star 但 117 commits）——长期稳定性需观察 1–2 个季度
- ⚠️ **不兼容 E2B 协议**——是自有 REST + SDK 体系（但有官方 Python/TS/Go SDK）

### 4.3 🥉 microsandbox（隔离强度最高，但门槛要算清）

**一句话**：Apache-2.0、**libkrun microVM 真硬件级隔离**、100ms 启动、OCI 兼容镜像、密钥不进入 VM——和腾讯云 AGR 的 Cube 微虚机最像。

**关键能力**：
- 硬件级隔离（libkrun + KVM/HVF）
- 100 ms 平均冷启动
- 嵌入式 SDK，**无需 server、无需 daemon**
- "Unexploitable secret keys that never enter the VM"——安全卖点突出
- OCI 兼容：直接复用 Docker Hub / GHCR 镜像

**致命门槛**：
- ⚠️ **要求 Linux 主机启用 KVM**，云主机需选支持嵌套虚拟化的实例规格
- ⚠️ macOS 仅 Apple Silicon 支持
- ⚠️ 仍是 Beta，README 明确写 "Expect breaking changes"

**对 162 的具体影响**：得先确认腾讯云的这台机型是否支持嵌套虚拟化（多数标准 CVM 默认不开）。如不支持，就只能换 microsandbox 跑在本机开发用，生产仍要回到 Docker 路线。

---

## 5. 不推荐的项目 & 理由（避坑）

### 5.1 Daytona — AGPL-3.0 是最大隐患
- 技术上几乎完美：72.5k star、90ms 启动、Snapshot 持久化、MCP 原生、完整生态
- **但 AGPL-3.0 对网络服务有传染性**：把它嵌进我们对外的医药营销 SaaS，可能被要求开源整个上层服务
- 想用得买商业 license（联系 Daytona 销售），或永久绑定他们的托管版 `app.daytona.io`
- **结论**：技术可用，**合规风险与我们的商业模式冲突**——pass

### 5.2 E2B 自托管 — 门槛太高
- 自托管要 Terraform + AWS/GCP + Supabase + microVM 运维——三人小队一周搭不完
- 商业云版本好用，但**收钱 + 要出网 + 数据出境**——医药数据合规会卡
- **结论**：PoC 期不选；商业云作为"对照实验"的标杆参考即可

### 5.3 SkyPilot Code Sandbox — 作者自己说是 POC
- 性能数据漂亮（0.28s 实测胜 E2B 2.6×）
- **但作者明确写："会话隔离存在安全隐患，用户间代码执行的数据可能泄露"**
- 缺审计日志、细粒度权限、CI/CD 集成
- **结论**：作演示可以，对**多用户的医药营销场景绝不能用**

---

## 6. 最终选型方案（落地路线）

### 6.1 PoC 阶段（M1，今天就开干）

```
┌──────── 162.14.76.209（不动 v1.1）────────┐
│  FastAPI / LangGraph 编排器                │
│    新增：product_agent.py                  │
│    新增：sandbox_executor.py（统一接口）   │
│       ├─ LocalSandbox（本地 subprocess）   │ ← 离线兜底
│       ├─ LLMSandboxExecutor                │ ← ②④ pandas 走它
│       └─ AgentInfraSandboxExecutor         │ ← ③ 行情抓取走它
└────────────────┬────────────────┬─────────┘
                 │ Docker SDK     │ HTTP :8080
                 ▼                ▼
        llm-sandbox 容器池   agent-infra/sandbox 容器
        （预装 pandas）      （Jupyter + Browser）
```

**为什么是这两个组合而不是单一选 agent-infra**：
- `llm-sandbox` **更轻、更快、池化省钱**——选品 ②④ 的纯计算用它，单实例 ~300 MB
- `agent-infra` 全栈但重——只在需要浏览器抓行情的 ③ 用，按需启停
- 两个都是 Docker，互不打架；统一在 `SandboxExecutor` 接口后面屏蔽差异

### 6.2 适配器接口（保持厂商无关）

```python
# backend/app/sandbox/executor.py
from typing import Protocol, Literal

class SandboxExecutor(Protocol):
    """统一接口：未来切 AGR / E2B / microsandbox 编排代码零改动。"""
    backend: str  # "local" | "llm-sandbox" | "agent-infra" | "tencent-agr" | "e2b"

    def run_python(
        self, code: str, *,
        files: list[tuple[str, bytes]] = (),
        libraries: list[str] = (),
        network: Literal["none", "egress_whitelist", "public"] = "none",
        mem_limit: str = "2g", cpus: float = 1.0, timeout_sec: int = 300,
    ) -> "ExecResult": ...

    def open_browser(
        self, url: str, *, allowed_domains: list[str],
    ) -> "BrowserHandle": ...
```

**好处**：
- `llm-sandbox` / `agent-infra` 各自实现一套，编排只看接口
- 未来腾讯云 AGR 内测下来 → 写第三个实现，**业务代码一行不改**
- 离线/CI 跑测试 → `LocalSandbox`（基于 `subprocess + tmpdir + resource limits`）

### 6.3 排期（在原 4–6 天计划上微调）

| Day | 任务 | 与原计划差异 |
|---|---|---|
| Day 1 上午 | 在 162 上 `pip install 'llm-sandbox[docker]'` + 跑通官方 hello | 替换原"等 AGR 内测申请"——**今天就能动手** |
| Day 1 下午 | 同机 `docker run agent-infra/sandbox` 跑通 hello + 用 Python SDK 调一次 jupyter | 新增 |
| Day 2 | 写 `SandboxExecutor` 抽象 + `LLMSandboxExecutor` 实现 + `LocalSandbox` 兜底 | 同原计划 |
| Day 3 | `product_agent.py` 五步骤；②④ 用 llm-sandbox，③ 用 agent-infra | 同原计划，仅 SDK 不同 |
| Day 4 | API + UI 联调 + SSE 进度 | 同原计划 |
| Day 5 | 容器池调参 + 安全开关（network=none/read_only/cap_drop） + AST 预扫描 | 同原计划 |
| Day 6 | 端到端真实样表跑通 + 文档 + 打 `v1.2.0-alpha` | 同原计划 |

**关键改进**：原计划 Day 1 半天阻塞在 AGR 内测申请，现在改成**双轨**——立即用开源跑通 PoC，AGR 内测下来后单独写一个适配器实现，PoC 代码完全复用。

### 6.4 未来迁回腾讯云 AGR 的路径

```
Phase 1 (M1)：llm-sandbox + agent-infra（开源、自托管 162）
Phase 2 (M2)：AGR 内测下来 → 加 TencentAGRSandboxExecutor → 灰度切流
Phase 3 (M3)：根据成本/性能/合规对比，决定是"AGR 主、开源备"还是相反
```

由于 §6.2 的接口抽象，**Phase 2/3 的切换只需要换 `SANDBOX_BACKEND` 配置**，业务代码、编排代码、前端代码全部不动——这正是《Agent_Runtime_部署与管理_设计说明.md》§5.1 强调"统一 Agent 契约"的应用。

---

## 7. 风险登记（增量）

| 风险 | 影响 | 缓解 |
|---|---|---|
| `llm-sandbox` 在多用户高并发下容器池泄漏 | 内存堆积 OOM | `max_container_uses=50` 强制回收 + 监控 `docker stats` |
| `agent-infra` 长期维护性（贡献者少） | 项目可能停更 | 接口隔离，可替换为 `browser-use` + 自托管 Playwright |
| 162 单机资源有限（4C8G？） | 并发浏览器只能 3–4 个 | 浏览器实例**按需启停**，跑完即销毁；不做常驻 |
| 选品 PoC 期遇到 AGPL 项目时误用 Daytona | 法务麻烦 | 决策已写明：Daytona 排除，只看 MIT/Apache-2.0 |
| 行情抓取被目标站反爬 | 数据获取不稳定 | 限制白名单到药监局/集采/米内网等公开渠道；不抓敏感商业数据 |

---

## 8. 待你拍板的事

1. **Day 1 是否就启动**？（不再等 AGR 内测，今天直接装 `llm-sandbox` 跑 hello）
2. **是否同意"双轨"方案**（llm-sandbox 跑 ②④ + agent-infra 跑 ③）？还是更倾向**先只用 llm-sandbox**，③ 行情抓取暂时用主进程的 `requests` 兜底？（后者更省事，但安全性弱一档）
3. **162 这台机器规格**：CVM 几核几 G？是否开了嵌套虚拟化（决定 microsandbox 能不能上）？— 我可以先 ssh 探一下
4. **AGR 内测申请是否同步发起**？（不阻塞 PoC，但晚两周拿到 AK/SK 总比没有强）
5. **本次新增依赖入 `backend/requirements.txt` 还是建独立的 `requirements.product.txt`**？（建议独立，避免污染 v1.1 视频链路的依赖树）

---

## 9. 一页速查（贴墙用）

```
选品 Agent · ②④ 算（pandas）→ llm-sandbox（MIT，Docker，容器池）
选品 Agent · ③  抓（行情）  → agent-infra/sandbox（Apache-2.0，Docker，All-in-One）
统一接口                    → SandboxExecutor（厂商无关，未来切 AGR 零改动）
本地兜底                    → LocalSandbox（subprocess + tmpdir）
不选                        → Daytona(AGPL) / E2B 自托管(门槛太高) / SkyPilot(POC 不安全)
```
