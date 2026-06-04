"""
SecureVault 可行性 Demo —— 按进程白名单的"透明加解密"文件系统玩具版

目标: 证明在 macOS Apple Silicon 上, 使用 FUSE 可以做到
      "同一份磁盘文件, 白名单进程读到明文, 非白名单进程读到密文"。

为了让演示直观:
  - 不引入真正的加密算法; "加密"就是把每个字节按位取反 (ciphertext = ~plaintext)
  - 这样肉眼能立刻看出明文 vs 密文, 而文件长度保持一致
  - 真实方案会替换成 AES-256-GCM

用法:
  python3 sv_demo.py <底层存储目录> <挂载点> --allow /bin/cat --allow /usr/bin/head
  (上面这条命令把 cat 加白, head 默认不在白名单)
卸载:
  umount <挂载点>     (或 diskutil unmount <挂载点>)
"""
import argparse
import errno
import logging
import os
import subprocess
import sys
from pathlib import Path

from fuse import FUSE, FuseOSError, Operations, fuse_get_context

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("sv-demo")


def xor_bytes(data: bytes) -> bytes:
    """玩具版'加密': 按位取反。真实方案换成 AES-256-GCM。"""
    return bytes(b ^ 0xFF for b in data)


def proc_info_of(pid: int) -> tuple[str, int]:
    """通过 PID 一次性查到 (可执行路径, 父进程PID)。失败返回 ('', 0)。
    macOS 注意:
      - `ps -o comm=` 默认会截断 / 在空格处断开 (返回 'CodeBuddy CN Helper (Plugin)'
        会被切成 'CodeBuddy')。所以改用 -o args= (完整命令行) 取首段。
      - 优先用 sysctl 拿 KERN_PROCARGS2 不现实, 这里用 lsof 也太重。
      - 折中: 先用 args= 拿完整路径, 再用 ppid= 单独查父进程。
    """
    try:
        # 先拿完整 args (含可执行路径 + 参数)
        out = subprocess.check_output(
            ["ps", "-p", str(pid), "-o", "args="],
            stderr=subprocess.DEVNULL,
            timeout=1,
        ).decode().rstrip("\n")
        # args 格式: "/path/to/exe arg1 arg2 ..."
        # 但路径里也可能有空格, 真要严格做需要 shlex; demo 阶段用启发式:
        # 1) 如果整个字符串作为路径存在, 用它
        # 2) 否则按空格切, 找最长的"以 / 开头且文件存在"的前缀
        exe = ""
        if out:
            # 启发式: 从右边逐步切空格, 找第一个能 stat 到的路径
            tokens = out.split(" ")
            for end in range(len(tokens), 0, -1):
                candidate = " ".join(tokens[:end])
                if candidate.startswith("/") and os.path.exists(candidate):
                    exe = candidate
                    break
            if not exe:
                # 兜底: 第一个空格前的内容
                exe = tokens[0] if tokens else ""

        # 父进程 PID 单独查
        ppid_out = subprocess.check_output(
            ["ps", "-p", str(pid), "-o", "ppid="],
            stderr=subprocess.DEVNULL,
            timeout=1,
        ).decode().strip()
        ppid = int(ppid_out) if ppid_out else 0

        return exe, ppid
    except Exception:
        return "", 0


def proc_path_of(pid: int) -> str:
    """兼容旧接口: 只返回可执行路径。"""
    return proc_info_of(pid)[0]


VALID_MODES = {"whitelist", "cipher", "plain"}
ANCESTOR_MAX_DEPTH_DEFAULT = 6


class SecureVaultDemo(Operations):
    def __init__(self, backend_dir: str, whitelist: set[str],
                 ancestor_depth: int = ANCESTOR_MAX_DEPTH_DEFAULT):
        self.backend = Path(backend_dir).resolve()
        self.backend.mkdir(parents=True, exist_ok=True)
        # 把白名单全部 resolve 成绝对路径, 便于 ps 输出后比对
        self.whitelist = {str(Path(p).resolve()) for p in whitelist}
        self.ancestor_depth = ancestor_depth
        # 控制文件: 写入 'cipher'/'plain'/'whitelist' 即可热切换查看模式
        self.control_file = self.backend / ".sv_control"
        if not self.control_file.exists():
            self.control_file.write_text("whitelist\n")
        log.info("backend dir : %s", self.backend)
        log.info("whitelist   : %s", self.whitelist)
        log.info("ancestor depth: %d (向上查 N 级父进程)", self.ancestor_depth)
        log.info("control file: %s (写入 cipher/plain/whitelist 即可切换)", self.control_file)

    # ---------- 工具 ----------
    def _full(self, partial: str) -> str:
        return str(self.backend / partial.lstrip("/"))

    def _current_mode(self) -> str:
        """每次 read 都重新读, 实现热切换。读不到就回退到默认 whitelist。"""
        try:
            mode = self.control_file.read_text().strip().lower()
            return mode if mode in VALID_MODES else "whitelist"
        except Exception:
            return "whitelist"

    def _match(self, comm: str) -> bool:
        """单个进程名/路径 是否命中白名单 (绝对路径精确匹配 或 basename 匹配)。"""
        if not comm:
            return False
        if comm in self.whitelist:
            return True
        # 兼容 ps 偶尔只给短名(如 'cat')
        return any(Path(p).name == comm for p in self.whitelist)

    def _is_caller_whitelisted(self) -> tuple[bool, str]:
        """沿父进程链最多向上查 ancestor_depth 级,任意一级命中即放行。
        返回 (是否放行, 给日志看的字符串)。
        """
        uid, gid, pid = fuse_get_context()
        chain: list[str] = []  # 用来日志展示
        cur_pid = pid

        for level in range(self.ancestor_depth + 1):  # 0 是自身, 之后是 1..N 级祖先
            comm, ppid = proc_info_of(cur_pid)
            tag = f"{cur_pid}:{Path(comm).name or '?'}" if comm else f"{cur_pid}:?"
            chain.append(tag)

            if self._match(comm):
                # 命中! 标记是哪一级命中的(0=自身, 1=父, 2=祖父...)
                hit_label = "self" if level == 0 else f"ancestor[+{level}]"
                return True, f"pid={pid} chain={'<-'.join(chain)} hit@{hit_label}"

            # 到顶 (launchd / 0) 或 ps 失败, 停
            if not ppid or ppid <= 1 or ppid == cur_pid:
                break
            cur_pid = ppid

        return False, f"pid={pid} chain={'<-'.join(chain)} no-hit"

    # ---------- 元数据 ----------
    def getattr(self, path, fh=None):
        st = os.lstat(self._full(path))
        return {
            k: getattr(st, k)
            for k in (
                "st_atime", "st_ctime", "st_gid", "st_mode",
                "st_mtime", "st_nlink", "st_size", "st_uid",
            )
        }

    def readdir(self, path, fh):
        yield "."
        yield ".."
        for name in os.listdir(self._full(path)):
            yield name

    # ---------- 读: 核心策略点 ----------
    def open(self, path, flags):
        # 简单起见: 用 O_RDWR 打开, 真实方案要按 flags 透传
        return os.open(self._full(path), os.O_RDWR)

    def read(self, path, size, offset, fh):
        os.lseek(fh, offset, os.SEEK_SET)
        raw = os.read(fh, size)  # 底层存的就是密文(取反后的字节)
        mode = self._current_mode()

        # ---- 模式 1: 全密文 ----
        if mode == "cipher":
            log.info("READ  CIPHER(mode=cipher) %s", path)
            return raw

        # ---- 模式 2: 全明文 ----
        if mode == "plain":
            log.info("READ  PLAIN (mode=plain)  %s", path)
            return xor_bytes(raw)

        # ---- 模式 3: 按白名单(默认) ----
        allowed, who = self._is_caller_whitelisted()
        if allowed:
            log.info("READ  ALLOW  %s by %s -> 明文", path, who)
            return xor_bytes(raw)
        else:
            log.info("READ  CIPHER %s by %s -> 密文", path, who)
            return raw

    # ---------- 写: 白名单才能写, 写入自动'加密' ----------
    def create(self, path, mode, fi=None):
        allowed, who = self._is_caller_whitelisted()
        if not allowed:
            log.warning("CREATE DENY  %s by %s", path, who)
            raise FuseOSError(errno.EACCES)
        log.info("CREATE ALLOW %s by %s", path, who)
        return os.open(self._full(path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)

    def write(self, path, data, offset, fh):
        allowed, who = self._is_caller_whitelisted()
        if not allowed:
            log.warning("WRITE DENY  %s by %s", path, who)
            raise FuseOSError(errno.EACCES)
        os.lseek(fh, offset, os.SEEK_SET)
        encrypted = xor_bytes(data)  # 落盘前'加密'
        n = os.write(fh, encrypted)
        log.info("WRITE ALLOW %s by %s (%d bytes)", path, who, n)
        return len(data)  # 必须返回原始(明文)长度, 否则上层认为没写完

    def truncate(self, path, length, fh=None):
        with open(self._full(path), "r+b") as f:
            f.truncate(length)

    def unlink(self, path):
        allowed, who = self._is_caller_whitelisted()
        if not allowed:
            raise FuseOSError(errno.EACCES)
        os.unlink(self._full(path))

    def release(self, path, fh):
        os.close(fh)
        return 0

    # 同步类操作放空即可, demo 不关心
    def flush(self, path, fh):
        return 0

    def fsync(self, path, datasync, fh):
        return 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("backend", help="底层(密文)存储目录")
    ap.add_argument("mountpoint", help="FUSE 挂载点")
    ap.add_argument("--allow", action="append", default=[],
                    help="加入白名单的可执行路径, 可多次指定")
    ap.add_argument("--ancestor-depth", type=int, default=ANCESTOR_MAX_DEPTH_DEFAULT,
                    help="向上沿父进程链查多少级 (0=只看自己, 默认 6). "
                         "白名单祖先派生的所有子进程都自动通过。")
    ap.add_argument("--foreground", action="store_true",
                    help="前台运行, 方便看日志(默认前台)")
    args = ap.parse_args()

    if not args.allow:
        log.warning("没有任何白名单进程! 所有读都将返回密文。")

    os.makedirs(args.mountpoint, exist_ok=True)
    FUSE(
        SecureVaultDemo(args.backend, set(args.allow), ancestor_depth=args.ancestor_depth),
        args.mountpoint,
        nothreads=True,
        foreground=True,
        allow_other=False,
        direct_io=True,
    )


if __name__ == "__main__":
    main()
