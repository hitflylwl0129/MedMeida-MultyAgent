"""医生形象库（avatar_i2v 首帧素材）。

6 个人设（年长/中年/青年 × 男/女），素材放在 backend/assets/doctors/。
首次使用时把本地图直传 VOD 换 FileId，并把 FileId 缓存到本地 json，
避免每次生成都重复上传（同一张图反复传会产生冗余媒资）。
"""
from __future__ import annotations

import json
import logging
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

log = logging.getLogger("video-agent.doctors")

# backend/ 根目录（app 的上一级）
BACKEND_ROOT = Path(__file__).resolve().parents[1]
DOCTORS_DIR = BACKEND_ROOT / "assets" / "doctors"
CACHE_PATH = BACKEND_ROOT / ".cache" / "doctor_fileids.json"

_lock = threading.Lock()


@dataclass(frozen=True)
class Doctor:
    key: str            # 稳定英文键（用于 URL/接口）
    name: str           # 中文显示名（与素材文件名一致）
    gender: str         # male / female
    age: str            # young / middle / senior
    emoji: str
    filename: str

    @property
    def path(self) -> Path:
        return DOCTORS_DIR / self.filename

    @property
    def exists(self) -> bool:
        return self.path.is_file()


# 注册表：key ↔ 中文名 双向可查
CATALOG: list[Doctor] = [
    Doctor("senior_male",   "年长男医生", "male",   "senior", "👨\u200d⚕️", "年长男医生.png"),
    Doctor("senior_female", "年长女医生", "female", "senior", "👩\u200d⚕️", "年长女医生.png"),
    Doctor("middle_male",   "中年男医生", "male",   "middle", "👨\u200d⚕️", "中年男医生.png"),
    Doctor("middle_female", "中年女医生", "female", "middle", "👩\u200d⚕️", "中年女医生.png"),
    Doctor("young_male",    "青年男医生", "male",   "young",  "👨\u200d⚕️", "青年男医生.png"),
    Doctor("young_female",  "青年女医生", "female", "young",  "👩\u200d⚕️", "青年女医生.png"),
]

_BY_KEY = {d.key: d for d in CATALOG}
_BY_NAME = {d.name: d for d in CATALOG}


def get_doctor(identifier: str) -> Optional[Doctor]:
    """按 key 或中文名查找医生形象。"""
    if not identifier:
        return None
    ident = identifier.strip()
    return _BY_KEY.get(ident) or _BY_NAME.get(ident)


def list_doctors() -> list[Doctor]:
    return list(CATALOG)


# --------------------------- FileId 缓存 --------------------------- #
def _load_cache() -> dict[str, str]:
    if not CACHE_PATH.is_file():
        return {}
    try:
        return json.loads(CACHE_PATH.read_text("utf-8")) or {}
    except Exception:  # noqa: BLE001
        return {}


def _save_cache(cache: dict[str, str]) -> None:
    CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    CACHE_PATH.write_text(json.dumps(cache, ensure_ascii=False, indent=2), "utf-8")


def get_cached_file_id(key: str) -> str:
    return _load_cache().get(key, "")


def set_cached_file_id(key: str, file_id: str) -> None:
    with _lock:
        cache = _load_cache()
        cache[key] = file_id
        _save_cache(cache)


def resolve_doctor_file_id(identifier: str, settings=None) -> str:
    """把医生人设(key 或中文名) 解析为 VOD FileId：

    命中缓存直接返回；否则把本地形象图直传 VOD，缓存后返回。
    """
    doctor = get_doctor(identifier)
    if not doctor:
        raise ValueError(f"未知医生形象：{identifier}")
    if not doctor.exists:
        raise FileNotFoundError(f"医生形象图缺失：{doctor.path}")

    cached = get_cached_file_id(doctor.key)
    if cached:
        return cached

    # 串行化上传，避免并发首跑重复上传同一张
    with _lock:
        cache = _load_cache()
        if cache.get(doctor.key):
            return cache[doctor.key]
        from .vod_upload import upload_local_image  # 延迟导入避免循环

        file_id = upload_local_image(str(doctor.path), settings)
        cache[doctor.key] = file_id
        _save_cache(cache)
        log.info("医生形象[%s]已上传并缓存 FileId=%s", doctor.key, file_id)
        return file_id


# --------------------------- 医生 → TTS 音色映射 --------------------------- #
# 腾讯云"超自然大模型音色"（详见 https://cloud.tencent.com/document/product/1073/92668）
# 选用大模型段（5xx/6xx）是因为账号当前持有"超自然大模型音色"资源包；精品段(101xxx)
# 资源包未购，会触发 PkgExhausted。映射尽量贴合医生年龄/性别/科普口播场景。
_VOICE_BY_DOCTOR: dict[str, int] = {
    "senior_male":   603003,  # 随和老李·中年/年长男声，沉稳
    "senior_female": 602005,  # 专业梓欣·女声，专业感强
    "middle_male":   502005,  # 智小解·男声解说，权威感
    "middle_female": 602003,  # 爱小悠·女声成熟亲和
    "young_male":    603005,  # 知心大林·男声温和
    "young_female":  603004,  # 温柔小柠·年轻女声温柔
}


def tts_voice_for_doctor(identifier: str, fallback: int = 602003) -> int:
    """按医生 key/中文名挑选 TTS 音色 ID。未命中时回退到 fallback。"""
    d = get_doctor(identifier)
    if not d:
        return fallback
    return _VOICE_BY_DOCTOR.get(d.key, fallback)


