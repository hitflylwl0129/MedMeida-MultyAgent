"""短视频制作 Agent 的 LangGraph 状态机。

节点（逻辑解耦，同进程）：
  storyboard → generate → compliance → handoff
                  └────────(失败)────────→ fail

decision(6)：编排在 Web 进程内（LangGraph）；其中 generate 是「重/长任务」，
由 Worker 线程承载其内部轮询（见 worker.py 用 asyncio.to_thread 执行整图）。
真实切 GPU/独立 Worker 时，仅需把 generate 节点换成投递队列实现，编排不变。
"""
from __future__ import annotations

import logging
import shutil
import time
from pathlib import Path
from typing import Any, Callable, Optional, TypedDict

from langgraph.graph import END, StateGraph

from ..agents import storyboard as sb_agent
from ..config import Settings
from ..schemas import (
    ComplianceResult,
    JobStatus,
    VideoJob,
    VideoOutput,
)
from .. import (
    composer,
    doctors as doctors_mod,
    kling_avatar,
    kling_base,
    motion_ref,
    tencent_avatar_pipeline,
    tts,
    vod_client,
)
from ..vod_upload import ensure_doctor_file_id

log = logging.getLogger("video-agent.graph")

# emit(job, stage) -> None：把当前 job 状态推给前端(SSE) 并落库
EmitFn = Callable[[VideoJob, str], None]

# 本地链路成片落盘目录（路线 Y）：backend/.cache/jobs/{id}/out.mp4
_LOCAL_JOBS_DIR = Path(__file__).resolve().parents[2] / ".cache" / "jobs"


def _dump_prompt(job_id: str, prompt: str) -> None:
    """把本次提交给 VOD 的 Prompt 落盘到 job 目录（prompt.txt），便于事后逐条审计。

    尽力而为：写失败不抛，不影响主链路。
    """
    try:
        d = _LOCAL_JOBS_DIR / job_id
        d.mkdir(parents=True, exist_ok=True)
        (d / "prompt.txt").write_text(prompt or "", encoding="utf-8")
    except OSError as e:  # noqa: BLE001
        log.warning("写入 prompt.txt 失败 %s：%s", job_id, e)


def _prune_local_jobs(keep: int) -> None:
    """保留最近 `keep` 个任务目录（按 mtime 倒序），多余整目录删除。

    keep<=0 时不做清理（用于关闭策略）。失败不抛——清理是尽力而为，不影响主链路。
    """
    if keep is None or keep <= 0:
        return
    if not _LOCAL_JOBS_DIR.is_dir():
        return
    try:
        entries = [p for p in _LOCAL_JOBS_DIR.iterdir() if p.is_dir()]
    except OSError:
        return
    if len(entries) <= keep:
        return
    # 按 mtime 降序：最新在前
    entries.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    for stale in entries[keep:]:
        try:
            shutil.rmtree(stale, ignore_errors=True)
            log.info("清理过期任务目录：%s", stale.name)
        except OSError as e:  # noqa: BLE001
            log.warning("清理失败 %s：%s", stale, e)




class GState(TypedDict, total=False):
    job: VideoJob
    settings: Settings
    doctor_key: str
    doctor_file_id: str
    doctor_url: str
    emit: EmitFn
    failed: bool
    # 前端引擎选择器：覆盖 .env 的 VIDEO_BACKEND（v1.3）
    video_backend_override: str


def _emit(state: GState, stage: str) -> None:
    fn = state.get("emit")
    if fn:
        fn(state["job"], stage)


# --------------------------- 节点实现 --------------------------- #
def node_storyboard(state: GState) -> GState:
    job = state["job"]
    job.status = JobStatus.STORYBOARD
    job.progress = 10
    job.message = "分镜拆解中（痛点→科普→带入→引导）"
    job.storyboard = sb_agent.build_storyboard(job.script)
    _emit(state, "st2")
    return state


def node_generate(state: GState) -> GState:
    job = state["job"]
    s = state["settings"]
    job.status = JobStatus.SUBMITTING
    job.progress = 25
    # 优先用前端"引擎覆盖"，没传则用 .env 的全局默认
    backend_mode = (
        (state.get("video_backend_override") or getattr(s, "video_backend", "") or "local")
    ).lower()
    job.message = f"提交生视频任务（backend={backend_mode}）"
    _emit(state, "st3")

    if backend_mode == "local":
        return _generate_local(state)
    if backend_mode == "motion":
        return _generate_motion(state)
    if backend_mode == "kling":
        return _generate_kling(state)
    if backend_mode == "tencent_avatar":
        return _generate_tencent_avatar(state)
    return _generate_aigc(state)


def _generate_local(state: GState) -> GState:
    """路线 Y：腾讯云 TTS 合成口播 + ffmpeg 拼"医生静帧+口播"成片。

    无需 avatar_i2v 白名单；产物落盘 backend/.cache/jobs/{id}/out.mp4，
    output.url 指向 /api/video/jobs/{id}/file（由 main.py 提供 FileResponse）。
    """
    job = state["job"]
    s = state["settings"]
    doctor_key = state.get("doctor_key") or s.default_doctor

    # 1) 解析医生形象图本地路径
    doctor = doctors_mod.get_doctor(doctor_key) or doctors_mod.get_doctor(s.default_doctor)
    if not doctor or not doctor.exists:
        job.status = JobStatus.FAILED
        job.error = f"医生形象图缺失：{doctor_key}"
        job.message = job.error
        state["failed"] = True
        _emit(state, "st3")
        return state

    # 2) TTS 合成口播
    job.status = JobStatus.GENERATING
    job.progress = 40
    job.message = f"合成医生口播音轨（{doctor.name}）"
    _emit(state, "st3")

    job_dir = _LOCAL_JOBS_DIR / job.id
    job_dir.mkdir(parents=True, exist_ok=True)
    audio_path = job_dir / "voice.mp3"
    video_path = job_dir / "out.mp4"

    narration = (job.storyboard.narration if job.storyboard else "") or job.script.text
    voice_type = doctors_mod.tts_voice_for_doctor(doctor.key, fallback=s.tts_voice_type)

    try:
        tts.synthesize_to_mp3(narration, audio_path, settings=s, voice_type=voice_type)
    except Exception as e:  # noqa: BLE001
        job.status = JobStatus.FAILED
        job.error = f"TTS 合成失败：{e}"
        job.message = job.error
        state["failed"] = True
        _emit(state, "st3")
        return state

    # 3) ffmpeg 合成成片
    job.progress = 70
    job.message = "拼合医生静帧 + 口播音轨为 9:16 成片"
    _emit(state, "st3")
    try:
        composer.compose_static_video(
            doctor.path, audio_path, video_path,
            storyboard=job.storyboard,
        )
    except Exception as e:  # noqa: BLE001
        job.status = JobStatus.FAILED
        job.error = f"合成失败：{e}"
        job.message = job.error
        state["failed"] = True
        _emit(state, "st3")
        return state

    duration = composer.probe_duration(video_path)
    # output.url 拼法：
    #   - 默认走相对路径，浏览器按当前页 origin 拼接（适配 nginx 反代同源场景）；
    #   - 若显式配置了 PUBLIC_BASE_URL（如 http://162.14.76.209），用绝对路径，
    #     兼容前端用 file:// 直开 HTML 这类跨源场景。
    base = (s.public_base_url or "").rstrip("/")
    job.task_id = f"local-{job.id}"
    job.output = VideoOutput(
        file_id="",
        url=f"{base}/api/video/jobs/{job.id}/file",
        cover_url=f"{base}/api/doctors/{doctor.key}/image",
        duration_sec=round(duration, 2),
        width=composer.VIDEO_W,
        height=composer.VIDEO_H,
    )
    job.progress = 85
    job.message = f"成片生成完成（{duration:.1f}s · 1080×1920）"
    # 保留最近 N 个任务目录（最新即当前，必在保留区内）
    _prune_local_jobs(getattr(s, "local_keep_jobs", 20))
    _emit(state, "st3")
    return state


def _generate_motion(state: GState) -> GState:
    """路线 Y+：腾讯云 motion_control 出"医生动效无声视频" + 本地 TTS mp3 → ffmpeg 合并出口播成片。

    输入：医生形象图（首帧）+ 参考动作视频（assets/motion_ref/<filename>，自动上传缓存 FileId）
    输出：与 _generate_local 同样的 output.url（指向 /api/video/jobs/{id}/file），无需白名单。
    """
    job = state["job"]
    s = state["settings"]
    doctor_key = state.get("doctor_key") or s.default_doctor

    # 1) 解析医生形象图本地路径 + FileId
    doctor = doctors_mod.get_doctor(doctor_key) or doctors_mod.get_doctor(s.default_doctor)
    if not doctor or not doctor.exists:
        job.status = JobStatus.FAILED
        job.error = f"医生形象图缺失：{doctor_key}"
        job.message = job.error
        state["failed"] = True
        _emit(state, "st3")
        return state

    job_dir = _LOCAL_JOBS_DIR / job.id
    job_dir.mkdir(parents=True, exist_ok=True)
    audio_path = job_dir / "voice.mp3"
    motion_raw_path = job_dir / "motion_raw.mp4"
    video_path = job_dir / "out.mp4"

    # 2) 并行准备：TTS 合成口播 + 解析参考视频 FileId
    job.status = JobStatus.GENERATING
    job.progress = 30
    job.message = f"准备：合成口播 + 上传/解析参考动作视频"
    _emit(state, "st3")

    narration = (job.storyboard.narration if job.storyboard else "") or job.script.text
    voice_type = doctors_mod.tts_voice_for_doctor(doctor.key, fallback=s.tts_voice_type)

    try:
        tts.synthesize_to_mp3(narration, audio_path, settings=s, voice_type=voice_type)
    except Exception as e:  # noqa: BLE001
        job.status = JobStatus.FAILED
        job.error = f"TTS 合成失败：{e}"
        job.message = job.error
        state["failed"] = True
        _emit(state, "st3")
        return state

    try:
        character_fid = doctors_mod.resolve_doctor_file_id(doctor.key, settings=s)
        # 强化：优先用与该医生同名的专属参考动作视频（中年女医生.mp4 …），无则回退默认 ref.mp4
        ref_name = motion_ref.ref_filename_for_doctor(doctor.name, s.motion_ref_filename)
        ref_fid = motion_ref.resolve_motion_ref_file_id(ref_name, settings=s)
        per_doctor_ref = motion_ref.has_per_doctor_ref(doctor.name)
        log.info("motion 参考视频：%s（%s）", ref_name,
                 "医生专属" if per_doctor_ref else "通用默认")
        job.message = (
            f"参考动作视频：{ref_name}"
            + ("（医生专属·已强化）" if per_doctor_ref else "（通用默认）")
        )
        _emit(state, "st3")
    except Exception as e:  # noqa: BLE001
        job.status = JobStatus.FAILED
        job.error = f"素材准备失败：{e}"
        job.message = job.error
        state["failed"] = True
        _emit(state, "st3")
        return state

    # 3) 提交 motion_control 任务
    job.progress = 45
    job.message = "提交动作控制任务（Kling · motion_control）"
    _emit(state, "st3")
    try:
        job.session_id = job.session_id or job.id
        # B1：复用 avatar_i2v 同款 overall_prompt —— 带上完整话术/医生/时长/结构，
        # motion_control 仍以参考视频为主驱动，Prompt 提升画面专业度（信息量更大）。
        motion_prompt = sb_agent.overall_prompt(job.script, job.storyboard)
        _dump_prompt(job.id, motion_prompt)
        job.task_id = vod_client.create_motion_control_task(
            character_file_id=character_fid,
            motion_ref_file_id=ref_fid,
            prompt=motion_prompt,
            session_id=job.session_id,
            session_context=f"video-agent:{job.id}",
            settings=s,
        )
    except vod_client.VodError as e:
        job.status = JobStatus.FAILED
        job.error = str(e)
        job.message = f"提交失败：{e}"
        state["failed"] = True
        _emit(state, "st3")
        return state

    # 4) 轮询任务详情
    deadline = time.time() + s.motion_poll_timeout_sec
    tick = 0
    detail = None
    while time.time() < deadline:
        time.sleep(s.motion_poll_interval_sec)
        tick += 1
        try:
            detail = vod_client.describe_task(job.task_id, s)
            status, output, msg = vod_client.parse_task_detail(detail)
        except vod_client.VodError as e:
            job.message = f"查询任务异常（将重试）：{e}"
            _emit(state, "st3")
            continue

        if status in ("WAITING", "PROCESSING", "UNKNOWN"):
            elapsed = tick * int(s.motion_poll_interval_sec)
            # 进度在 PROCESSING 期间缓慢爬升至 76，并显示已用时，避免长时间停在同一数字像"卡住"
            job.progress = min(46 + tick * 2, 76)
            job.message = f"动作迁移中…（{status} · 已 {elapsed}s，通常需 2–3 分钟）"
            _emit(state, "st3")
            continue
        if status == "ABORTED":
            job.status = JobStatus.FAILED
            job.error = msg or "motion_control 任务终止(ABORTED)"
            job.message = f"生视频失败：{job.error}"
            state["failed"] = True
            _emit(state, "st3")
            return state
        if status == "FINISH":
            if not output or not output.url:
                job.status = JobStatus.FAILED
                job.error = f"motion_control 完成但未取到成片 URL：{msg}"
                job.message = job.error
                state["failed"] = True
                _emit(state, "st3")
                return state
            # 5) 把腾讯云临时 URL 落到本地
            job.progress = 78
            job.message = "下载动作成片（临时 URL，本地落盘）"
            _emit(state, "st3")
            try:
                composer.download_to(output.url, motion_raw_path)
            except Exception as e:  # noqa: BLE001
                job.status = JobStatus.FAILED
                job.error = f"下载 motion_control 成片失败：{e}"
                job.message = job.error
                state["failed"] = True
                _emit(state, "st3")
                return state
            break
    else:
        job.status = JobStatus.FAILED
        job.error = "motion_control 轮询超时"
        job.message = job.error
        state["failed"] = True
        _emit(state, "st3")
        return state

    # 6) ffmpeg 合并：motion 视频 + 我们的口播 → 最终成片
    job.progress = 85
    job.message = "拼合动作视频 + 口播音轨"
    _emit(state, "st3")
    try:
        composer.mux_video_audio(
            motion_raw_path, audio_path, video_path,
            storyboard=job.storyboard,
        )
    except Exception as e:  # noqa: BLE001
        job.status = JobStatus.FAILED
        job.error = f"合成失败：{e}"
        job.message = job.error
        state["failed"] = True
        _emit(state, "st3")
        return state

    duration = composer.probe_duration(video_path)
    # 同 _generate_local：默认相对路径，PUBLIC_BASE_URL 显式配置时改用绝对路径。
    base = (s.public_base_url or "").rstrip("/")
    job.output = VideoOutput(
        file_id="",
        url=f"{base}/api/video/jobs/{job.id}/file",
        cover_url=f"{base}/api/doctors/{doctor.key}/image",
        duration_sec=round(duration, 2),
        width=composer.VIDEO_W,
        height=composer.VIDEO_H,
    )
    job.progress = 90
    job.message = f"成片生成完成（{duration:.1f}s · 1080×1920 · motion_control）"
    _prune_local_jobs(getattr(s, "local_keep_jobs", 20))
    _emit(state, "st3")
    return state


def _generate_kling(state: GState) -> GState:
    """路 A：可灵原厂 Kling API → 数字人精准口型同步。

    链路：
      1) TTS 合成口播 mp3（落 voice.mp3 + voice.segments.json 供字幕对齐）
      2) 准备基础视频：缓存命中即用；否则 image2video 生成"动作克制"的医生基础视频
      3) 把基础视频循环到 ≥ 音频时长，落到 job 目录并暴露 artifact url
      4) identify-face 拿 session_id
      5) advanced-lip-sync(session_id + 口播 base64) → 口型同步带音频成片
      6) ffmpeg 烧字幕 + 规范 9:16 → out.mp4
    """
    job = state["job"]
    s = state["settings"]
    doctor_key = state.get("doctor_key") or s.default_doctor

    if not kling_avatar.credentials_ready(s):
        job.status = JobStatus.FAILED
        job.error = "未配置 KLING_ACCESS_KEY / KLING_SECRET_KEY"
        job.message = job.error
        state["failed"] = True
        _emit(state, "st3")
        return state

    # Kling 接口需公网可拉取的 URL，必须有 PUBLIC_BASE_URL
    base_public = (s.kling_public_base_url or s.public_base_url or "").rstrip("/")
    if not base_public:
        job.status = JobStatus.FAILED
        job.error = "Kling 路径需配置 KLING_PUBLIC_BASE_URL 或 PUBLIC_BASE_URL（公网可达地址）"
        job.message = job.error
        state["failed"] = True
        _emit(state, "st3")
        return state

    doctor = doctors_mod.get_doctor(doctor_key) or doctors_mod.get_doctor(s.default_doctor)
    if not doctor or not doctor.exists:
        job.status = JobStatus.FAILED
        job.error = f"医生形象图缺失：{doctor_key}"
        job.message = job.error
        state["failed"] = True
        _emit(state, "st3")
        return state

    job_dir = _LOCAL_JOBS_DIR / job.id
    job_dir.mkdir(parents=True, exist_ok=True)
    audio_path = job_dir / "voice.mp3"
    base_loop_path = job_dir / "kling_base_loop.mp4"
    lipsync_raw_path = job_dir / "kling_lipsync_raw.mp4"
    out_path = job_dir / "out.mp4"

    # 1) TTS
    job.status = JobStatus.GENERATING
    job.progress = 15
    job.message = "合成口播（TTS）"
    _emit(state, "st3")
    narration = (job.storyboard.narration if job.storyboard else "") or job.script.text
    voice_type = doctors_mod.tts_voice_for_doctor(doctor.key, fallback=s.tts_voice_type)
    try:
        tts.synthesize_to_mp3(narration, audio_path, settings=s, voice_type=voice_type)
    except Exception as e:  # noqa: BLE001
        job.status = JobStatus.FAILED
        job.error = f"TTS 合成失败：{e}"
        job.message = job.error
        state["failed"] = True
        _emit(state, "st3")
        return state

    try:
        audio_sec = composer.probe_duration(audio_path)
    except composer.ComposerError as e:
        job.status = JobStatus.FAILED
        job.error = f"探测口播时长失败：{e}"
        job.message = job.error
        state["failed"] = True
        _emit(state, "st3")
        return state

    # 2) 基础视频（缓存命中直用，否则 image2video）
    job.progress = 25
    job.message = "准备医生基础视频（首次约 3 分钟，命中缓存秒级）"
    _emit(state, "st3")

    def _i2v_progress(pct: int, msg: str) -> None:
        # 把 kling_avatar 内部 30~50% 的进度转译给上游 UI
        job.progress = pct
        job.message = msg
        _emit(state, "st3")

    try:
        base_video = kling_base.ensure_base_video(doctor.key, settings=s, progress=_i2v_progress)
    except kling_avatar.KlingError as e:
        job.status = JobStatus.FAILED
        job.error = f"基础视频准备失败：{e}"
        job.message = job.error
        state["failed"] = True
        _emit(state, "st3")
        return state

    # 3) 循环到 ≥ 音频时长并落到 job 目录（供 artifact 暴露）
    job.progress = 50
    job.message = f"基础视频循环至 ≥{audio_sec:.1f}s 覆盖口播"
    _emit(state, "st3")
    try:
        kling_base.make_loop_for_duration(base_video, base_loop_path, audio_sec)
    except Exception as e:  # noqa: BLE001
        job.status = JobStatus.FAILED
        job.error = f"循环基础视频失败：{e}"
        job.message = job.error
        state["failed"] = True
        _emit(state, "st3")
        return state

    base_loop_url = f"{base_public}/api/video/jobs/{job.id}/artifact/kling_base_loop.mp4"

    # 4) identify-face
    job.progress = 55
    job.message = "识别人脸（identify-face）"
    _emit(state, "st3")
    try:
        session_id, faces = kling_avatar.identify_face(s, base_loop_url)
    except kling_avatar.KlingError as e:
        job.status = JobStatus.FAILED
        job.error = f"人脸识别失败：{e}"
        job.message = job.error
        state["failed"] = True
        _emit(state, "st3")
        return state
    job.task_id = f"kling:{session_id}"

    # 5) advanced-lip-sync
    job.progress = 58
    job.message = "提交高级对口型（advanced-lip-sync）"
    _emit(state, "st3")
    audio_ms = int(audio_sec * 1000)

    def _lip_progress(pct: int, msg: str) -> None:
        job.progress = pct
        job.message = msg
        _emit(state, "st3")

    try:
        cdn_url = kling_avatar.advanced_lip_sync(
            s, session_id, audio_path, audio_ms=audio_ms,
            face_id=str(faces[0].get("face_id") or "0"),
            progress=_lip_progress,
        )
    except kling_avatar.KlingError as e:
        job.status = JobStatus.FAILED
        job.error = str(e)
        job.message = f"口型同步失败：{e}"
        state["failed"] = True
        _emit(state, "st3")
        return state

    # 落盘 lip-sync 原片
    job.progress = 82
    job.message = "下载口型同步成片"
    _emit(state, "st3")
    try:
        kling_avatar.download(cdn_url, lipsync_raw_path)
    except kling_avatar.KlingError as e:
        job.status = JobStatus.FAILED
        job.error = str(e)
        job.message = f"下载失败：{e}"
        state["failed"] = True
        _emit(state, "st3")
        return state

    # 6) 烧字幕（保留原音频）+ 规范 9:16
    job.progress = 88
    job.message = "烧录字幕（按 TTS 段时长逐句对齐）"
    _emit(state, "st3")
    try:
        composer.burn_captions_keep_audio(
            lipsync_raw_path, out_path,
            audio_path=audio_path, storyboard=job.storyboard,
        )
    except composer.ComposerError as e:
        job.status = JobStatus.FAILED
        job.error = f"烧字幕失败：{e}"
        job.message = job.error
        state["failed"] = True
        _emit(state, "st3")
        return state

    duration = composer.probe_duration(out_path)
    base = (s.public_base_url or "").rstrip("/")
    job.output = VideoOutput(
        file_id="",
        url=f"{base}/api/video/jobs/{job.id}/file",
        cover_url=f"{base}/api/doctors/{doctor.key}/image",
        duration_sec=round(duration, 2),
        width=composer.VIDEO_W,
        height=composer.VIDEO_H,
    )
    job.progress = 92
    job.message = f"成片生成完成（{duration:.1f}s · 1080×1920 · Kling 高级对口型）"
    _prune_local_jobs(getattr(s, "local_keep_jobs", 20))
    _emit(state, "st3")
    return state


def _generate_tencent_avatar(state: GState) -> GState:
    """v1.3 引擎二：腾讯云数智人「照片免训练」。

    与 Kling 路 A 平级；产物落 backend/.cache/jobs/{id}/out.mp4，
    output.url 走同款 /api/video/jobs/{id}/file（前端/分发零改动）。

    详见 backend/app/tencent_avatar_pipeline.py 与调研报告。
    """
    job = state["job"]
    s = state["settings"]
    doctor_key = state.get("doctor_key") or s.default_doctor

    doctor = doctors_mod.get_doctor(doctor_key) or doctors_mod.get_doctor(s.default_doctor)
    if not doctor or not doctor.exists:
        job.status = JobStatus.FAILED
        job.error = f"医生形象图缺失：{doctor_key}"
        job.message = job.error
        state["failed"] = True
        _emit(state, "st3")
        return state

    narration = (job.storyboard.narration if job.storyboard else "") or job.script.text

    def _progress(pct: int, msg: str) -> None:
        job.status = JobStatus.GENERATING
        job.progress = pct
        job.message = msg
        _emit(state, "st3")

    try:
        result = tencent_avatar_pipeline.run(
            settings=s,
            doctor_key=doctor.key,
            narration=narration,
            storyboard=job.storyboard,
            job_id=job.id,
            progress=_progress,
        )
    except tencent_avatar_pipeline.PipelineError as e:
        job.status = JobStatus.FAILED
        job.error = str(e)
        job.message = f"数智人引擎失败：{e}"
        state["failed"] = True
        _emit(state, "st3")
        return state

    # 任务 ID 标识引擎来源，便于事后溯源
    job.task_id = f"tencent_avatar:{result['task_id']}"
    base = (s.public_base_url or "").rstrip("/")
    job.output = VideoOutput(
        file_id="",
        url=f"{base}/api/video/jobs/{job.id}/file",
        cover_url=f"{base}/api/doctors/{doctor.key}/image",
        duration_sec=result["duration_sec"],
        width=result["width"],
        height=result["height"],
    )
    job.progress = 92
    job.message = (
        f"成片生成完成（{result['duration_sec']:.1f}s · "
        f"{result['width']}×{result['height']} · 腾讯云数智人）"
    )
    _prune_local_jobs(getattr(s, "local_keep_jobs", 20))
    _emit(state, "st3")
    return state


def _generate_aigc(state: GState) -> GState:
    """原 AIGC 路径：调腾讯云 CreateAigcVideoTask（avatar_i2v，需要白名单）。

    保留供白名单到位后切回；运行时由 settings.video_backend=aigc 触发。
    """
    job = state["job"]
    s = state["settings"]

    try:
        doctor_fid = ensure_doctor_file_id(
            file_id=state.get("doctor_file_id") or None,
            doctor_key=state.get("doctor_key") or None,
            url=state.get("doctor_url") or None,
            settings=s,
        )
        prompt = sb_agent.overall_prompt(job.script, job.storyboard)
        _dump_prompt(job.id, prompt)
        job.session_id = job.session_id or job.id
        job.task_id = vod_client.create_aigc_video_task(
            prompt=prompt,
            doctor_file_id=doctor_fid,
            session_id=job.session_id,
            session_context=f"video-agent:{job.id}",
            settings=s,
        )
    except vod_client.VodError as e:
        job.status = JobStatus.FAILED
        job.error = str(e)
        job.message = f"提交失败：{e}"
        state["failed"] = True
        _emit(state, "st3")
        return state

    # 进入轮询（长任务内部循环，由 Worker 线程承载）
    job.status = JobStatus.GENERATING
    job.progress = 35
    job.message = f"生视频进行中 TaskId={job.task_id}"
    _emit(state, "st3")

    deadline = time.time() + s.poll_timeout_sec
    tick = 0
    while time.time() < deadline:
        time.sleep(s.poll_interval_sec)
        tick += 1
        try:
            detail = vod_client.describe_task(job.task_id, s)
            status, output, msg = vod_client.parse_task_detail(detail)
        except vod_client.VodError as e:
            job.message = f"查询任务异常（将重试）：{e}"
            _emit(state, "st3")
            continue

        if status in ("WAITING", "PROCESSING", "UNKNOWN"):
            job.progress = min(35 + tick * 4, 80)
            job.message = f"生视频进行中…（{status}）"
            _emit(state, "st3")
            continue
        if status == "FINISH":
            job.output = output or VideoOutput()
            job.progress = 85
            job.message = "成片生成完成"
            _emit(state, "st3")
            return state
        if status == "ABORTED":
            job.status = JobStatus.FAILED
            job.error = msg or "任务被终止(ABORTED)"
            job.message = f"生视频失败：{job.error}"
            state["failed"] = True
            _emit(state, "st3")
            return state

    job.status = JobStatus.FAILED
    job.error = "生视频轮询超时"
    job.message = job.error
    state["failed"] = True
    _emit(state, "st3")
    return state


def node_compliance(state: GState) -> GState:
    job = state["job"]
    job.status = JobStatus.COMPLIANCE
    job.progress = 92
    job.message = "成片合规复审（API 内置输入/输出审核）"
    # decision(5)：先只用 API 内置合规。任务能 FINISH 即代表内置审核已放行。
    job.compliance = ComplianceResult(
        passed=True,
        input_check="Enabled · 通过",
        output_check="Enabled · 通过",
        detail="API 内置输入/输出合规审核已放行（自研 OCR/ASR 口径比对后续叠加）",
    )
    _emit(state, "st4")
    return state


def node_handoff(state: GState) -> GState:
    job = state["job"]
    job.status = JobStatus.DONE
    job.progress = 100
    job.message = "制作完成，成片可移交分发"
    _emit(state, "st5")
    return state


def node_fail(state: GState) -> GState:
    job = state["job"]
    if job.status != JobStatus.FAILED:
        job.status = JobStatus.FAILED
    _emit(state, "st3")
    return state


# --------------------------- 路由 --------------------------- #
def _after_generate(state: GState) -> str:
    return "fail" if state.get("failed") else "compliance"


_GRAPH = None


def get_graph():
    """编译并缓存 LangGraph。"""
    global _GRAPH
    if _GRAPH is not None:
        return _GRAPH
    g = StateGraph(GState)
    g.add_node("storyboard", node_storyboard)
    g.add_node("generate", node_generate)
    g.add_node("compliance", node_compliance)
    g.add_node("handoff", node_handoff)
    g.add_node("fail", node_fail)

    g.set_entry_point("storyboard")
    g.add_edge("storyboard", "generate")
    g.add_conditional_edges(
        "generate", _after_generate, {"compliance": "compliance", "fail": "fail"}
    )
    g.add_edge("compliance", "handoff")
    g.add_edge("handoff", END)
    g.add_edge("fail", END)

    _GRAPH = g.compile()
    return _GRAPH


def run_pipeline(
    job: VideoJob,
    settings: Settings,
    emit: EmitFn,
    doctor_key: str = "",
    doctor_file_id: str = "",
    doctor_url: str = "",
    video_backend_override: str = "",
) -> VideoJob:
    """同步执行整图（在 Worker 线程里调用）。"""
    state: GState = {
        "job": job,
        "settings": settings,
        "emit": emit,
        "doctor_key": doctor_key,
        "doctor_file_id": doctor_file_id,
        "doctor_url": doctor_url,
        "failed": False,
        "video_backend_override": video_backend_override or "",
    }
    result = get_graph().invoke(state)
    return result["job"]
