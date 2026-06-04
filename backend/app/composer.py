"""ffmpeg 合成器：医生静帧 + 口播音轨 + 烧录字幕 → 9:16 mp4。

路线 Y 的最后一步：拿到口播 mp3 后，把医生形象图作为 9:16 静帧、配上口播音轨、
按 storyboard 的镜头时间把每条字幕烧到画面下方，输出 H.264/AAC 的 mp4。

外部依赖：系统 ffmpeg（≥4.x，本机已有 8.1）。
- 默认调 PATH 里的 `ffmpeg`/`ffprobe`；
- 需要"字幕烧录"时建议安装 `brew install ffmpeg-full`（带 libass），并在 .env 设
  FFMPEG_BIN=/opt/homebrew/opt/ffmpeg-full/bin/ffmpeg
  本模块会自动改用它，未配置则回退到默认。
"""
from __future__ import annotations

import logging
import os
import shutil
import subprocess
from pathlib import Path
from typing import Iterable

log = logging.getLogger("video-agent.composer")

# macOS 自带简体中文字体，ass 烧录字幕用
DEFAULT_FONT_PATH = "/System/Library/Fonts/Hiragino Sans GB.ttc"
DEFAULT_FONT_NAME = "Hiragino Sans GB"

VIDEO_W, VIDEO_H = 1080, 1920  # 9:16


def _ffmpeg_bin() -> str:
    """优先用 .env / 环境变量 FFMPEG_BIN（如指向 ffmpeg-full），否则回退到 PATH 里的 ffmpeg。"""
    return os.environ.get("FFMPEG_BIN") or "ffmpeg"


def _ffprobe_bin() -> str:
    custom = os.environ.get("FFMPEG_BIN")
    if custom:
        # 同目录里的 ffprobe（ffmpeg-full 同目录就有）
        cand = Path(custom).with_name("ffprobe")
        if cand.is_file():
            return str(cand)
    return "ffprobe"



class ComposerError(RuntimeError):
    pass


# --------------------------------------------------------------------------- #
# ffmpeg 能力探测（按需缓存）
# --------------------------------------------------------------------------- #
_FILTER_CACHE: dict[str, bool] = {}


def _ffmpeg_has_filter(name: str) -> bool:
    """检测当前 ffmpeg 是否编译了某滤镜（如 subtitles、drawtext）。结果按进程缓存。"""
    if name in _FILTER_CACHE:
        return _FILTER_CACHE[name]
    try:
        r = subprocess.run(
            [_ffmpeg_bin(), "-hide_banner", "-filters"],
            check=True, capture_output=True, text=True,
        )
        # 输出每行形如 ".. drawtext  V->V  ..."，按词出现即认为存在
        present = any(
            line.split()[1:2] == [name]
            for line in r.stdout.splitlines() if line.strip()
        )
    except (subprocess.CalledProcessError, FileNotFoundError):
        present = False
    _FILTER_CACHE[name] = present
    return present



# --------------------------------------------------------------------------- #
# 字幕段（caption + 时间区间）
# --------------------------------------------------------------------------- #
def _fmt_ass_time(sec: float) -> str:
    """ASS 时间格式 H:MM:SS.cc"""
    sec = max(0.0, sec)
    h = int(sec // 3600)
    m = int((sec % 3600) // 60)
    s = sec - h * 3600 - m * 60
    return f"{h}:{m:02d}:{s:05.2f}"


def _build_ass(captions: list[tuple[float, float, str]], total_sec: float) -> str:
    """把 (start, end, text) 列表编成 ASS 字幕文本。

    样式：底部居中、Hiragino Sans GB、白字 + 半透明黑底，避免压在医生脸上。
    """
    head = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {VIDEO_W}
PlayResY: {VIDEO_H}
WrapStyle: 2
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, OutlineColour, BackColour, Bold, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Cap,{DEFAULT_FONT_NAME},64,&H00FFFFFF,&H00000000,&H80000000,1,3,0,2,2,80,80,160,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    lines = []
    for start, end, text in captions:
        if not text:
            continue
        # 把 start/end 一起截断到 total_sec；丢弃 start>=end 的非法区间（libass 会忽略且
        # 报警告）。这里出现非法区间通常是 storyboard 累计时长 > 实际口播时长导致。
        s_start = max(0.0, min(float(start), total_sec))
        s_end = max(0.0, min(float(end), total_sec))
        if s_end <= s_start:
            continue
        # ASS 文本里需 escape 大括号；我们的 cap 里基本只有汉字+标点，简单清洗
        safe = text.replace("\n", " ").replace("{", "(").replace("}", ")")
        lines.append(
            f"Dialogue: 0,{_fmt_ass_time(s_start)},{_fmt_ass_time(s_end)},Cap,,0,0,0,,{safe}"
        )
    return head + "\n".join(lines) + "\n"


def _captions_from_storyboard(storyboard) -> list[tuple[float, float, str]]:
    """从 Storyboard.shots 抽出 (start, end, cap) 序列。

    storyboard.shots[].sec 形如 "0-2.9s"；直接按累加 duration_sec 算更稳。
    """
    out: list[tuple[float, float, str]] = []
    if not storyboard or not getattr(storyboard, "shots", None):
        return out
    cur = 0.0
    for shot in storyboard.shots:
        dur = float(getattr(shot, "duration_sec", 0) or 0)
        cap = (getattr(shot, "cap", "") or "").strip()
        if dur <= 0 or not cap:
            cur += dur
            continue
        out.append((cur, cur + dur, cap))
        cur += dur
    return out


# --------------------------------------------------------------------------- #
# 媒体探测
# --------------------------------------------------------------------------- #
def probe_duration(path: Path) -> float:
    """返回媒体时长（秒）。失败抛 ComposerError。"""
    try:
        r = subprocess.run(
            [
                _ffprobe_bin(), "-v", "error",
                "-show_entries", "format=duration",
                "-of", "default=nw=1:nk=1",
                str(path),
            ],
            check=True, capture_output=True, text=True,
        )
        return float(r.stdout.strip())
    except (subprocess.CalledProcessError, ValueError) as e:
        raise ComposerError(f"ffprobe 失败：{path} -> {e}") from e


# --------------------------------------------------------------------------- #
# 主入口
# --------------------------------------------------------------------------- #
def compose_static_video(
    image_path: Path,
    audio_path: Path,
    out_path: Path,
    *,
    captions: Iterable[tuple[float, float, str]] | None = None,
    storyboard=None,
    tail_silence_sec: float = 0.3,
) -> Path:
    """把"医生静帧 + 口播 + 字幕"合成 9:16 mp4。

    captions 可直接传 (start, end, text) 序列；若不传则从 storyboard 抽取。
    输出参数：1080x1920、H.264 yuv420p 30fps、AAC 128k、faststart。
    """
    image_path = Path(image_path)
    audio_path = Path(audio_path)
    out_path = Path(out_path)
    if not image_path.is_file():
        raise ComposerError(f"医生形象图不存在：{image_path}")
    if not audio_path.is_file():
        raise ComposerError(f"口播音频不存在：{audio_path}")
    if shutil.which(_ffmpeg_bin()) is None and not Path(_ffmpeg_bin()).is_file():
        raise ComposerError(f"未找到 ffmpeg 可执行文件：{_ffmpeg_bin()}")

    audio_sec = probe_duration(audio_path)
    total_sec = audio_sec + max(0.0, tail_silence_sec)

    # 字幕（可选）：仅当 ffmpeg 同时具备 subtitles 滤镜与 libass 时才烧录。
    # macOS brew 的默认 ffmpeg 多数未编译 libass/freetype，烧录会失败；这里检测后
    # 优雅降级——画面只放医生静帧+口播，字幕由前端镜头表呈现。
    cap_list = list(captions) if captions is not None else _captions_from_storyboard(storyboard)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    burn_subs = _ffmpeg_has_filter("subtitles") and cap_list
    ass_path: Path | None = None
    vf_chain = [
        f"scale={VIDEO_W}:{VIDEO_H}:force_original_aspect_ratio=decrease",
        f"pad={VIDEO_W}:{VIDEO_H}:(ow-iw)/2:(oh-ih)/2:color=black",
        "setsar=1",
        "format=yuv420p",
    ]
    if burn_subs:
        ass_path = out_path.parent / (out_path.stem + ".ass")
        ass_path.write_text(_build_ass(cap_list, total_sec), encoding="utf-8")
        ass_arg = (
            str(ass_path)
            .replace("\\", "\\\\")
            .replace(":", r"\:")
            .replace("'", r"\\\\'")
            .replace(",", r"\,")
            .replace("[", r"\[")
            .replace("]", r"\]")
        )
        vf_chain.append(f"subtitles=filename={ass_arg}")
    else:
        log.info("composer 跳过字幕烧录（ffmpeg 缺 subtitles/libass，或无字幕条目）")
    vf = ",".join(vf_chain)

    cmd = [
        _ffmpeg_bin(), "-y", "-loglevel", "error",
        # 静帧：循环输入图片
        "-loop", "1", "-i", str(image_path),
        # 口播音频
        "-i", str(audio_path),
        # 滤镜：缩放/居中/字幕烧录
        "-vf", vf,
        # 输出长度=音频时长+尾静音
        "-t", f"{total_sec:.3f}",
        # 视频编码
        "-r", "30",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "22",
        "-pix_fmt", "yuv420p",
        # 音频编码
        "-c:a", "aac", "-b:a", "128k", "-ar", "44100",
        # 流绑定：vid 来自 0（图片），aud 来自 1（音频）；并以最短结束（实际由 -t 控制）
        "-map", "0:v:0", "-map", "1:a:0",
        # 边下边播
        "-movflags", "+faststart",
        # 静帧 + -t 时长输出后会有些非关键帧问题，给个收尾
        "-shortest",
        str(out_path),
    ]
    log.info("ffmpeg compose: %s -> %s (%.2fs)", image_path.name, out_path.name, total_sec)
    try:
        subprocess.run(cmd, check=True, capture_output=True)
    except subprocess.CalledProcessError as e:
        raise ComposerError(
            f"ffmpeg 合成失败：{e.stderr.decode('utf-8', 'ignore')[-800:]}"
        ) from e
    finally:
        # 字幕中间产物保留可调试；如要清理可解除注释
        # ass_path.unlink(missing_ok=True)
        pass

    log.info("compose 完成 size=%dB -> %s", out_path.stat().st_size, out_path)
    return out_path


# --------------------------------------------------------------------------- #
# motion_control 后处理：把"无声医生动效视频 + 我们的口播 mp3"拼成最终成片
# --------------------------------------------------------------------------- #
def mux_video_audio(
    video_path: Path,
    audio_path: Path,
    out_path: Path,
    *,
    storyboard=None,
    captions: Iterable[tuple[float, float, str]] | None = None,
    tail_silence_sec: float = 0.3,
) -> Path:
    """合并无声/有声视频 + 口播音轨为最终成片。

    时长策略：以音频时长 + tail_silence_sec 为准；视频不够则用 stream_loop 循环（保持最自然的画面），
    多余则被 -t 截掉。同时 reset 音视频流为 H.264/AAC 1080×1920，并尝试烧录字幕（若 ffmpeg 支持）。
    """
    video_path = Path(video_path)
    audio_path = Path(audio_path)
    out_path = Path(out_path)
    if not video_path.is_file():
        raise ComposerError(f"视频文件不存在：{video_path}")
    if not audio_path.is_file():
        raise ComposerError(f"音频文件不存在：{audio_path}")

    audio_sec = probe_duration(audio_path)
    total_sec = audio_sec + max(0.0, tail_silence_sec)

    cap_list = list(captions) if captions is not None else _captions_from_storyboard(storyboard)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    burn_subs = _ffmpeg_has_filter("subtitles") and cap_list
    ass_path: Path | None = None
    vf_chain = [
        f"scale={VIDEO_W}:{VIDEO_H}:force_original_aspect_ratio=decrease",
        f"pad={VIDEO_W}:{VIDEO_H}:(ow-iw)/2:(oh-ih)/2:color=black",
        "setsar=1",
        "format=yuv420p",
    ]
    if burn_subs:
        ass_path = out_path.parent / (out_path.stem + ".ass")
        ass_path.write_text(_build_ass(cap_list, total_sec), encoding="utf-8")
        ass_arg = (
            str(ass_path)
            .replace("\\", "\\\\")
            .replace(":", r"\:")
            .replace("'", r"\\\\'")
            .replace(",", r"\,")
            .replace("[", r"\[")
            .replace("]", r"\]")
        )
        vf_chain.append(f"subtitles=filename={ass_arg}")
    vf = ",".join(vf_chain)

    cmd = [
        _ffmpeg_bin(), "-y", "-loglevel", "error",
        # 视频：循环到至少覆盖音频时长（-stream_loop -1 配合 -t 自动截）
        "-stream_loop", "-1", "-i", str(video_path),
        # 口播音频
        "-i", str(audio_path),
        # 视频重采样/字幕
        "-vf", vf,
        # 输出长度
        "-t", f"{total_sec:.3f}",
        "-r", "30",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "22",
        "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "128k", "-ar", "44100",
        "-map", "0:v:0", "-map", "1:a:0",
        "-movflags", "+faststart",
        "-shortest",
        str(out_path),
    ]
    log.info("ffmpeg mux: %s + %s -> %s (%.2fs)",
             video_path.name, audio_path.name, out_path.name, total_sec)
    try:
        subprocess.run(cmd, check=True, capture_output=True)
    except subprocess.CalledProcessError as e:
        raise ComposerError(
            f"ffmpeg mux 失败：{e.stderr.decode('utf-8', 'ignore')[-800:]}"
        ) from e

    log.info("mux 完成 size=%dB -> %s", out_path.stat().st_size, out_path)
    return out_path


def download_to(url: str, out_path: Path, timeout: float = 120.0) -> Path:
    """把远程 URL 流式下载到本地（motion_control 成片是临时 URL，要尽早落地）。"""
    import urllib.request
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    log.info("downloading %s -> %s", url[:80], out_path)
    with urllib.request.urlopen(url, timeout=timeout) as r, open(out_path, "wb") as f:
        shutil.copyfileobj(r, f)
    return out_path

