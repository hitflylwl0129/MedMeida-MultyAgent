# SecureVault 可行性 Demo 验证报告

> **日期**：2026 年 5 月 31 日
> **平台**：MacBook Pro，Apple Silicon (M 系列)，macOS 15.6.1 (Sequoia)
> **耗时**：半天（含调研 + 编码 + 验证）
> **结论**：✅ **技术路线已被端到端证实可行**

---

## 0. 背景与目标

**用户需求**：在 macOS 上实现一个"加密文件夹"，满足：
1. 文件夹内文件**默认落地加密**（新建/拷入自动加密）
2. **任何应用**打开看到的是**密文**（包括 Word/Pages/Finder 等）
3. **用户手动加白**某应用后，该应用**透明地读到明文、写回自动加密**

**核心技术疑问**：是否可以在**不依赖商业产品、不依赖 Apple 特批 entitlement**的前提下，做到"同一份磁盘文件、按调用进程身份返回明文或密文"？

**调研结论（已完成）**：开源界没有现成方案，但理论上可行。技术路线确定为：
**macFUSE + `fuse_get_context()` 拿调用方 PID + 进程身份鉴权 + 自定义加解密 read/write 钩子**

本报告记录半天内完成的可行性 Demo 及其端到端验证证据。

---

## 1. 4 个被实验证实的核心事实 ✅

| # | 命题 | 证据 | 状态 |
|---|---|---|---|
| ① | 物理落盘的确是密文 | `~/.sv_demo_store/baseline.txt` 字节为 `1740 6619 6750 ...`（demo 用按位取反模拟加密，真实方案换 AES-256-GCM） | ✅ 证实 |
| ② | 非白名单应用读取只能看到密文 | Word/xxd/head 读 `baseline.txt` 全部看到乱码；日志记录 `READ CIPHER ... by Microsoft Word` | ✅ 证实 |
| ③ | 非白名单应用写入/新建会被拒绝 | Word 另存 `0531 22.docx` / `0531 11.docx` 全部失败，弹"无法保存"对话框；日志记录 `CREATE DENY` | ✅ 证实 |
| ④ | 白名单进程身份能被准确识别 | 日志拿到完整可执行路径 `/Applications/Microsoft Word.app/Contents/MacOS/Microsoft Word`，比裸进程名可靠得多 | ✅ 证实 |

**附加被证实的能力**：

- ✅ **三种安全模式可热切换**（`whitelist` / `cipher` / `plain`），无需重启 FUSE
- ✅ **macOS 系统暗中写入也能被拦**（Finder 写 `.DS_Store`、Spotlight 写 `._xxx` 元数据）
- ✅ **整套方案不依赖 KEXT / ESF entitlement**，开源工具链即可

---

## 2. Word 实验完整证据链（日志原文）

实验环境：白名单只含 `/usr/bin/tee` 和 `/bin/cat`，**Word 不在白名单**。

### 2.1 用白名单 `tee` 写入文件（成功）

```
2026-05-31 11:37:45,291 [INFO] CREATE ALLOW /baseline.txt by pid=83940 comm='/usr/bin/tee'
2026-05-31 11:37:45,299 [INFO] WRITE  ALLOW /baseline.txt by pid=83940 comm='/usr/bin/tee' (52 bytes)
```

### 2.2 Word 打开文件（看到密文）

```
2026-05-31 11:38:33,167 [INFO] READ  CIPHER /baseline.txt by pid=71154 comm='/Applications/Microsoft Word.app/Contents/MacOS/Microsoft Word' -> 密文
2026-05-31 11:38:33,182 [INFO] READ  CIPHER /baseline.txt by pid=71154 comm='/Applications/Microsoft Word.app/Contents/MacOS/Microsoft Word' -> 密文
...（连续 8 次 READ CIPHER，Word 反复读取后只能看到乱码）
```

### 2.3 Word 尝试创建辅助文件（被拒）

```
2026-05-31 11:38:33,110 [WARNING] CREATE DENY  /._baseline.txt by Microsoft Word    ← AppleDouble 扩展属性文件
2026-05-31 11:38:33,189 [WARNING] CREATE DENY  /~$seline.txt   by Microsoft Word    ← Word 的"打开锁定"文件
```

### 2.4 Word 尝试保存新文档（被拒，对应错误对话框）

```
2026-05-31 11:39:46,806 [WARNING] CREATE DENY  /0531 22.docx by Microsoft Word
2026-05-31 11:40:13,941 [WARNING] CREATE DENY  /0531 22.docx by Microsoft Word
2026-05-31 11:44:35,661 [WARNING] CREATE DENY  /0531 22.docx by Microsoft Word
2026-05-31 11:44:43,237 [WARNING] CREATE DENY  /0531 11.docx by Microsoft Word
```

**用户看到的报错**：
> Word 无法保存或创建此文件。请确定要用于保存此文件的磁盘未满、未受写保护且未被破坏。(0531 22.docx)

### 2.5 切到 plain 模式，Word 立刻能看到明文（验证策略热切换）

```
2026-05-31 11:44:57,978 [INFO] READ  PLAIN (mode=plain) /baseline.txt
...
```

**用户在 Word 里看到的**：
> 这是一份机密的医疗合同草稿 2026年5月

**关键点**：Word 进程没变（PID 71154），文件没变，**唯一变化的只是 FUSE 的策略开关**。这证明"按策略呈现不同视图"的机制完全工作。

### 2.6 切回 whitelist 模式，保护立即恢复

```
2026-05-31 11:47:32,245 [INFO] READ  CIPHER /baseline.txt by Microsoft Word -> 密文
```

### 2.7 意外发现：Finder 和 Spotlight 也被自动拦截

```
2026-05-31 11:35:28,887 [WARNING] CREATE DENY /.DS_Store     by Finder
2026-05-31 11:44:57,963 [WARNING] CREATE DENY /._baseline.txt by mdwrite (Spotlight)
```

→ 默认状态下，**系统级"暗中写入"也被纳入策略管控**，这反而是好事（防止 Spotlight 索引泄露明文）。

---

## 3. 同时暴露的 3 个真实工程难点 ⚠️

实验同时把"半成品"会踩的坑摆到了台面上，反而是 Demo 的最大价值。

### 难点 1：Office 类应用依赖辅助文件，单加白主进程不够

**现象**：即使把 Word 加白，保存仍会失败，因为它需要同时创建：
- `~$xxx.docx`（Word 内部的"打开锁定"文件）
- `._xxx.docx`（macOS AppleDouble 扩展属性文件，由 `doubleagentd` 写）
- `.DS_Store`（Finder 写）
- Spotlight `._xxx`（由 `mdwrite` 写）

**真实方案对策**：
- 引入"**派生信任**"概念：白名单 App **创建主文件时，对应的 `._xxx` / `~$xxx` 自动放行**
- 系统守护进程对**元数据文件**（前缀 `._`、`.DS_Store`）整体豁免，但**只能读不能写主文件**

### 难点 2：进程身份识别需升级到代码签名级别

**现象**：当前 Demo 用 `ps -o comm=` 拿可执行路径，**存在被替换/伪装的风险**。

**真实方案对策**：
- 用 `audit_token` + `SecCodeCopyGuestWithAttributes` 校验**Team ID + Bundle ID + 代码签名 hash**
- 三者组合后**几乎不可伪造**
- 白名单文件格式升级为 YAML，支持按 Team ID/Bundle ID 整批授权

### 难点 3：`plain` 模式是把双刃剑

**现象**：本次 Demo 设了 `plain` 模式后忘了切回，1 分钟内 Word 都能看明文，等于安全裸奔。

**真实方案对策**：
- `plain` 模式**默认带超时**（如 5 分钟自动回滚到 `whitelist`）
- 切换 `plain` 模式必须 **Touch ID 二次确认**
- 所有模式切换写入**不可逆审计日志**（带签名）

---

## 4. 技术栈与依赖

| 组件 | 选型 | 备注 |
|---|---|---|
| 用户态文件系统 | **macFUSE 5.x** | macOS 12+，Apple Silicon 原生 |
| Python 绑定 | **fusepy** | 注意：`pyfuse3` 在 macOS 上跑不起来（需 libfuse3，macFUSE 是 libfuse2 ABI） |
| 进程身份识别 | `fuse_get_context()->pid` + `ps`/`SecCode` | Demo 用 `ps`，正式版换 `SecCode` |
| 加密算法（真实方案） | AES-256-GCM 分块（64KB） | Demo 用按位取反占位 |
| 主密钥保护（真实方案） | macOS Keychain + Secure Enclave | 设备绑定，离开本机不可解 |

---

## 5. Demo 产物清单

位置：`securevault_demo/`

| 文件 | 作用 | 行数 |
|---|---|---|
| `sv_demo.py` | FUSE 主程序（含三模式热切换） | ~150 |
| `start.sh` | 后台启动脚本 | ~60 |
| `stop.sh` | 停止 + 清理（`--purge` 彻底重置） | ~50 |
| `status.sh` | 查看运行状态 | ~40 |
| `sv-mode` | 三模式开关命令（`cipher`/`plain`/`whitelist`） | ~50 |
| `run_demo.sh` | 一键自动化基础测试 | ~80 |
| `demo_mode_switch.sh` | 模式切换演示脚本 | ~60 |

**核心代码总量**：约 **500 行 Python + Bash**。

---

## 6. 验真矩阵（已全部通过 ✅）

| 等级 | 测试项 | 结果 |
|---|---|---|
| L0 | FUSE 挂载成功 | ✅ `macfuse` 挂载类型确认 |
| L1 | 白名单进程写入 → 落盘密文 | ✅ tee 写入 → 物理字节加密 |
| L1 | 白名单进程读取 → 看到明文 | ✅ cat 看到原文 |
| L2 | 非白名单进程读 → 看到密文 | ✅ Word/xxd 看到乱码 |
| L2 | 非白名单进程写 → 被拒 | ✅ Word 保存失败 |
| L3 | 模式热切换 | ✅ `plain` 后 Word 立刻看到明文 |
| L3 | 切回 `whitelist` 立即恢复保护 | ✅ Word 再次看到密文 |
| L4 | 系统进程也受策略约束 | ✅ Finder/Spotlight `.DS_Store`/`._xxx` 被拦 |

---

## 7. 给同事/老板的"一句话总结"

> **半天时间用纯开源工具链证实**：macOS Apple Silicon 上可以做到"**同一份磁盘加密文件，按调用进程身份返回明文或密文**"，无需 Apple 特批 entitlement，无需 KEXT。Word 实验完整闭环：写加密、读密文、保存被拒、热切换看明文——全部按预期工作。同时识别出 3 个真实工程难点，已有清晰对策。**建议进入 M1 PoC（基于 securefs C++ 改造）**。

---

## 8. 下一步建议

1. **M1 PoC**（3-5 周）：基于 `securefs` 改造，把"按进程鉴权 + 派生信任 + Touch ID 模式切换"做成正式可用的版本（详见 `M1_PoC_启动规划.md`）
2. **M2 完整白名单 + 派生 + GUI**（4-6 周）
3. **M3 加固/容灾/审计**（2-3 周）

整体预算：**约 2-3 个月（1 人）**到企业可试用。

---

## 附录 A：父进程链白名单功能（2026-06-01 追加）

### A.1 背景

最初 demo 仅支持"调用进程必须本身在白名单"，无法满足"WorkBuddy/CodeBuddy 整个 IDE 家族都能访问保险箱"的需求——因为：

- WorkBuddy/CodeBuddy 是 Electron App，主进程是 `Electron`，工作进程是各种 Helper（Renderer/Plugin/GPU）
- 小 w 调工具时，**真正读文件的是 Helper 派生的 `bash`/`python`/`cat`/`grep` 等**，这些工具自身不在白名单
- 如果把 `python`/`bash` 全加白，又会让用户在自己 Terminal 里也能直接读保险箱 → 失去策略意义

### A.2 解决方案

在 `_is_caller_whitelisted()` 中**沿父进程链向上爬最多 N 级**，任意一级命中白名单即放行。

**关键参数**：`--ancestor-depth` 默认 6 级（足以覆盖 `Plugin Helper → Electron 主进程 → bash → python` 这种典型链路）。

### A.3 验证证据（同一条 Python 命令，两种行为）

| 配置 | Python 读到的 | 日志记录 |
|---|---|---|
| `--ancestor-depth 0` | **密文** | `chain=Python no-hit -> 密文` |
| `--ancestor-depth 6` | **明文** | `chain=Python<-zsh<-CodeBuddy CN Helper (Plugin) hit@ancestor[+2] -> 明文` |

完整日志：

```
2026-06-01 00:00:03,432 [INFO] READ CIPHER /baseline.txt by pid=17980
    chain=17980:Python  no-hit -> 密文                          ← depth=0

2026-06-01 00:00:05,619 [INFO] READ ALLOW  /baseline.txt by pid=18062
    chain=18062:Python<-17847:zsh<-53066:CodeBuddy CN Helper (Plugin)
    hit@ancestor[+2] -> 明文                                    ← depth=6
```

### A.4 实现中的关键工程发现

#### 发现 1：macOS `ps -o comm=` 不能用

- 默认会**截断**且**在空格处切断**
- 实测："`/Applications/CodeBuddy CN.app/Contents/MacOS/Electron`" 被切成 "`Co`"
- **正确做法**：用 `ps -o args=` 拿完整命令行，再启发式找最长的"以 `/` 开头且文件存在"的前缀作为可执行路径

```python
# demo 里的实现 (sv_demo.py)
out = subprocess.check_output(["ps", "-p", str(pid), "-o", "args="], ...)
tokens = out.split(" ")
for end in range(len(tokens), 0, -1):
    candidate = " ".join(tokens[:end])
    if candidate.startswith("/") and os.path.exists(candidate):
        exe = candidate
        break
```

> **M1 PoC 提示**：C++ 实现应直接用 `proc_pidpath(pid, buf, PROC_PIDPATHINFO_MAXSIZE)` 系统调用，一步到位且无歧义。

#### 发现 2：Electron App 的进程层级很深

实测 CodeBuddy CN 的工具调用链（≥ 4 级）：

```
工具进程 (cat/python/grep)
    ↑ 父
zsh                           ← 集成终端 shell
    ↑ 父
CodeBuddy CN Helper (Plugin)  ← Electron 插件进程 (★ 命中白名单)
    ↑ 父
CodeBuddy CN Electron         ← Electron 主进程
    ↑ 父
launchd
```

→ **`ancestor-depth` 不能设小于 3**，否则 Helper 之上的进程链会被忽略；建议默认 6 留余量。

#### 发现 3：父进程链放行有语义陷阱（M1 必须解决）

如果攻击者用 `nohup` / `setsid` / `disown` 让恶意进程脱离当前进程链，重新挂到 `launchd` 下，就**绕过了**父进程链白名单。

**M1 PoC 对策**：放弃用 PID 链，改用 `audit_token`。每个进程拥有不可伪造的 audit token，包含**派生它的"原始"上下文**，操作系统层面跟踪，绕不过去。

### A.5 依赖修复的两个真实 Bug

实验过程中还修复了两个真实工程问题：

**Bug 1：模式开关是把双刃剑**
- 现象：从 `cipher` 模式忘记切回 `whitelist` 后，所有读取（包括 `cat`）都被强制返回密文，误以为白名单失效
- 防呆：M1 中 `cipher`/`plain` 模式默认带 30 分钟自动回滚

**Bug 2：`truncate` 钩子未做权限检查**
- 现象：Word/Spotlight 试图写元数据时间接调用 `O_TRUNC` 或 `truncate()`，会**清空保险箱里的文件**（因为底层 `os.lstat` 显示 0 字节但内容已经被破坏）
- 教训：**所有可改变文件大小的钩子**（`truncate`、`open(O_TRUNC)`、`ftruncate`）都必须做策略检查，缺一不可

### A.6 代码改动量

| 文件 | 新增行 | 改动行 |
|---|---|---|
| `sv_demo.py` | +40 | ~15 |
| `start.sh` | +20 | ~10 |

### A.7 给 M1 的明确指引

| 来自 Demo 的发现 | M1 必做项 |
|---|---|
| `ps` 拿可执行路径不靠谱 | 用 `proc_pidpath` 系统调用 |
| Electron 进程链深 | 进程身份不能只看直接调用方，要看"父链"或"audit token" |
| PID 链能被脱链绕过 | 用 `audit_token`，由内核维护，不可伪造 |
| 模式开关易忘 | 默认带超时；`plain` 模式必须 Touch ID 二次确认 |
| `truncate` 等钩子是隐藏风险点 | 所有可改变文件状态的钩子都加策略；做单元测试覆盖 |
