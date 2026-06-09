"""腾讯云智能数智人「照片免训练」接口客户端（最小可用版）。

定位：
- 仅作为 v1.1 Kling 路 A 的**备用路径**调研使用，**不替换主链路**
- aPaas 平台鉴权：AppKey + AccessToken（HMAC-SHA256，签名走 URL Query）
- 异步：提交 → 拿 TaskId → 轮询 getprogress → 拿视频 URL

文档：
- https://cloud.tencent.com/document/product/1240/118475 （照片免训练接口）
- https://cloud.tencent.com/document/product/1240/107197 （aPaas 鉴权规范）
- https://cloud.tencent.com/document/product/1240/81270 （getprogress）

鉴权规范要点（踩坑提示）：
1) 没有任何自定义 HTTP Header，所有鉴权全在 URL QueryString
2) 公共参数：appkey / timestamp / signature（无 nonce）
3) HMAC-SHA256(签名密钥=AccessToken)，被签内容按 key 字典序拼 "k=v&k=v"
4) 结果 Base64 → URL Encode 后拼到 URL 末尾
5) Body 必须包 {"Header":{}, "Payload":{...}}，不是平铺

安全：
- AppKey/AccessToken 仅从环境变量读取，绝不入版本库
- 默认 timeout=30s 防止主进程卡死
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Optional
from urllib.parse import quote

import requests

log = logging.getLogger("video-agent.tencent_avatar")

# aPaas 平台接入域名（v2 接口）
BASE_URL = "https://gw.tvs.qq.com"
SUBMIT_PATH = "/v2/ivh/videomaker/broadcastservice/phototovideonotrain"
PROGRESS_PATH = "/v2/ivh/videomaker/broadcastservice/getprogress"

# 默认参数
DEFAULT_TIMEOUT_SEC = 30
DEFAULT_POLL_INTERVAL_SEC = 8
DEFAULT_POLL_MAX_WAIT_SEC = 1800  # 30 分钟硬上限


class TencentAvatarError(RuntimeError):
    """所有照片免训练相关的客户端异常的基类。"""


@dataclass
class SubmitResult:
    task_id: str
    raw: dict = field(default_factory=dict)


@dataclass
class ProgressResult:
    """getprogress 接口的结构化返回。"""
    status: str               # COMMIT / MAKING / SUCCESS / FAIL
    array_count: int = 0      # 排队位次（COMMIT 阶段才有意义）
    media_url: str = ""       # 成片 URL（7 天有效，除非传 VideoStorageS3Url）
    subtitles_url: str = ""   # SRT 字幕 URL
    duration_ms: int = 0      # 视频时长（毫秒）
    word_timestamps: list[dict] = field(default_factory=list)  # 逐字时间戳
    fail_reason: str = ""
    raw: dict = field(default_factory=dict)


# --------------------------------------------------------------------------- #
# 鉴权：HMAC-SHA256(accessToken, sorted_query) → Base64 → URL encode
# --------------------------------------------------------------------------- #
def _gen_signed_url(*, base_url: str, app_key: str, access_token: str,
                    extra_query: Optional[dict[str, str]] = None) -> str:
    """按 aPaas 规范生成已签名的完整 URL。

    extra_query 用于 wss 长链接接口（需 requestid），普通 HTTPS 接口不传。
    """
    params: dict[str, str] = {
        "appkey": app_key,
        "timestamp": str(int(time.time())),
    }
    if extra_query:
        params.update({k: str(v) for k, v in extra_query.items()})

    # 字典序拼接被签内容
    signing_content = "&".join(f"{k}={params[k]}" for k in sorted(params.keys()))

    # HMAC-SHA256(secret=accessToken, msg=signing_content)
    h = hmac.new(access_token.encode("utf-8"),
                 signing_content.encode("utf-8"),
                 hashlib.sha256)
    sig_b64 = base64.b64encode(h.digest()).decode("ascii")
    sig_encoded = quote(sig_b64, safe="")  # URL encode（safe=空，连 = 也编）

    return f"{base_url}?{signing_content}&signature={sig_encoded}"


def _post(
    *, path: str, app_key: str, access_token: str, payload: dict,
    timeout: int = DEFAULT_TIMEOUT_SEC,
) -> dict:
    """统一 POST：URL 签名 + body 包成 {Header:{}, Payload:{...}}。"""
    if not app_key:
        raise TencentAvatarError("AppKey 未配置")
    if not access_token:
        raise TencentAvatarError("AccessToken 未配置（仅 AppKey 无法签名）")

    url = _gen_signed_url(base_url=BASE_URL + path,
                          app_key=app_key, access_token=access_token)
    body = {"Header": {}, "Payload": payload}
    try:
        r = requests.post(
            url, json=body,
            headers={"Content-Type": "application/json"},
            timeout=timeout,
        )
    except requests.RequestException as e:
        raise TencentAvatarError(f"网络错误 {path}: {e}") from e

    if r.status_code != 200:
        raise TencentAvatarError(
            f"HTTP {r.status_code} on {path}: {r.text[:300]}"
        )
    data = r.json()
    # 网关层错误：header.code != 0
    header = data.get("Header") or data.get("header") or {}
    code = header.get("code") or header.get("Code") or 0
    if code:
        msg = header.get("message") or header.get("Message") or ""
        raise TencentAvatarError(f"业务失败 code={code} msg={msg} raw={data}")
    return data


def submit_photo_to_video(
    *,
    app_key: str,
    access_token: str,
    photo_url: str,
    text: str = "",
    voice_url: str = "",
    timbre_key: str = "",
    speech_speed: float = 1.0,
    resolution: str = "720P",         # "720P" / "1080P"（内部转 0/1）
    prompt: str = "",                  # 人物表现 prompt
    callback_url: str = "",
    video_storage_s3_url: str = "",   # 指定 COS 推送（可选）
    timeout: int = DEFAULT_TIMEOUT_SEC,
) -> SubmitResult:
    """提交「照片免训练」任务，立即返回 TaskId。

    驱动方式二选一：
    - 文本驱动：传 text（≤300 字），必须配 timbre_key（如 "male_1" / "female_1"）
    - 音频驱动：传 voice_url（2-60 秒，≤10MB）

    字段名/层级严格按官方文档对齐，常见误写：
      PhotoUrl → RefPhotoUrl ；Text → InputSsml ；OriginalVoice 字段 → InputAudioUrl
      DriverType 必填 ：Text / OriginalVoice
      Resolution 必须是整数（0=720P 默认，1=1080P）
    """
    if not photo_url:
        raise TencentAvatarError("photo_url 必填")
    if not (text or voice_url):
        raise TencentAvatarError("text 与 voice_url 至少传一个")
    if text and not timbre_key:
        raise TencentAvatarError("文本驱动必须传 timbre_key（如 'male_1' / 'female_1'）")

    # Resolution: int 0/1
    res_int = 1 if str(resolution).strip().lower() in ("1080p", "1080") else 0

    payload: dict[str, Any] = {
        "RefPhotoUrl": photo_url,
        "VideoParam": {"Resolution": res_int},
    }
    if prompt:
        payload["VideoParam"]["Prompt"] = prompt

    if text:
        payload["DriverType"] = "Text"
        payload["InputSsml"] = text
        payload["SpeechParam"] = {
            "TimbreKey": timbre_key,
            "Speed": speech_speed,
        }
    else:
        payload["DriverType"] = "OriginalVoice"
        payload["InputAudioUrl"] = voice_url

    if callback_url:
        payload["CallbackUrl"] = callback_url
    if video_storage_s3_url:
        payload["VideoStorageS3Url"] = video_storage_s3_url

    log.info("submit photo_to_video resolution=%s text_len=%d voice=%s timbre=%s",
             resolution, len(text), bool(voice_url), timbre_key)
    data = _post(path=SUBMIT_PATH, app_key=app_key, access_token=access_token,
                 payload=payload, timeout=timeout)

    # 兼容多种返回结构层
    candidates = [
        data,
        data.get("Payload") or {},
        data.get("payload") or {},
        data.get("Data") or {},
        data.get("Response") or {},
    ]
    task_id = ""
    for c in candidates:
        if not isinstance(c, dict):
            continue
        task_id = (c.get("TaskId") or c.get("taskId") or "")
        if task_id:
            break
    if not task_id:
        raise TencentAvatarError(f"submit 未拿到 TaskId：{data}")
    return SubmitResult(task_id=task_id, raw=data)


def get_progress(*, app_key: str, access_token: str, task_id: str,
                 timeout: int = DEFAULT_TIMEOUT_SEC) -> ProgressResult:
    """查询任务进度。"""
    if not task_id:
        raise TencentAvatarError("task_id 不能为空")
    data = _post(path=PROGRESS_PATH, app_key=app_key,
                 access_token=access_token,
                 payload={"TaskId": task_id}, timeout=timeout)
    # 兼容三种可能的包裹层
    body = (
        data.get("Payload")
        or data.get("payload")
        or data.get("Data")
        or data.get("Response")
        or data
    )

    return ProgressResult(
        status=str(body.get("Status") or body.get("status") or "").upper(),
        array_count=int(body.get("ArrayCount") or body.get("arrayCount") or 0),
        media_url=str(body.get("MediaUrl") or body.get("mediaUrl") or ""),
        subtitles_url=str(body.get("SubtitlesUrl") or body.get("subtitlesUrl") or ""),
        duration_ms=int(body.get("Duration") or body.get("duration") or 0),
        word_timestamps=list(body.get("TextTimestampResult") or []),
        fail_reason=str(body.get("FailReason") or body.get("failReason") or ""),
        raw=data,
    )


def wait_until_done(
    *,
    app_key: str,
    access_token: str,
    task_id: str,
    poll_interval: int = DEFAULT_POLL_INTERVAL_SEC,
    max_wait: int = DEFAULT_POLL_MAX_WAIT_SEC,
    on_progress: Optional[Any] = None,
) -> ProgressResult:
    """阻塞轮询直到 SUCCESS/FAIL 或超时。"""
    t0 = time.time()
    last_status = ""
    while True:
        elapsed = time.time() - t0
        if elapsed > max_wait:
            raise TencentAvatarError(
                f"等待超过 {max_wait}s 仍未完成（last_status={last_status}）"
            )
        p = get_progress(app_key=app_key, access_token=access_token,
                         task_id=task_id)
        if p.status != last_status:
            log.info("[%s] status=%s array_count=%d elapsed=%.1fs",
                     task_id, p.status, p.array_count, elapsed)
        last_status = p.status
        if on_progress:
            try:
                on_progress(p, elapsed)
            except Exception:  # noqa: BLE001
                pass
        if p.status == "SUCCESS":
            return p
        if p.status == "FAIL":
            raise TencentAvatarError(
                f"任务失败 task_id={task_id} reason={p.fail_reason or p.raw}"
            )
        time.sleep(poll_interval)
