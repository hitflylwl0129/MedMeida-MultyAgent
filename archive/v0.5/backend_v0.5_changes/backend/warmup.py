#!/usr/bin/env python3
"""形象库预热脚本：把全部医生形象图直传 VOD 换取 FileId 并写入本地缓存。

可独立运行（后端进程外），结果落盘到 .cache/doctor_fileids.json，
后端启动后即可复用，无需重启。

用法:
    python warmup.py            # 预热全部形象
退出码:
    0  全部成功
    1  存在失败项（详见输出）
"""
from __future__ import annotations

import logging
import sys

logging.basicConfig(level=logging.INFO, format="%(message)s")

from app import doctors


def main() -> int:
    ok: list[tuple[str, str]] = []
    fail: list[tuple[str, str]] = []
    for d in doctors.list_doctors():
        try:
            fid = doctors.resolve_doctor_file_id(d.key)
            ok.append((d.key, fid))
            print(f"[OK]   {d.key:14s} -> {fid}")
        except Exception as e:  # noqa: BLE001  预热阶段需逐项收集失败原因
            fail.append((d.key, str(e)))
            print(f"[FAIL] {d.key:14s} -> {e}")

    print("---")
    print(f"SUCCESS={len(ok)} FAIL={len(fail)}")
    return 0 if not fail else 1


if __name__ == "__main__":
    sys.exit(main())
