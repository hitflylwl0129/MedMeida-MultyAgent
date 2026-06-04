"""参考动作视频管理（motion_control 用）。

设计：
- 素材放在 backend/assets/motion_ref/ 下，用户可放多个；默认 ref.mp4。
- 首次使用时把文件直传 VOD 拿 FileId，缓存 (path, mtime, size) → FileId 到 .cache/motion_ref_fileids.json。
- 文件变了（mtime/size 不一致）自动重传；没变命中缓存秒回。
"""
from __future__ import annotations

import json
import logging
import threading
from pathlib import Path
from typing import Optional

from .config import Settings, get_settings
from .vod_upload import VodError, upload_local_file

log = logging.getLogger("video-agent.motion_ref")

BACKEND_ROOT = Path(__file__).resolve().parents[1]
MOTION_REF_DIR = BACKEND_ROOT / "assets" / "motion_ref"
CACHE_PATH = BACKEND_ROOT / ".cache" / "motion_ref_fileids.json"

_lock = threading.Lock()


def ref_filename_for_doctor(doctor_name: str, default_filename: str = "ref.mp4") -> str:
    """按医生中文名匹配同名参考动作视频（<中文名>.mp4），强化每位医生的动作贴合度。

    例如选中「中年女医生」→ 优先用 assets/motion_ref/中年女医生.mp4；
    若该医生没有专属参考视频，则回退到默认（ref.mp4 或 .env 的 MOTION_REF_FILENAME）。
    """
    if doctor_name:
        cand = MOTION_REF_DIR / f"{doctor_name}.mp4"
        if cand.is_file():
            return f"{doctor_name}.mp4"
    return default_filename


def has_per_doctor_ref(doctor_name: str) -> bool:
    """该医生是否配有专属参考动作视频。"""
    return bool(doctor_name) and (MOTION_REF_DIR / f"{doctor_name}.mp4").is_file()


def _load_cache() -> dict:
    if not CACHE_PATH.is_file():
        return {}
    try:
        return json.loads(CACHE_PATH.read_text("utf-8")) or {}
    except Exception:  # noqa: BLE001
        return {}


def _save_cache(cache: dict) -> None:
    CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    CACHE_PATH.write_text(json.dumps(cache, ensure_ascii=False, indent=2), "utf-8")


def _signature(path: Path) -> str:
    st = path.stat()
    return f"{int(st.st_mtime)}:{st.st_size}"


def resolve_motion_ref_file_id(filename: str, settings: Optional[Settings] = None) -> str:
    """把参考动作视频解析为 VOD FileId。文件不变命中缓存秒回。

    filename：相对 motion_ref/ 目录的文件名（默认 ref.mp4），允许含子目录。
    """
    s = settings or get_settings()
    if not filename:
        raise VodError("未指定参考动作视频文件名")
    path = (MOTION_REF_DIR / filename).resolve()
    # 防止越狱
    if MOTION_REF_DIR.resolve() not in path.parents and path != MOTION_REF_DIR.resolve():
        raise VodError(f"参考视频路径越界：{filename}")
    if not path.is_file():
        raise VodError(
            f"参考动作视频缺失：{path}（请把素材放到 backend/assets/motion_ref/ 下）"
        )

    sig = _signature(path)
    cache_key = str(path.relative_to(BACKEND_ROOT))

    with _lock:
        cache = _load_cache()
        entry = cache.get(cache_key) or {}
        if entry.get("sig") == sig and entry.get("file_id"):
            return entry["file_id"]

        log.info("上传参考动作视频 %s …", path.name)
        file_id = upload_local_file(str(path), s)
        cache[cache_key] = {"sig": sig, "file_id": file_id}
        _save_cache(cache)
        log.info("参考动作视频[%s]已上传并缓存 FileId=%s", path.name, file_id)
        return file_id
