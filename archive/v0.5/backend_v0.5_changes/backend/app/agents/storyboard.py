"""分镜拆解 Agent（阶段②）。

decision(a)：本阶段先用规则法把话术终稿按「痛点→科普→带入→引导」切 4 镜，
并为每镜生成 avatar_i2v 的画面 Prompt。后续可平滑替换为 LLM 真实拆分镜
（接口契约 Storyboard 不变，仅换实现）。
"""
from __future__ import annotations

import re

from ..schemas import ScriptInput, Shot, Storyboard

# 四段式结构与默认时长占比（总时长≈22s）
_SEGMENTS = [
    ("痛点", "医生正面口播·特写", 3.0),
    ("科普", "医生讲解 + 知识动画", 7.0),
    ("带入", "医生讲解 + 成分/产品图", 8.0),
    ("引导", "品牌出镜 + 行动引导", 4.0),
]


def _split_sentences(text: str) -> list[str]:
    parts = re.split(r"(?<=[。！？!?；;])", (text or "").strip())
    return [p.strip() for p in parts if p.strip()]


def _allocate(sentences: list[str], n: int) -> list[str]:
    """把句子均匀分配到 n 个镜头。"""
    if not sentences:
        return [""] * n
    buckets: list[list[str]] = [[] for _ in range(n)]
    for i, sent in enumerate(sentences):
        buckets[min(i * n // len(sentences), n - 1)].append(sent)
    return ["".join(b) for b in buckets]


def _total_seconds(duration: str) -> float:
    m = re.search(r"(\d+(?:\.\d+)?)", duration or "")
    return float(m.group(1)) if m else 22.0


def build_storyboard(script: ScriptInput) -> Storyboard:
    sentences = _split_sentences(script.text)
    lines = _allocate(sentences, len(_SEGMENTS))
    total = _total_seconds(script.duration)

    # 按占比缩放每镜时长到实际总时长
    base = sum(d for _, _, d in _SEGMENTS)
    scale = total / base if base else 1.0

    shots: list[Shot] = []
    cursor = 0.0
    emoji = script.doctorEmoji or "👩\u200d⚕️"
    for idx, (stage, shot_desc, dur) in enumerate(_SEGMENTS, start=1):
        dsec = round(dur * scale, 1)
        start, end = round(cursor, 1), round(cursor + dsec, 1)
        cursor += dsec
        line = lines[idx - 1] or stage
        cap = line[:14] if line else stage
        prompt = (
            f"竖版9:16医疗科普短视频，{emoji}{script.doctor or '出镜医生'}"
            f"以专业亲和的姿态出镜口播；镜头：{shot_desc}；"
            f"阶段：{stage}；台词：{line}。画面干净、字幕清晰、无夸大疗效暗示。"
        )
        shots.append(
            Shot(
                sc=str(idx),
                sec=f"{start:g}-{end:g}s",
                duration_sec=dsec,
                shot=f"{emoji} {shot_desc}",
                line=line,
                cap=cap,
                prompt=prompt,
            )
        )

    return Storyboard(
        shots=shots,
        total_duration_sec=round(cursor, 1),
        narration=script.text or "",
    )


def overall_prompt(script: ScriptInput, sb: Storyboard) -> str:
    """汇总给 CreateAigcVideoTask 的整体 Prompt（口径=话术终稿）。"""
    return (
        f"医疗健康科普竖版口播短视频（9:16，约{sb.total_duration_sec:g}秒）。"
        f"出镜：{script.doctor or '专业医生'}，专业亲和、科普向。"
        f"内容结构：{script.structure}。"
        f"完整口播文案（字幕与口播严格一致）：{sb.narration}"
    )
