"""LLM 封装：调用腾讯云大模型（默认 hy3-preview / lkeap 兼容 OpenAI 端点）。

设计目标：
- 单一入口 `stream_chat()` —— 返回 token 增量生成器；
- 业务侧（agents/...）只关心 messages + 模型参数，不关心 SDK 细节；
- 通过 `Settings.llm_*` 在 .env 控制密钥/模型/温度等，**不入前端、不入版本控制**。
- 失败带重试（指数退避，最多 3 次），可控超时（默认 60s）。
"""
from __future__ import annotations

import logging
import time
from typing import Generator, Iterable, Optional

from openai import APIConnectionError, APIError, APITimeoutError, OpenAI, RateLimitError

from .config import Settings, get_settings

log = logging.getLogger("video-agent.llm")


class LLMError(RuntimeError):
    pass


_CLIENT_CACHE: dict[tuple[str, str], OpenAI] = {}


def _get_client(s: Settings) -> OpenAI:
    """按 (api_key, base_url) 缓存 OpenAI 客户端，避免重复握手。"""
    if not s.llm_api_key or not s.llm_base_url:
        raise LLMError("LLM 未配置（请在 backend/.env 设 LLM_API_KEY / LLM_BASE_URL）")
    key = (s.llm_api_key, s.llm_base_url)
    client = _CLIENT_CACHE.get(key)
    if client is None:
        client = OpenAI(
            api_key=s.llm_api_key,
            base_url=s.llm_base_url,
            timeout=s.llm_timeout_sec,
            max_retries=0,  # SDK 内置重试关闭，由本模块统一控制策略
        )
        _CLIENT_CACHE[key] = client
    return client


def stream_chat(
    messages: list[dict],
    *,
    model: Optional[str] = None,
    temperature: Optional[float] = None,
    max_tokens: Optional[int] = None,
    settings: Optional[Settings] = None,
) -> Generator[str, None, None]:
    """以**流式**方式调 LLM，yield 出每个 token 的 content 增量字符串。

    - 失败时按指数退避重试（连接错误/超时/限流）；不可恢复错误（4xx 校验类）直接抛 LLMError。
    - 返回的字符串拼接即完整回答。调用方可顺手做"打字机"前端推送。
    """
    s = settings or get_settings()
    client = _get_client(s)

    mdl = model or s.llm_model
    temp = temperature if temperature is not None else s.llm_temperature
    mt = max_tokens or s.llm_max_tokens

    last_err: Optional[Exception] = None
    for attempt in range(1, 4):
        try:
            stream = client.chat.completions.create(
                model=mdl,
                messages=messages,
                temperature=temp,
                max_tokens=mt,
                stream=True,
            )
            for chunk in stream:
                try:
                    delta = chunk.choices[0].delta
                except (AttributeError, IndexError):
                    continue
                piece = getattr(delta, "content", None)
                if piece:
                    yield piece
            return
        except (APIConnectionError, APITimeoutError, RateLimitError) as e:
            last_err = e
            wait = 1.5 * attempt
            log.warning("LLM 第%d次失败（可重试）：%s；%.1fs 后重试", attempt, e, wait)
            time.sleep(wait)
        except APIError as e:
            # 4xx 等不可重试错误：直接上抛
            raise LLMError(f"LLM 调用失败：{e}") from e
        except Exception as e:  # noqa: BLE001
            raise LLMError(f"LLM 未预期错误：{e}") from e

    raise LLMError(f"LLM 重试 3 次仍失败：{last_err}")


def complete_chat(
    messages: list[dict],
    **kwargs,
) -> str:
    """非流式封装：把 stream_chat 的增量拼成完整字符串。"""
    return "".join(stream_chat(messages, **kwargs))
