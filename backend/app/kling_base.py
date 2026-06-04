"""Kling 基础视频缓存：每位医生一段"动作克制"的基础视频，跨任务复用。

为什么要缓存：
  image2video 单次约 3 分钟 + 1 单位配额。每次新生成视频都重新跑很浪费——
  对同一位医生形象图，配同样的 Prompt，基础视频效果几乎一致，应该缓存复用。

落盘策略：
  backend/.cache/kling_base/{doctor_key}.mp4  — 原始基础视频（10s）
  backend/.cache/kling_base/{doctor_key}.meta.json — 元数据 {duration_sec, created_at}

调用流程：
  ensure_base_video(doctor_key, settings) -> Path
    1) 命中缓存：直接返回
    2) 未命中：取医生形象 URL → image2video → 下载落盘 → 返回

下游 advanced-lip-sync 还需要"基础视频时长 ≥ 音频时长"：
  ensure_base_video_for_duration(...) 会在不够时用 ffmpeg 循环出 _loop.mp4 副本。
"""
from __future__ import annotations

import json
import logging
import shutil
import subprocess
import threading
import time
from pathlib import Path
from typing import Optional

from .config import Settings, get_settings
from . import doctors as doctors_mod, kling_avatar

log = logging.getLogger("video-agent.kling_base")

BACKEND_ROOT = Path(__file__).resolve().parents[1]
CACHE_DIR = BACKEND_ROOT / ".cache" / "kling_base"
_lock = threading.Lock()


def _meta_path(doctor_key: str) -> Path:
    return CACHE_DIR / f"{doctor_key}.meta.json"


def _video_path(doctor_key: str) -> Path:
    return CACHE_DIR / f"{doctor_key}.mp4"


def _ffmpeg_bin() -> str:
    import os
    return os.environ.get("FFMPEG_BIN") or "ffmpeg"


def _ffprobe_bin() -> str:
    import os
    custom = os.environ.get("FFMPEG_BIN")
    if custom:
        cand = Path(custom).with_name("ffprobe")
        if cand.is_file():
            return str(cand)
    return "ffprobe"


def _probe_duration(path: Path) -> float:
    r = subprocess.run(
        [_ffprobe_bin(), "-v", "error", "-show_entries", "format=duration",
         "-of", "default=nw=1:nk=1", str(path)],
        check=True, capture_output=True, text=True,
    )
    return float(r.stdout.strip())


def cached_video_path(doctor_key: str) -> Optional[Path]:
    """命中缓存返回路径；否则 None。"""
    p = _video_path(doctor_key)
    return p if p.is_file() and p.stat().st_size > 0 else None


def ensure_base_video(
    doctor_key: str,
    settings: Optional[Settings] = None,
    *,
    progress: Optional[kling_avatar.ProgressFn] = None,
) -> Path:
    """确保该医生的基础视频可用，返回本地路径。命中缓存即返回；否则调 image2video 落盘。

    image_url 取自医生形象库的公网地址：{PUBLIC_BASE_URL}/api/doctors/{key}/image
    """
    s = settings or get_settings()
    doctor = doctors_mod.get_doctor(doctor_key)
    if not doctor:
        raise kling_avatar.KlingError(f"未知医生形象：{doctor_key}")

    with _lock:
        existing = cached_video_path(doctor.key)
        if existing:
            log.info("Kling 基础视频命中缓存 %s -> %s", doctor.key, existing.name)
            return existing

        # 用 Kling 专用公网地址（若未配置则回退 public_base_url）
        base_url = (s.kling_public_base_url or s.public_base_url or "").rstrip("/")
        if not base_url:
            raise kling_avatar.KlingError(
                "KLING_PUBLIC_BASE_URL 或 PUBLIC_BASE_URL 未配置，"
                "Kling 接口需要公网可达的医生图 URL"
            )
        image_url = f"{base_url}/api/doctors/{doctor.key}/image"
        log.info("Kling 基础视频未命中，调 image2video doctor=%s image=%s",
                 doctor.key, image_url)

        cdn_url = kling_avatar.image_to_video(s, image_url, progress=progress)

        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        out_path = _video_path(doctor.key)
        kling_avatar.download(cdn_url, out_path)

        try:
            dur = _probe_duration(out_path)
        except Exception as e:  # noqa: BLE001
            log.warning("ffprobe 基础视频失败 %s：%s", out_path, e)
            dur = 0.0

        _meta_path(doctor.key).write_text(
            json.dumps({
                "doctor_key": doctor.key,
                "duration_sec": round(dur, 3),
                "created_at": int(time.time()),
                "prompt": s.kling_base_prompt,
                "model": s.kling_image_model,
                "size_bytes": out_path.stat().st_size,
            }, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        log.info("Kling 基础视频已落盘 %s duration=%.2fs", out_path.name, dur)
        return out_path


def make_loop_for_duration(
    base_video: Path,
    out_path: Path,
    target_sec: float,
) -> Path:
    """把基础视频用 ffmpeg `-stream_loop -1 -t target_sec` 循环到至少覆盖 target_sec。

    若 base_video 已 ≥ target_sec，则直接 ffmpeg copy 到 out_path（统一文件名约定）。
    """
    base_video = Path(base_video)
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    base_dur = _probe_duration(base_video)
    # 留 0.3s 余量：advanced-lip-sync 要求 audio_end ≤ video_duration
    need = max(target_sec + 0.3, base_dur)
    cmd = [
        _ffmpeg_bin(), "-y", "-loglevel", "error",
    ]
    if need > base_dur:
        cmd += ["-stream_loop", "-1", "-i", str(base_video),
                "-t", f"{need:.3f}", "-c", "copy", str(out_path)]
    else:
        # 复制即可
        shutil.copyfile(base_video, out_path)
        return out_path
    subprocess.run(cmd, check=True, capture_output=True)
    return out_path
