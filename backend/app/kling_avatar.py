"""可灵 Kling 原厂 API 封装（路 A：数字人口型同步）。

完整链路（与腾讯云 VOD 独立，直连可灵开放平台）：
  1) image2video  : 医生形象图 + "动作克制" Prompt → 一段嘴部自然、轻微点头的基础视频
  2) identify-face: 基础视频 → session_id + 人脸列表（精准锁定人脸）
  3) advanced-lip-sync: session_id + 口播音频(base64) → 口型严格同步的成片

设计要点：
- 鉴权：AK/SK 本地签 JWT（HS256，30 分钟有效），每次请求带 Bearer。
- identify-face 需要"公网可拉取"的基础视频 URL → 由本服务的 artifact 端点暴露，
  调用方需提供 video_url（绝对地址）。
- advanced-lip-sync 要求音频结束时间 ≤ 基础视频时长，故调用方需保证基础视频
  已循环到 ≥ 音频时长（见 orchestrator 调用处）。
- 所有耗时任务（image2video / advanced-lip-sync）内部轮询到终态。

密钥仅从 Settings(env) 读取，绝不硬编码。
"""
from __future__ import annotations

import base64
import logging
import time
from pathlib import Path
from typing import Any, Callable, Optional

import jwt
import requests

from .config import Settings, get_settings

log = logging.getLogger("video-agent.kling")

# 进度回调：progress(percent:int, message:str) -> None（可选）
ProgressFn = Callable[[int, str], None]


class KlingError(RuntimeError):
    """可灵 API 调用异常。"""


# --------------------------------------------------------------------------- #
# 鉴权
# --------------------------------------------------------------------------- #
def _jwt_token(s: Settings, ttl: int = 1800) -> str:
    if not (s.kling_access_key and s.kling_secret_key):
        raise KlingError("未配置 KLING_ACCESS_KEY / KLING_SECRET_KEY")
    now = int(time.time())
    payload = {"iss": s.kling_access_key, "exp": now + ttl, "nbf": now - 5}
    return jwt.encode(
        payload, s.kling_secret_key, algorithm="HS256",
        headers={"alg": "HS256", "typ": "JWT"},
    )


def _headers(s: Settings) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {_jwt_token(s)}",
        "Content-Type": "application/json",
    }


def _post(s: Settings, path: str, body: dict[str, Any], timeout: float = 60.0) -> dict[str, Any]:
    url = s.kling_base_url.rstrip("/") + path
    try:
        r = requests.post(url, headers=_headers(s), json=body, timeout=timeout)
    except requests.RequestException as e:  # noqa: BLE001
        raise KlingError(f"请求失败 {path}：{e}") from e
    try:
        j = r.json()
    except ValueError as e:
        raise KlingError(f"{path} 返回非 JSON（HTTP {r.status_code}）：{r.text[:200]}") from e
    if j.get("code") != 0:
        raise KlingError(f"{path} 失败 code={j.get('code')} msg={j.get('message')}")
    return j.get("data") or {}


def _get(s: Settings, path: str, timeout: float = 30.0) -> dict[str, Any]:
    url = s.kling_base_url.rstrip("/") + path
    try:
        r = requests.get(url, headers=_headers(s), timeout=timeout)
    except requests.RequestException as e:  # noqa: BLE001
        raise KlingError(f"请求失败 {path}：{e}") from e
    try:
        j = r.json()
    except ValueError as e:
        raise KlingError(f"{path} 返回非 JSON（HTTP {r.status_code}）：{r.text[:200]}") from e
    if j.get("code") != 0:
        raise KlingError(f"{path} 失败 code={j.get('code')} msg={j.get('message')}")
    return j.get("data") or {}


# --------------------------------------------------------------------------- #
# 通用任务轮询
# --------------------------------------------------------------------------- #
def _poll_task(
    s: Settings,
    query_path: str,
    *,
    label: str,
    progress: Optional[ProgressFn] = None,
    base_percent: int = 0,
    span_percent: int = 20,
) -> dict[str, Any]:
    """轮询某个异步任务到终态，返回 task_result。

    query_path 形如 /v1/videos/image2video/{task_id}。
    """
    deadline = time.time() + s.kling_poll_timeout_sec
    start = time.time()
    tick = 0
    while time.time() < deadline:
        data = _get(s, query_path)
        status = (data.get("task_status") or "").lower()
        msg = data.get("task_status_msg") or ""
        elapsed = int(time.time() - start)
        if status == "succeed":
            return data.get("task_result") or {}
        if status == "failed":
            raise KlingError(f"{label} 任务失败：{msg}")
        # submitted / processing
        if progress:
            pct = min(base_percent + tick * 2, base_percent + span_percent)
            progress(pct, f"{label} 中…（{status} · 已 {elapsed}s）")
        tick += 1
        time.sleep(s.kling_poll_interval_sec)
    raise KlingError(f"{label} 轮询超时（>{s.kling_poll_timeout_sec}s）")


# --------------------------------------------------------------------------- #
# 1) image2video：医生图 → 动作克制的基础视频
# --------------------------------------------------------------------------- #
def image_to_video(
    s: Settings,
    image_url: str,
    *,
    progress: Optional[ProgressFn] = None,
) -> str:
    """提交 image2video（动作克制 Prompt），轮询完成，返回成片的可灵 CDN URL。"""
    body = {
        "model_name": s.kling_image_model,
        "image": image_url,
        "prompt": s.kling_base_prompt,
        "negative_prompt": s.kling_base_negative_prompt,
        "cfg_scale": s.kling_base_cfg_scale,
        "duration": s.kling_base_duration,
        "mode": s.kling_base_mode,
    }
    data = _post(s, "/v1/videos/image2video", body)
    task_id = data.get("task_id")
    if not task_id:
        raise KlingError(f"image2video 未返回 task_id：{data}")
    log.info("Kling image2video 已提交 task_id=%s", task_id)
    result = _poll_task(
        s, f"/v1/videos/image2video/{task_id}",
        label="生成基础视频", progress=progress, base_percent=30, span_percent=20,
    )
    videos = result.get("videos") or []
    if not videos or not videos[0].get("url"):
        raise KlingError(f"image2video 完成但无视频 URL：{result}")
    url = videos[0]["url"]
    log.info("Kling image2video 完成 duration=%s", videos[0].get("duration"))
    return url


# --------------------------------------------------------------------------- #
# 2) identify-face：基础视频 → session_id + 人脸
# --------------------------------------------------------------------------- #
def identify_face(s: Settings, video_url: str) -> tuple[str, list[dict]]:
    """对基础视频做人脸识别，返回 (session_id, face_data)。

    video_url 必须是可灵可公网拉取的地址。
    """
    data = _post(s, "/v1/videos/identify-face", {"video_url": video_url}, timeout=60)
    session_id = data.get("session_id") or ""
    faces = data.get("face_data") or []
    if not session_id:
        raise KlingError(f"identify-face 未返回 session_id：{data}")
    if not faces:
        raise KlingError("identify-face 未检测到人脸（请检查基础视频画面）")
    log.info("Kling identify-face session_id=%s faces=%d", session_id, len(faces))
    return session_id, faces


# --------------------------------------------------------------------------- #
# 3) advanced-lip-sync：session + 音频 → 口型同步成片
# --------------------------------------------------------------------------- #
def advanced_lip_sync(
    s: Settings,
    session_id: str,
    audio_path: Path,
    *,
    audio_ms: int,
    face_id: str = "0",
    progress: Optional[ProgressFn] = None,
) -> str:
    """提交高级对口型（精准人脸驱动），轮询完成，返回成片可灵 CDN URL。

    audio_ms：音频时长（毫秒），需 ≤ 基础视频时长（否则接口报错）。
    """
    audio_b64 = base64.b64encode(Path(audio_path).read_bytes()).decode()
    body = {
        "session_id": session_id,
        "face_choose": [{
            "face_id": face_id,
            "sound_file": audio_b64,
            "sound_start_time": 0,
            "sound_end_time": audio_ms,
            "sound_insert_time": 0,
            "sound_volume": 1.0,
            "original_audio_volume": 0.0,   # 不要基础视频原声（它本身无声/静默）
        }],
    }
    data = _post(s, "/v1/videos/advanced-lip-sync", body, timeout=90)
    task_id = data.get("task_id")
    if not task_id:
        raise KlingError(f"advanced-lip-sync 未返回 task_id：{data}")
    log.info("Kling advanced-lip-sync 已提交 task_id=%s", task_id)
    result = _poll_task(
        s, f"/v1/videos/advanced-lip-sync/{task_id}",
        label="口型同步", progress=progress, base_percent=55, span_percent=25,
    )
    videos = result.get("videos") or []
    if not videos or not videos[0].get("url"):
        raise KlingError(f"advanced-lip-sync 完成但无视频 URL：{result}")
    url = videos[0]["url"]
    log.info("Kling advanced-lip-sync 完成 duration=%s", videos[0].get("duration"))
    return url


# --------------------------------------------------------------------------- #
# 下载
# --------------------------------------------------------------------------- #
def download(url: str, out_path: Path, timeout: float = 180.0) -> Path:
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with requests.get(url, stream=True, timeout=timeout) as r:
            r.raise_for_status()
            with open(out_path, "wb") as f:
                for chunk in r.iter_content(8192):
                    f.write(chunk)
    except requests.RequestException as e:  # noqa: BLE001
        raise KlingError(f"下载成片失败：{e}") from e
    return out_path


def credentials_ready(s: Optional[Settings] = None) -> bool:
    s = s or get_settings()
    return bool(s.kling_access_key and s.kling_secret_key)
