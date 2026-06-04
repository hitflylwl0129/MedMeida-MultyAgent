"""腾讯云 TTS（TextToVoice）封装。

路线 Y 用途：把分镜话术文本合成为 mp3，作为 ffmpeg 拼片的口播音轨。

约束与对策：
- TextToVoice 中文单次最多 150 字（含全角标点）。本模块按句切分（中文句末标点 +
  逗号兜底），逐段合成后再用 ffmpeg concat 成单个 mp3。
- 同步返回 base64，无需轮询/上传。
- 失败/限流由腾讯云 SDK 抛 TencentCloudSDKException，本模块捕获后包成 TtsError。
"""
from __future__ import annotations

import base64
import json
import logging
import re
import shutil
import subprocess
import tempfile
import uuid
from pathlib import Path
from typing import Optional

from tencentcloud.common import credential
from tencentcloud.common.exception.tencent_cloud_sdk_exception import TencentCloudSDKException
from tencentcloud.common.profile.client_profile import ClientProfile
from tencentcloud.common.profile.http_profile import HttpProfile
from tencentcloud.tts.v20190823 import models, tts_client

from .config import Settings, get_settings

log = logging.getLogger("video-agent.tts")

TTS_ENDPOINT = "tts.tencentcloudapi.com"
# 中文 TTS 单段安全长度（接口上限 150，留余量避全角标点把界限挤掉）
_CN_SAFE_LEN = 120
# 句末标点优先切，逗号顿号兜底。注意：仅用于断句，不影响 Prompt 内容。
_SENT_END = "。！？!?；;\n"
_SOFT_END = "，、,"


class TtsError(RuntimeError):
    pass


# --------------------------------------------------------------------------- #
# 客户端
# --------------------------------------------------------------------------- #
def _build_client(s: Settings) -> tts_client.TtsClient:
    cred = credential.Credential(s.tencentcloud_secret_id, s.tencentcloud_secret_key)
    http = HttpProfile()
    http.endpoint = TTS_ENDPOINT
    cp = ClientProfile()
    cp.httpProfile = http
    return tts_client.TtsClient(cred, s.tts_region or "ap-guangzhou", cp)


# --------------------------------------------------------------------------- #
# 文本分段（中文友好）
# --------------------------------------------------------------------------- #
def split_text(text: str, max_len: int = _CN_SAFE_LEN) -> list[str]:
    """按中文标点把长文本切成 ≤max_len 的若干段。

    策略：先按句末标点切，超长段再按软标点切；仍超长则强制按 max_len 切片。
    返回的每段都不含首尾空白，空段被丢弃。
    """
    text = (text or "").strip()
    if not text:
        return []
    # 1) 句末标点切：保留标点在段尾，便于自然停顿
    pattern_hard = f"([{re.escape(_SENT_END)}])"
    raw = re.split(pattern_hard, text)
    sentences: list[str] = []
    buf = ""
    for chunk in raw:
        if chunk is None:
            continue
        if chunk in _SENT_END:
            buf += chunk
            if buf.strip():
                sentences.append(buf.strip())
            buf = ""
        else:
            buf += chunk
    if buf.strip():
        sentences.append(buf.strip())

    # 2) 仍超长的段按软标点二次切
    out: list[str] = []
    for seg in sentences:
        if len(seg) <= max_len:
            out.append(seg)
            continue
        soft_parts = re.split(f"([{re.escape(_SOFT_END)}])", seg)
        cur = ""
        for ch in soft_parts:
            if ch is None:
                continue
            if len(cur) + len(ch) > max_len and cur:
                out.append(cur.strip())
                cur = ""
            cur += ch
        if cur.strip():
            out.append(cur.strip())

    # 3) 兜底：仍超长直接硬切
    final: list[str] = []
    for seg in out:
        while len(seg) > max_len:
            final.append(seg[:max_len])
            seg = seg[max_len:]
        if seg:
            final.append(seg)
    return final


# --------------------------------------------------------------------------- #
# 单段合成
# --------------------------------------------------------------------------- #
def _synthesize_segment(
    client: tts_client.TtsClient,
    text: str,
    *,
    voice_type: int,
    speed: float,
    volume: float,
    sample_rate: int,
    codec: str,
    emotion_category: str,
) -> bytes:
    """调一次 TextToVoice，返回原始音频字节。"""
    req = models.TextToVoiceRequest()
    payload: dict = {
        "Text": text,
        "SessionId": uuid.uuid4().hex,
        "VoiceType": voice_type,
        "Speed": speed,
        "Volume": volume,
        "SampleRate": sample_rate,
        "Codec": codec,
        "ModelType": 1,
        "PrimaryLanguage": 1,
    }
    if emotion_category:
        payload["EmotionCategory"] = emotion_category
    req.from_json_string(json.dumps(payload, ensure_ascii=False))

    try:
        resp = client.TextToVoice(req)
    except TencentCloudSDKException as e:  # noqa: BLE001
        raise TtsError(f"TextToVoice 失败：{e.get_code()} {e.get_message()}") from e

    audio_b64 = getattr(resp, "Audio", "") or ""
    if not audio_b64:
        raise TtsError(f"TextToVoice 无 Audio 返回：{resp.to_json_string()}")
    return base64.b64decode(audio_b64)


# --------------------------------------------------------------------------- #
# 多段拼接（mp3）
# --------------------------------------------------------------------------- #
def _concat_mp3(parts: list[Path], out_path: Path) -> None:
    """用 ffmpeg concat 协议把多段同采样率/同编码 mp3 拼成单文件。"""
    if not parts:
        raise TtsError("无音频片段可拼接")
    if len(parts) == 1:
        shutil.copyfile(parts[0], out_path)
        return

    out_path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        "w", suffix=".txt", delete=False, encoding="utf-8"
    ) as fp:
        for p in parts:
            # ffmpeg concat demuxer 要求每行 file '<path>'，路径需 escape
            fp.write(f"file '{p.as_posix()}'\n")
        list_file = Path(fp.name)
    try:
        # 与 composer 用同一份 ffmpeg（默认 PATH，可被 .env FFMPEG_BIN 覆盖）
        import os as _os
        ffmpeg_bin = _os.environ.get("FFMPEG_BIN") or "ffmpeg"
        cmd = [
            ffmpeg_bin, "-y", "-loglevel", "error",
            "-f", "concat", "-safe", "0",
            "-i", str(list_file),
            "-c", "copy",
            str(out_path),
        ]
        subprocess.run(cmd, check=True, capture_output=True)
    except subprocess.CalledProcessError as e:
        raise TtsError(f"ffmpeg concat 失败：{e.stderr.decode('utf-8', 'ignore')}") from e
    finally:
        list_file.unlink(missing_ok=True)


# --------------------------------------------------------------------------- #
# 对外入口
# --------------------------------------------------------------------------- #
def _probe_audio_duration(path: Path) -> float:
    """用 ffprobe 探测单段音频时长（秒），失败返回 0.0（不抛，TTS 流程仍可继续）。"""
    try:
        import os as _os
        ffmpeg_bin = _os.environ.get("FFMPEG_BIN") or "ffmpeg"
        # ffprobe 一般与 ffmpeg 同目录；若是绝对路径取同目录的 ffprobe
        ffprobe_bin = "ffprobe"
        if "/" in ffmpeg_bin or "\\" in ffmpeg_bin:
            cand = Path(ffmpeg_bin).with_name("ffprobe")
            if cand.is_file():
                ffprobe_bin = str(cand)
        r = subprocess.run(
            [ffprobe_bin, "-v", "error", "-show_entries", "format=duration",
             "-of", "default=nw=1:nk=1", str(path)],
            check=True, capture_output=True, text=True,
        )
        return float(r.stdout.strip() or 0.0)
    except (subprocess.CalledProcessError, ValueError, FileNotFoundError):
        return 0.0


def synthesize_to_mp3(
    text: str,
    out_path: Path,
    *,
    settings: Optional[Settings] = None,
    voice_type: Optional[int] = None,
    speed: Optional[float] = None,
    volume: Optional[float] = None,
    emotion: Optional[str] = None,
) -> Path:
    """把整段话术合成 mp3 落盘到 out_path。

    超过 150 字会自动按句切分，逐段合成后 ffmpeg 拼接。
    返回最终文件路径（== out_path）。

    边带产物：同目录写一份 `<out_stem>.segments.json`，结构：
        {"segments":[{"idx":0,"text":"...","duration_sec":2.34}, ...],
         "total_sec":15.74}
    供下游 composer 按真实段时长生成字幕轨（字幕文本 = 原口播分句）。
    """
    s = settings or get_settings()
    if not (s.tencentcloud_secret_id and s.tencentcloud_secret_key):
        raise TtsError("未配置 TENCENTCLOUD_SECRET_ID/KEY")

    segments = split_text(text)
    if not segments:
        raise TtsError("空文本不可合成")

    client = _build_client(s)
    vt = voice_type if voice_type is not None else s.tts_voice_type
    sp = speed if speed is not None else s.tts_speed
    vol = volume if volume is not None else s.tts_volume
    em = emotion if emotion is not None else s.tts_emotion
    sr = s.tts_sample_rate
    codec = "mp3"

    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    tmp_dir = Path(tempfile.mkdtemp(prefix="tts_"))
    parts: list[Path] = []
    segments_meta: list[dict] = []
    try:
        for idx, seg in enumerate(segments):
            log.info("TTS 合成 [%d/%d] %d字", idx + 1, len(segments), len(seg))
            audio = _synthesize_segment(
                client, seg,
                voice_type=vt, speed=sp, volume=vol,
                sample_rate=sr, codec=codec, emotion_category=em,
            )
            p = tmp_dir / f"seg_{idx:03d}.mp3"
            p.write_bytes(audio)
            parts.append(p)
            # 立刻探测本段时长，写入 segments_meta（用于字幕轨对齐口播）
            dur = _probe_audio_duration(p)
            segments_meta.append({"idx": idx, "text": seg, "duration_sec": round(dur, 3)})

        _concat_mp3(parts, out_path)
        total_sec = round(sum(m["duration_sec"] for m in segments_meta), 3)
        # 落盘边带元数据（失败不影响主链路）
        meta_path = out_path.with_suffix(out_path.suffix + ".segments.json")
        # 上面 .suffix=".mp3" 加 .segments.json → "voice.mp3.segments.json"，
        # 更直观一点用 stem 写：voice.segments.json
        meta_path = out_path.with_name(out_path.stem + ".segments.json")
        try:
            meta_path.write_text(
                json.dumps(
                    {"segments": segments_meta, "total_sec": total_sec},
                    ensure_ascii=False, indent=2,
                ),
                encoding="utf-8",
            )
            log.info(
                "TTS 合成完成 -> %s (%d段, total=%.2fs, meta=%s)",
                out_path, len(parts), total_sec, meta_path.name,
            )
        except OSError as e:  # noqa: BLE001
            log.warning("写 segments.json 失败：%s", e)
            log.info("TTS 合成完成 -> %s (%d段)", out_path, len(parts))
        return out_path
    finally:
        # 清理临时片段
        for p in parts:
            p.unlink(missing_ok=True)
        try:
            tmp_dir.rmdir()
        except OSError:
            pass
