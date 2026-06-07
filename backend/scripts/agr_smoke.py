"""Agent Runtime 端到端烟测脚本。

用法（在 backend/ 目录下）::

    cd backend && python -m scripts.agr_smoke

需要先在 backend/.env 设置：
    AGR_ENABLED=true
    E2B_API_KEY=ark_xxx
    AGR_TEMPLATE_CODE=code-medmedia-v1
    AGR_TEMPLATE_BROWSER=browser-medmedia-v1

跑通后会输出：
  - 代码沙箱冷启动毫秒数 / Python 版本 / 平台
  - 浏览器沙箱冷启动毫秒数 / 药监局首页标题 / noVNC URL
"""
from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path

# 让 `python -m scripts.agr_smoke` 与 `python scripts/agr_smoke.py` 都能跑通
_BACKEND = Path(__file__).resolve().parent.parent
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

# 注入 backend/.env（与 app.main 同款）
from dotenv import load_dotenv

load_dotenv(_BACKEND / ".env", override=False)

from app.sandbox_executor import code_sandbox, browser_sandbox  # noqa: E402


def smoke_code() -> dict:
    """跑通代码沙箱：建实例 → run_code → 取 stdout。"""
    print("\n========== [1/2] CODE SANDBOX ==========")
    t0 = time.perf_counter()
    with code_sandbox() as sb:
        cold_ms = (time.perf_counter() - t0) * 1000
        print(f"[code] cold start  : {cold_ms:.1f} ms")
        print(f"[code] sandbox_id  : {sb.sandbox_id}")

        t1 = time.perf_counter()
        r = sb.run_code(
            "import sys, platform, os\n"
            "print('py      :', sys.version.split()[0])\n"
            "print('platform:', platform.platform())\n"
            "print('cpus    :', os.cpu_count())\n"
        )
        run_ms = (time.perf_counter() - t1) * 1000
        print(f"[code] run_code    : {run_ms:.1f} ms")
        print(f"[code] stdout      :")
        for line in (r.logs.stdout or []):
            print("    " + line.rstrip("\n"))
        if r.logs.stderr:
            print(f"[code] stderr      : {r.logs.stderr}")

        # 顺手装包 + 跑 pandas，验证「公网」配置 + 依赖安装链路
        print("\n[code] installing pandas via pip ...")
        t2 = time.perf_counter()
        sb.run_code("import subprocess, sys; subprocess.run([sys.executable, '-m', 'pip', 'install', '-q', 'pandas'], check=True)")
        inst_ms = (time.perf_counter() - t2) * 1000
        print(f"[code] pip install : {inst_ms:.1f} ms")

        t3 = time.perf_counter()
        r2 = sb.run_code(
            "import pandas as pd\n"
            "df = pd.DataFrame({'sku':['A','B','C'], 'sales':[120, 88, 203]})\n"
            "print(df.describe().to_string())"
        )
        pd_ms = (time.perf_counter() - t3) * 1000
        print(f"[code] pandas run  : {pd_ms:.1f} ms")
        for line in (r2.logs.stdout or []):
            print("    " + line.rstrip("\n"))

        return {
            "cold_ms": cold_ms,
            "run_ms": run_ms,
            "pip_install_ms": inst_ms,
            "pandas_run_ms": pd_ms,
            "sandbox_id": sb.sandbox_id,
        }


async def smoke_browser() -> dict:
    """跑通浏览器沙箱：CDP 接入 → 访问药监局首页 → 取标题。"""
    print("\n========== [2/2] BROWSER SANDBOX ==========")
    from playwright.async_api import async_playwright

    t0 = time.perf_counter()
    with browser_sandbox() as (sb, cdp_url, novnc_url):
        cold_ms = (time.perf_counter() - t0) * 1000
        print(f"[browser] cold start: {cold_ms:.1f} ms")
        print(f"[browser] sandbox_id: {sb.sandbox_id}")
        print(f"[browser] vnc       : {novnc_url}")
        print(f"[browser] cdp       : {cdp_url}")

        token = getattr(sb, "_envd_access_token", "")
        t1 = time.perf_counter()
        async with async_playwright() as p:
            browser = await p.chromium.connect_over_cdp(
                cdp_url, headers={"X-Access-Token": str(token)}
            )
            cdp_ms = (time.perf_counter() - t1) * 1000
            print(f"[browser] cdp conn  : {cdp_ms:.1f} ms")

            context = browser.contexts[0]
            page = context.pages[0]

            # 抓药监局公开首页（不登录、不抓敏感数据，符合选品 Agent 行情抓取场景）
            target = "https://www.nmpa.gov.cn/"
            t2 = time.perf_counter()
            await page.goto(target, wait_until="domcontentloaded", timeout=30_000)
            nav_ms = (time.perf_counter() - t2) * 1000
            title = await page.title()
            print(f"[browser] nav       : {nav_ms:.1f} ms")
            print(f"[browser] target    : {target}")
            print(f"[browser] title     : {title!r}")

        return {
            "cold_ms": cold_ms,
            "cdp_connect_ms": cdp_ms,
            "navigate_ms": nav_ms,
            "title": title,
            "sandbox_id": sb.sandbox_id,
            "novnc_url": novnc_url,
        }


def main() -> int:
    print(">>> AGR smoke test starting ...")
    code_stats = smoke_code()
    browser_stats = asyncio.run(smoke_browser())

    print("\n========== SUMMARY ==========")
    print(f"  code    cold start : {code_stats['cold_ms']:.1f} ms")
    print(f"  code    run_code   : {code_stats['run_ms']:.1f} ms")
    print(f"  code    pandas run : {code_stats['pandas_run_ms']:.1f} ms")
    print(f"  browser cold start : {browser_stats['cold_ms']:.1f} ms")
    print(f"  browser navigate   : {browser_stats['navigate_ms']:.1f} ms")
    print(f"  page title         : {browser_stats['title']!r}")
    print(">>> AGR smoke test done.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
