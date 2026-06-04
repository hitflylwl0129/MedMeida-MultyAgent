# SecureVault M1 PoC 启动规划

> **目标**：把半天 Demo（Python + 假加密）升级为**正式可对外演示的 PoC**（C++ + 真 AES-256-GCM + 真进程身份校验）
> **周期**：3-5 周（1 人全职）
> **底座**：fork [securefs](https://github.com/netheril96/securefs)（MIT，C++17，质量高）

---

## 1. 为什么选 securefs 做底座

| 维度 | securefs | gocryptfs | 自研 |
|---|---|---|---|
| 加密强度 | AES-256-GCM + AES-SIV 文件名加密 | 同 | — |
| 完整性校验 | ✅ 每块 GCM tag | ✅ | 需自己写 |
| Apple Silicon | ✅ 已知运行 | ⚠️ 偏 Linux | — |
| 代码质量 | C++17，注释完整 | Go，可读性好 | — |
| **改造成本** | **加策略钩子约 800-1500 行 C++** | 需懂 Go | 5000+ 行 |
| License | MIT（可商用） | MIT | — |

**结论**：securefs 是最少改动、最高复用度的底座。

---

## 2. M1 范围与不做什么

### 2.1 M1 必做（Must）

| 模块 | 工时 | 说明 |
|---|---|---|
| 真加密替换占位算法 | 直接复用 securefs 现成 | 0（白嫖） |
| 添加 `fuse_get_context()` 鉴权层 | 2 天 | 在 `FuseHighLevelOps::read/write/create/unlink` 注入策略检查 |
| 进程身份识别：`proc_pidpath` + `SecCode` | 3 天 | 校验 Team ID + Bundle ID + 签名 hash |
| 白名单文件（YAML 格式） | 2 天 | 路径/Team ID/Bundle ID 三种规则 + 热加载 |
| **派生信任**机制 | 3 天 | 白名单 App 创建主文件时，`._xxx`/`~$xxx` 自动放行 |
| 系统元数据豁免 | 1 天 | `.DS_Store`、Spotlight `._*` 走单独策略链 |
| 三模式开关（继承自 Demo） | 1 天 | `whitelist`/`cipher`/`plain`，控制文件方案 |
| CLI 命令封装 | 2 天 | `sv init/mount/umount/add-app/list/revoke/audit` |
| 主密钥保护 → macOS Keychain | 2 天 | `SecItemAdd` API，先不用 Secure Enclave |
| 单元测试 + 集成测试 | 3 天 | pytest 跑集成，gtest 跑 C++ 单元 |
| **Word 完整工作流通过** | 验收门槛 | 保存/打开/编辑/另存 全链路 |

**合计工时**：约 **19 天纯开发 + 3 天联调** = **4-5 周**

### 2.2 M1 不做（Won't）

- ❌ GUI（菜单栏 App） → M2 做
- ❌ Secure Enclave（绑设备） → M3 做
- ❌ Touch ID 模式切换 → M3 做
- ❌ MDM 部署、自动启动 LaunchDaemon → M3 做
- ❌ Spotlight 索引明文（让白名单 mdworker 进保险箱看明文） → M2 评估
- ❌ Time Machine 兼容 → M3 评估

---

## 3. 任务清单（可直接复制到 TAPD/Jira）

### Sprint 1（第 1-2 周）：基础改造

- [ ] **T1.1** Fork `securefs` 到内部 git 仓库，跑通编译 + 测试用例
- [ ] **T1.2** 通读 `src/fuse_high_level_ops_base.cpp` 中 `read`/`write`/`create` 实现，确认注入点
- [ ] **T1.3** 实现 `PolicyEngine` 类：输入 `(operation, path, caller_token)`，输出 `Allow/Deny/Cipher`
- [ ] **T1.4** 在 `read()` 钩子中接入：白名单 → 解密返回，否则返回原始密文字节
- [ ] **T1.5** 在 `create()`/`write()`/`unlink()` 钩子中接入：白名单 → 加密落盘，否则 `EACCES`
- [ ] **T1.6** 整体跑通"tee 写 + cat 读"白名单场景，回归测试

### Sprint 2（第 3 周）：进程身份与派生信任

- [ ] **T2.1** 实现 `CallerIdentifier`：
  - `proc_pidpath(pid, buf, size)` 拿可执行路径
  - `SecCodeCopyGuestWithAttributes` 拿 Team ID + Bundle ID
  - 缓存（按 audit_token，避免每次 syscall 开销）
- [ ] **T2.2** 设计 `whitelist.yaml` 格式，支持 path/team_id/bundle_id/regex 四种规则
- [ ] **T2.3** 实现 YAML 解析器 + 热加载（inotify on Linux / kqueue on macOS）
- [ ] **T2.4** 实现"**派生信任**"：
  - 同一进程创建的 `xxx` 和 `._xxx` / `~$xxx` 视为同源
  - `.DS_Store` 单独走"只允许系统进程写、内容不参与加密"
- [ ] **T2.5** 验证：Word 加白后，能完整完成"新建 → 保存 → 关闭 → 重开"流程

### Sprint 3（第 4 周）：密钥管理与 CLI

- [ ] **T3.1** 主密钥从 securefs 原本的"密码派生" 改为"macOS Keychain 存储 32 字节 random"
  - `SecItemAdd` / `SecItemCopyMatching`
  - 应用首次启动时生成，存入 Keychain（带 access control: ThisDeviceOnly）
- [ ] **T3.2** 实现 `sv` 命令行：
  - `sv init <path>`：初始化新保险箱
  - `sv mount <path> <mountpoint>`：挂载
  - `sv umount <mountpoint>`：卸载
  - `sv add-app <bundle_id|path|team_id>`：加白名单
  - `sv list-apps`：列白名单
  - `sv revoke <app>`：撤销
  - `sv mode [cipher|plain|whitelist]`：三模式切换
  - `sv audit [--tail N]`：查审计日志
- [ ] **T3.3** 审计日志：JSON 行格式，每条 `{ts, op, path, caller: {pid, exe, team_id, bundle_id, signature_ok}, decision, mode}`

### Sprint 4（第 5 周）：测试与交付

- [ ] **T4.1** 单元测试覆盖率 ≥ 70%（PolicyEngine、CallerIdentifier、YAML 解析）
- [ ] **T4.2** 集成测试矩阵：
  - 白名单读写
  - 非白名单读密文 / 写被拒
  - Office 完整流程（Word/Excel/PowerPoint）
  - Apple Pages/Numbers/Keynote
  - VS Code / Cursor 编辑
  - Finder 拖拽进 + 拖拽出
  - 三模式切换
  - 大文件（1GB）
  - 并发读写
- [ ] **T4.3** 对抗测试：
  - 伪造进程名（应被签名校验拦下）
  - 直接读底层密文文件（应只能拿到密文，主密钥在 Keychain 不可读）
  - 篡改密文字节（应 GCM 校验失败）
- [ ] **T4.4** 写《M1 PoC 演示手册》+ 录屏 5 分钟
- [ ] **T4.5** 内部 Demo Day

---

## 4. 关键技术决策（提前确定，避免返工）

| 决策点 | 方案 | 原因 |
|---|---|---|
| FUSE 后端 | macFUSE 5.x（KEXT 模式） | macOS 15 仍需 KEXT，FSKit 要 macOS 26+ |
| Python or C++ | **C++（基于 securefs）** | 性能、避开 Python GIL、可签名分发 |
| 加密算法 | AES-256-GCM 分块（64KB） | 复用 securefs 默认 |
| 文件名加密 | AES-SIV | 复用 securefs |
| 主密钥位置 | macOS Keychain | M1 用 Keychain；M3 升级 Secure Enclave |
| 白名单格式 | YAML | 人类可读，热加载方便 |
| 进程鉴权 | `audit_token` + `SecCode` | 防 PID 复用、防伪 |
| 派生信任规则 | 同 PID + 文件名前缀（`._`、`~$`、`.DS_Store`） | 覆盖 Office/Apple 应用主要场景 |
| 模式切换控制 | 控制文件 + 文件锁 | 简单可靠，无需 IPC |

---

## 5. 风险与缓解

| 风险 | 概率 | 影响 | 缓解 |
|---|---|---|---|
| macFUSE 在 macOS 15 KEXT 加载失败 | 中 | 阻塞 | 安装时指引用户进恢复模式开启"用户管理 KEXT" |
| Office 仍有未发现的辅助文件依赖 | 高 | 中 | 用 `fs_usage` / `opensnoop` 抓 Office 真实 syscall，扩展派生信任规则 |
| Spotlight 索引把明文写入系统数据库 | 中 | 高 | 默认禁止 mdworker 进保险箱；或保险箱目录加入 Spotlight 排除 |
| 性能不达预期（< 50MB/s） | 低 | 中 | 启用 `direct_io=false`（用 kernel cache）+ 优化分块大小 |
| Apple 在新版本 macOS 进一步封禁 KEXT | 中 | 高 | M1 完成后立即评估 FSKit 迁移（macOS 26+） |
| `SecCode` API 性能瓶颈 | 中 | 中 | 按 `audit_token` 缓存校验结果，TTL 30s |

---

## 6. 投入预算

| 项目 | 估算 |
|---|---|
| 人力 | 1 人 × 4-5 周 = **20-25 人天** |
| 硬件 | 已有（你的 MacBook Pro） |
| 软件 | 全部开源，0 元 |
| 三方服务 | 无 |
| 测试设备 | 建议另租 1 台 Intel Mac 做兼容性回归（约 200 元/月） |
| **总成本** | **~ 1 个人月 + 0 元采购** |

---

## 7. 交付物清单（M1 完成时）

1. ✅ 可独立编译运行的 `sv` 命令行工具（pkg 安装包）
2. ✅ 白名单配置文件示例（含 Word/Excel/Cursor/Pages）
3. ✅ 单元测试报告（覆盖率 ≥ 70%）
4. ✅ 集成测试矩阵报告
5. ✅ 5 分钟演示录屏
6. ✅ 部署/使用手册
7. ✅ 设计文档（架构图、模块拆分、接口定义）
8. ✅ M2 规划草案（GUI + MCP 接入 WorkBuddy）

---

## 8. 与 WorkBuddy 的集成时机

**M1 不直接做 MCP 接入**，但要在 CLI 设计时预留：

- 所有 `sv` 子命令都支持 `--json` 输出（便于 MCP server 解析）
- 审计日志走 JSON Lines 格式（便于 MCP 工具 `vault_recent_access` 直接读）
- 白名单的"加白"操作有独立子命令 `sv add-app`（便于将来 MCP 工具 `vault_grant_app` 调用）

**M2 再做**：写一个 Python MCP server 包装 `sv` 命令，给 WorkBuddy 暴露：
- `vault_list_apps`
- `vault_grant_app(bundle_id, reason)`（带二次确认）
- `vault_revoke_app(bundle_id)`
- `vault_recent_access(limit=20)`
- `vault_current_mode()`

---

## 9. 立即可启动

如果决策"开干"，**第 1 周第 1 天的具体任务**：

```bash
# Day 1
git clone https://github.com/netheril96/securefs.git
cd securefs && cmake -B build && cmake --build build
./build/securefs --help    # 跑通基础命令

# Day 2-3
# 找到 src/fuse_high_level_ops_base.cpp，理解 read/write/create 调用链
# 写一份"注入点分析"文档

# Day 4-5
# 实现最小可行 PolicyEngine（先硬编码白名单，跑通"白名单 vs 非白名单"分支）
```

需要时随时联系我，把"Day 1-5 的实际产物"丢给我，我帮你 review。
