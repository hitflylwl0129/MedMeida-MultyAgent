"""统一沙箱执行入口——基于腾讯云 Agent Runtime（E2B 协议兼容）。

设计原则（沿用 v1.1 的厂商适配器思路）：
- 业务层（agents/product_agent.py）只看本模块，不直接 import e2b_code_interpreter / e2b。
- 未来切 CubeSandbox 自托管 / e2b.dev 商业云 / 其它 E2B 兼容实现，只改 _make_sandbox() 内部。
- 沙箱按 job 起、跑完即销毁，绝不依赖沙箱内文件做跨 job 持久化（公测期商业化后 30 天删数据）。
- 大文件中转走 COS（后续 product_agent 落地时再补 COS helper）。

用法示例（PoC 见 scripts/agr_smoke.py）::

    from app.sandbox_executor import code_sandbox, browser_sandbox

    with code_sandbox() as sb:
        r = sb.run_code("print('hi')")

    with browser_sandbox() as (sb, cdp_url, novnc_url):
        # 用 playwright.connect_over_cdp 接入 cdp_url
        ...
"""
from __future__ import annotations

import logging
import os
from contextlib import contextmanager
from typing import Iterator, Tuple

from .config import get_settings

log = logging.getLogger("video-agent.sandbox")


def _ensure_env() -> None:
    """把 .env 的 AGR 配置注入进程环境——e2b SDK 只从 env 读取。

    重复调用幂等。仅在 agr_enabled=true 且 api_key 非空时注入，否则保留环境原值。

    附带做一件事：**禁用 e2b SDK 2.x 客户端对 API Key 格式的硬校验**。
    e2b 2.x 的 `validate_api_key` 写死了正则 `\\Ae2b_[0-9a-f]+\\Z`，
    而腾讯云 Agent Runtime 发的 Key 是 `ark_xxx` 前缀，会被客户端直接拒掉。
    我们在调用前用 noop 替换这个函数，把校验交给真正的服务端来做。
    （服务端域名走 E2B_DOMAIN=ap-guangzhou.tencentags.com，鉴权失败会从那边正常回 401。）
    """
    s = get_settings()
    if not s.agr_ready:
        raise RuntimeError(
            "AGR 未就绪：请检查 .env 中 AGR_ENABLED=true 且 E2B_API_KEY 已配置"
        )
    os.environ["E2B_DOMAIN"] = s.e2b_domain
    os.environ["E2B_API_KEY"] = s.e2b_api_key

    # 禁用 2.x 客户端硬正则校验（兼容腾讯云 ark_ 前缀 Key）
    try:
        from e2b import api as _e2b_api  # type: ignore

        if getattr(_e2b_api, "validate_api_key", None) is not None and \
                not getattr(_e2b_api.validate_api_key, "_agr_patched", False):
            def _noop(_key: str) -> None:  # noqa: D401
                return None
            _noop._agr_patched = True  # type: ignore[attr-defined]
            _e2b_api.validate_api_key = _noop  # type: ignore[assignment]
            log.info("e2b.api.validate_api_key patched (allow ark_* keys for Tencent AGR)")
    except Exception as e:  # noqa: BLE001
        log.warning("patch e2b validate_api_key failed: %s", e)


@contextmanager
def code_sandbox(timeout_sec: int | None = None) -> Iterator["object"]:
    """代码沙箱上下文：跑 pandas / 打分 / LLM 生成代码等。

    退出时自动 kill；异常路径也保证销毁，避免内测期沙箱配额泄漏。
    """
    _ensure_env()
    from e2b_code_interpreter import Sandbox  # 延迟导入，避免未装包时 main 启动失败

    s = get_settings()
    sb = Sandbox.create(
        template=s.agr_template_code,
        timeout=timeout_sec or s.agr_default_timeout_sec,
    )
    log.info("AGR code sandbox created id=%s template=%s", sb.sandbox_id, s.agr_template_code)
    try:
        yield sb
    finally:
        try:
            sb.kill()
            log.info("AGR code sandbox killed id=%s", sb.sandbox_id)
        except Exception as e:  # noqa: BLE001
            log.warning("AGR code sandbox kill failed id=%s: %s", sb.sandbox_id, e)


@contextmanager
def browser_sandbox(
    timeout_sec: int | None = None,
) -> Iterator[Tuple["object", str, str]]:
    """浏览器沙箱上下文：抓行情 / 自动化登录态等。

    返回 (sandbox, cdp_url, novnc_url)：
      - cdp_url   : 用 playwright.connect_over_cdp(cdp_url, headers={"X-Access-Token": ...}) 程控
      - novnc_url : 复制到浏览器可实时看沙箱里的画面（PoC 与排障利器）
    """
    _ensure_env()
    from e2b import Sandbox  # 浏览器沙箱用基础 e2b SDK，不要混 e2b_code_interpreter

    s = get_settings()
    sb = Sandbox.create(
        template=s.agr_template_browser,
        timeout=timeout_sec or s.agr_default_timeout_sec,
    )
    host = sb.get_host(9000)
    # 访问 token 是 sandbox 实例属性；E2B 当前 SDK 暴露在 _envd_access_token，遵循官方 quickstart 用法
    token = getattr(sb, "_envd_access_token", "")
    cdp_url = f"https://{host}/cdp"
    novnc_url = (
        f"https://{host}/novnc/vnc_lite.html?&path=websockify?access_token={token}"
    )
    log.info("AGR browser sandbox created id=%s template=%s", sb.sandbox_id, s.agr_template_browser)
    try:
        yield sb, cdp_url, novnc_url
    finally:
        try:
            sb.kill()
            log.info("AGR browser sandbox killed id=%s", sb.sandbox_id)
        except Exception as e:  # noqa: BLE001
            log.warning("AGR browser sandbox kill failed id=%s: %s", sb.sandbox_id, e)
