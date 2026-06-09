"""短视频制作 Agent · 引擎二：腾讯云数智人「照片免训练」编排。

定位：
- 与 v1.1 Kling 路 A 平级的引擎选项，由 settings.video_backend=tencent_avatar 触发
- 调用栈：node_generate → _generate_tencent_avatar（本模块）
- v1.1 Kling 路 A 完全保留，互不影响

链路：
  1) 校验配置（AppKey + AccessToken + PUBLIC_BASE_URL）
  2) photo URL 用现成的 /api/doctors/{key}/image
  3) 提交（带重试）：DriverType=Text + InputSsml(narration) + SpeechParam(TimbreKey/Speed)
  4) 轮询 getprogress 直到 SUCCESS
  5) 下载 MediaUrl 到 job 目录
  6) FFmpeg cover 模式裁切到 9:16 + 烧字幕（字幕条目按 storyboard 估时长，因为接口的 SubtitlesUrl 实测为空）
  7) 落 out.mp4，复用 /api/video/jobs/{id}/file 同款 URL 暴露

注意事项（PoC 实测固化）：
- 100008 LimitExceeded 时任务**可能已入队**，触发自动重试（间隔 SUBMIT_RETRY_INTERVAL_SEC）
- 输出比例 ≈ 5:7.4，需 cover 中心裁切到 9:16
- TTS 偏慢 31%，默认 Speed=1.2 抵消
- 文本 ≤ 300 字（接口硬限制），超过会直接报错
"""
from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Callable, Optional

from . import composer, doctors as doctors_mod, tencent_avatar

log = logging.getLogger("video-agent.tencent_avatar_pipeline")

# 接口硬限：文本驱动 ≤ 300 字
MAX_TEXT_LEN = 300


class PipelineError(RuntimeError):
    """数智人编排专用异常。"""


ProgressCb = Callable[[int, str], None]


def credentials_ready(settings) -> bool:
    return bool(getattr(settings, "tencent_avatar_app_key", "")
                and getattr(settings, "tencent_avatar_access_token", ""))


def _submit_with_retry(
    *,
    settings,
    photo_url: str,
    text: str,
    timbre_key: str,
    progress: Optional[ProgressCb],
) -> str:
    """带重试的提交。

    试用账号并发=1，遇到 100008 LimitExceeded（任务名义上"超额"但实际可能已入队）时：
    - 等 SUBMIT_RETRY_INTERVAL_SEC 后重试（默认 30s）
    - 最多 SUBMIT_RETRIES 次（默认 6 → 总等待 3 分钟）

    其他错误立刻抛。
    """
    retries = getattr(settings, "tencent_avatar_submit_retries", 6)
    interval = getattr(settings, "tencent_avatar_submit_retry_interval_sec", 30)
    speed = getattr(settings, "tencent_avatar_speech_speed", 1.2)
    resolution = getattr(settings, "tencent_avatar_resolution", "720P")

    last_exc: Optional[Exception] = None
    for attempt in range(1, retries + 2):  # 首次不算重试
        try:
            sub = tencent_avatar.submit_photo_to_video(
                app_key=settings.tencent_avatar_app_key,
                access_token=settings.tencent_avatar_access_token,
                photo_url=photo_url,
                text=text,
                timbre_key=timbre_key,
                speech_speed=speed,
                resolution=resolution,
            )
            return sub.task_id
        except tencent_avatar.TencentAvatarError as e:
            msg = str(e)
            last_exc = e
            if "100008" in msg or "LimitExceeded" in msg:
                if attempt > retries:
                    break
                if progress:
                    progress(
                        45,
                        f"数智人并发已满，等 {interval}s 重试（{attempt}/{retries}）"
                    )
                log.info("submit LimitExceeded, retry %d/%d after %ds",
                         attempt, retries, interval)
                time.sleep(interval)
                continue
            # 非配额错误直接抛
            raise PipelineError(f"数智人 submit 失败：{e}") from e
    raise PipelineError(f"数智人提交重试 {retries} 次仍受限：{last_exc}")


def _poll_until_done(
    *,
    settings,
    task_id: str,
    progress: Optional[ProgressCb],
) -> tencent_avatar.ProgressResult:
    """轮询 getprogress 直到 SUCCESS / FAIL / 超时。"""
    interval = getattr(settings, "tencent_avatar_poll_interval_sec", 8)
    timeout = getattr(settings, "tencent_avatar_poll_timeout_sec", 900)

    t0 = time.time()
    last_status = ""
    pct_floor = 55
    while True:
        elapsed = time.time() - t0
        if elapsed > timeout:
            raise PipelineError(
                f"数智人轮询超时（{timeout}s）task_id={task_id} last_status={last_status}"
            )
        try:
            p = tencent_avatar.get_progress(
                app_key=settings.tencent_avatar_app_key,
                access_token=settings.tencent_avatar_access_token,
                task_id=task_id,
            )
        except tencent_avatar.TencentAvatarError as e:
            log.warning("getprogress 异常（将重试）：%s", e)
            time.sleep(interval)
            continue

        if p.status != last_status:
            log.info("[%s] %s array=%d elapsed=%.1fs",
                     task_id, p.status, p.array_count, elapsed)
        last_status = p.status

        if progress:
            # 让 UI 看到爬升：COMMIT 55→65，MAKING 65→85
            if p.status == "COMMIT":
                pct = min(pct_floor + int(elapsed / 10), 65)
                progress(pct,
                         f"数智人排队中…（位次 {p.array_count}，已 {int(elapsed)}s）")
            elif p.status == "MAKING":
                pct = min(65 + int(elapsed / 8), 85)
                progress(pct, f"数智人渲染中…（已 {int(elapsed)}s）")

        if p.status == "SUCCESS":
            return p
        if p.status == "FAIL":
            raise PipelineError(
                f"数智人任务失败 task_id={task_id} reason={p.fail_reason or p.raw}"
            )
        time.sleep(interval)


def run(
    *,
    settings,
    doctor_key: str,
    narration: str,
    storyboard,
    job_id: str,
    progress: Optional[ProgressCb] = None,
) -> dict:
    """跑完整的数智人编排，返回成片信息：
    {
      "out_path": Path,
      "task_id": str,
      "duration_sec": float,
      "width": int,
      "height": int,
      "media_url_remote": str,   # 接口的远端 URL（7 天有效）
    }

    异常都抛 PipelineError，graph 节点统一处理。
    """
    # ---- 1) 前置校验 ----
    if not credentials_ready(settings):
        raise PipelineError("数智人未就绪：TENCENT_AVATAR_APP_KEY / ACCESS_TOKEN 未配置")

    base_public = (
        getattr(settings, "tencent_avatar_public_base_url", "")
        or getattr(settings, "public_base_url", "")
        or getattr(settings, "kling_public_base_url", "")
    ).rstrip("/")
    if not base_public:
        raise PipelineError(
            "数智人需配置 PUBLIC_BASE_URL（接口要公网拉医生形象图）"
        )

    doctor = doctors_mod.get_doctor(doctor_key) or doctors_mod.get_doctor(
        getattr(settings, "default_doctor", "")
    )
    if not doctor or not doctor.exists:
        raise PipelineError(f"医生形象图缺失：{doctor_key}")

    text = (narration or "").strip()
    if not text:
        raise PipelineError("数智人需要 narration 文本（不能空）")
    if len(text) > MAX_TEXT_LEN:
        raise PipelineError(
            f"数智人文本超限：{len(text)} > {MAX_TEXT_LEN}（接口硬限制）"
        )

    timbre = doctors_mod.avatar_timbre_for_doctor(
        doctor.key,
        fallback=getattr(settings, "tencent_avatar_default_timbre", "male_1"),
    )

    job_dir = Path(__file__).resolve().parent.parent / ".cache" / "jobs" / job_id
    job_dir.mkdir(parents=True, exist_ok=True)
    raw_path = job_dir / "tencent_avatar_raw.mp4"
    out_path = job_dir / "out.mp4"

    # ---- 2) 提交（带重试）----
    photo_url = f"{base_public}/api/doctors/{doctor.key}/image"
    if progress:
        progress(40, f"数智人提交任务（{doctor.name} · {timbre}）")
    task_id = _submit_with_retry(
        settings=settings, photo_url=photo_url,
        text=text, timbre_key=timbre, progress=progress,
    )
    log.info("数智人 TaskId=%s doctor=%s timbre=%s", task_id, doctor.key, timbre)

    # ---- 3) 轮询 ----
    if progress:
        progress(55, f"数智人任务已入队 TaskId={task_id[:8]}…")
    done = _poll_until_done(settings=settings, task_id=task_id, progress=progress)

    if not done.media_url:
        raise PipelineError(f"数智人 SUCCESS 但未拿到 MediaUrl：{done.raw}")

    # ---- 4) 下载远端 ----
    if progress:
        progress(87, "下载数智人成片")
    try:
        composer.download_to(done.media_url, raw_path)
    except Exception as e:  # noqa: BLE001
        raise PipelineError(f"下载数智人成片失败：{e}") from e

    # ---- 5) 后处理：cover 裁切到 9:16 + 烧字幕 ----
    if progress:
        progress(91, "规整到 9:16 并烧录字幕")

    target_aspect = getattr(settings, "tencent_avatar_target_aspect", "9:16")
    fit_mode = getattr(settings, "tencent_avatar_fit_mode", "cover")
    target_w, target_h = composer.VIDEO_W, composer.VIDEO_H
    # 简单 case：9:16 用 composer 默认；非 9:16 由调用方扩展
    if str(target_aspect).strip() != "9:16":
        log.warning("当前仅支持 9:16，忽略 target_aspect=%s", target_aspect)

    try:
        composer.burn_captions_to_aspect(
            raw_path, out_path,
            storyboard=storyboard,
            target_w=target_w, target_h=target_h,
            fit_mode=fit_mode,
        )
    except composer.ComposerError as e:
        raise PipelineError(f"数智人后处理失败：{e}") from e

    duration = composer.probe_duration(out_path)
    return {
        "out_path": out_path,
        "task_id": task_id,
        "duration_sec": round(duration, 2),
        "width": target_w,
        "height": target_h,
        "media_url_remote": done.media_url,
    }
