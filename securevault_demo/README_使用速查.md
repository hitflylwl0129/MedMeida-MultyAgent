# SecureVault Demo 速查（继续玩别的应用）

> Demo 当前**仍在运行**。挂载点：`~/SecureVaultDemo`。当前白名单：`/usr/bin/tee`、`/bin/cat`。
> 当前模式：`whitelist`（按白名单鉴权）。

---

## 1. 状态/启停

```bash
cd "/Users/coelho/Documents/文稿 - ROYWLLI-MC1/Tencent Job/2026医疗/20260522_成都先导/7. 本地IPGUARD/securevault_demo"
./status.sh        # 看运行状态
./stop.sh          # 停止（数据保留）
./stop.sh --purge  # 停止并彻底清空
./start.sh         # 启动（默认白名单 tee + cat）
```

---

## 2. 玩三模式开关

```bash
./sv-mode             # 看当前模式
./sv-mode cipher      # 所有应用读都是密文（最严格）
./sv-mode plain       # 所有应用读都是明文（用完一定要切回去！）
./sv-mode whitelist   # 回到按白名单（默认）
```

---

## 3. 推荐试一试的应用

| App | 怎么试 | 预期 |
|---|---|---|
| **TextEdit** | ⌘+N 写点东西 → 另存为 → `~/SecureVaultDemo/test.txt` | 保存失败（同 Word，因为不在白名单） |
| **Apple Pages** | 新建 → 另存为 `~/SecureVaultDemo/x.pages` | 保存失败 |
| **Numbers/Excel** | 同上 | 保存失败 |
| **Cursor / VS Code** | `File → Open Folder → ~/SecureVaultDemo` 然后试着新建/修改文件 | 新建失败；打开已有文件看到密文 |
| **预览（Preview）** | 用 `tee` 写一个文本，然后用 Preview 打开 | 显示乱码或报错 |
| **QuickLook** | 在 Finder 选中 `baseline.txt`，按空格 | 看到密文乱码 |
| **Terminal 自带工具** | `cat`/`head`/`tail`/`grep`/`less`/`vim`/`nano`/`hexdump` | `cat` 看明文（白名单），其他全部密文 |

---

## 4. 想让某个 App 试着"读到明文"

**临时方法**：杀掉 demo，加上 `--allow` 重启

```bash
./stop.sh

# 示例：加白 Cursor
cd "/Users/coelho/Documents/文稿 - ROYWLLI-MC1/Tencent Job/2026医疗/20260522_成都先导/7. 本地IPGUARD/securevault_demo"
nohup python3 sv_demo.py ~/.sv_demo_store ~/SecureVaultDemo \
    --allow /usr/bin/tee \
    --allow /bin/cat \
    --allow "/Applications/Cursor.app/Contents/MacOS/Cursor" \
    > sv_demo.log 2>&1 &
echo $! > .sv_demo.pid
```

> ⚠️ **注意**：仅限"**读**"会变成明文。"**写**"由于 macOS 元数据文件（`._xxx`/`~$xxx`）依赖系统进程，**保存大概率仍失败**。这是 Demo 的已知限制（不是 bug，是简化版没做派生信任）。
> 真实 M1 方案会解决这个问题。

---

## 5. 想偷懒一次看明文？

```bash
./sv-mode plain
# ... 做你想做的事 ...
./sv-mode whitelist    # ★★★ 千万记得切回去 ★★★
```

---

## 6. 一些有趣的"对抗实验"

| 实验 | 命令 | 看什么 |
|---|---|---|
| 直接读底层加密文件 | `xxd ~/.sv_demo_store/baseline.txt \| head` | 真密文，无法解 |
| 把密文文件拷出去 | `cp ~/SecureVaultDemo/baseline.txt ~/Desktop/leaked.txt` | 桌面拿到的是**密文**（cp 不在白名单） |
| 同一个文件，cat 和 xxd 对比 | `cat ~/SecureVaultDemo/baseline.txt; echo; xxd ~/SecureVaultDemo/baseline.txt` | cat 明文、xxd 密文 |
| 看实时日志 | `tail -f securevault_demo/sv_demo.log` | 所有应用对保险箱的访问都被记录 |

---

## 7. 完了别忘了

```bash
cd "/Users/coelho/Documents/文稿 - ROYWLLI-MC1/Tencent Job/2026医疗/20260522_成都先导/7. 本地IPGUARD/securevault_demo"
./sv-mode whitelist    # 确保切回安全模式
./stop.sh              # 完全停掉
```
