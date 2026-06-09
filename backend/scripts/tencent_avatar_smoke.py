"""腾讯云数智人「照片免训练」PoC 烟测 + 对比矩阵。

目标（A 路线，4-6 小时工作量）：
- 跑 4 组对比验证 4 个关键问题：
  1. 9:16 竖屏到底支不支持（720P/1080P 各一组）
  2. 文本驱动 vs 音频驱动哪个嘴形更精准
  3. 不同医生形象一致性如何
  4. 渲染速度与官方"1s 视频 ≈ 20s 渲染"的承诺差距

用法（在 162 上 .env 配好 TENCENT_AVATAR_APP_KEY 后）：
    cd /opt/video-agent/backend
    .venv/bin/python -m scripts.tencent_avatar_smoke

环境变量：
- TENCENT_AVATAR_APP_KEY 必填
- TENCENT_AVATAR_ACCESS_TOKEN 必填（HMAC-SHA256 的密钥，等同 SecretKey，绝不出现在请求中）
- TENCENT_AVATAR_PUBLIC_BASE_URL 可选，默认用 KLING_PUBLIC_BASE_URL（沿用现成的公网域名）
- TENCENT_AVATAR_TIMBRE_KEY 可选，默认空（用接口默认音色）

输出：
- 标准输出实时打印 4 组测试进度
- 把每组的 ProgressResult.raw 落盘到 .cache/tencent_avatar_smoke/<group>.json
- 视频 URL 直接打印，浏览器可打开（7 天有效）
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path
from typing import Optional

# 让 `python -m scripts.tencent_avatar_smoke` 在 backend 根目录跑得通
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(ROOT / ".env", override=False)

from app.tencent_avatar import (  # noqa: E402
    TencentAvatarError,
    submit_photo_to_video,
    wait_until_done,
)

APP_KEY = os.getenv("TENCENT_AVATAR_APP_KEY", "")
ACCESS_TOKEN = os.getenv("TENCENT_AVATAR_ACCESS_TOKEN", "")
PUBLIC_BASE = (
    os.getenv("TENCENT_AVATAR_PUBLIC_BASE_URL")
    or os.getenv("KLING_PUBLIC_BASE_URL")
    or os.getenv("PUBLIC_BASE_URL")
    or "http://162.14.76.209"
).rstrip("/")
TIMBRE_KEY = os.getenv("TENCENT_AVATAR_TIMBRE_KEY", "")

# 输出目录
OUT_DIR = ROOT / ".cache" / "tencent_avatar_smoke"
OUT_DIR.mkdir(parents=True, exist_ok=True)

# ---------------------------------------------------------------------------
# 测试样本
# ---------------------------------------------------------------------------
# 17 秒口播脚本（与 v1.1 Kling 实测同款，便于直接对比）
SCRIPT_17S = (
    "总是凌晨三四点醒，再也睡不着？别再以为是年纪大了。"
    "研究发现，长期早醒往往跟褪黑素分泌节律紊乱有关。"
    "规律作息、睡前一小时远离手机蓝光，可以帮你重建节律。"
    "如果连续两周改善不明显，建议到医院睡眠门诊评估。"
)

# 4 组测试矩阵
# 注：文本驱动必须传 timbre_key。male_1 / female_1 是接口预置音色（接口文档列出的范围）
MATRIX = [
    {
        "name": "A_middle_male_text_720p",
        "doctor": "middle_male",
        "driver": "text",
        "resolution": "720P",
        "timbre": "male_1",
        "memo": "文本驱动 + 接口 TTS male_1，看 9:16 是否生效 + 嘴形",
    },
    {
        "name": "B_middle_male_text_1080p",
        "doctor": "middle_male",
        "driver": "text",
        "resolution": "1080P",
        "timbre": "male_1",
        "memo": "同 A 但升 1080P，看 9:16 在高分辨率下表现",
    },
    {
        "name": "C_senior_female_text_720p",
        "doctor": "senior_female",
        "driver": "text",
        "resolution": "720P",
        "timbre": "female_1",
        "memo": "不同形象/性别，看一致性",
    },
    # D 组（音频驱动）需要 v1.1 的 voice.wav 公网 URL，未配置时跳过
    {
        "name": "D_middle_male_voice_720p",
        "doctor": "middle_male",
        "driver": "voice",
        "resolution": "720P",
        "timbre": "",
        "memo": "音频驱动（v1.1 已生成音频）→ 与 Kling 同输入直接对照",
    },
]


def _photo_url_of(doctor_key: str) -> str:
    # 复用 v1.1 已有的 /api/doctors/{key}/image 端点
    return f"{PUBLIC_BASE}/api/doctors/{doctor_key}/image"


def _voice_url_of_recent_job() -> Optional[str]:
    """v1.1 已经成功跑出来的 voice.wav 路径（找最近一个 done 的 job）。"""
    jobs_dir = ROOT / ".cache" / "jobs"
    if not jobs_dir.is_dir():
        return None
    cands = sorted(
        [d for d in jobs_dir.iterdir()
         if d.is_dir() and (d / "voice.wav").is_file()],
        key=lambda d: d.stat().st_mtime,
        reverse=True,
    )
    if not cands:
        return None
    jid = cands[0].name
    # v1.1 已有 /api/video/jobs/{id}/artifact/{name} 端点（白名单）
    return f"{PUBLIC_BASE}/api/video/jobs/{jid}/artifact/voice.wav"


def _run_one(case: dict) -> dict:
    """跑单组测试，返回汇总结果。"""
    name = case["name"]
    print(f"\n========== [{name}] {case['memo']} ==========")

    photo_url = _photo_url_of(case["doctor"])
    submit_kw = dict(
        app_key=APP_KEY,
        access_token=ACCESS_TOKEN,
        photo_url=photo_url,
        resolution=case["resolution"],
    )
    if case["driver"] == "text":
        submit_kw["text"] = SCRIPT_17S
        # 优先用 case 里指定的音色；否则回落到 env 的 TIMBRE_KEY
        submit_kw["timbre_key"] = case.get("timbre") or TIMBRE_KEY or "male_1"
    else:
        voice_url = _voice_url_of_recent_job()
        if not voice_url:
            print(f"  [SKIP] 找不到 .cache/jobs/*/voice.wav，跳过音频驱动组")
            return {"name": name, "status": "SKIP",
                    "reason": "no voice.wav available"}
        submit_kw["voice_url"] = voice_url
        print(f"  voice_url = {voice_url}")

    print(f"  photo_url = {photo_url}")
    print(f"  resolution = {case['resolution']}")

    t_submit = time.time()
    try:
        sub = submit_photo_to_video(**submit_kw)
    except TencentAvatarError as e:
        print(f"  [FAIL] submit: {e}")
        return {"name": name, "status": "SUBMIT_FAIL", "error": str(e)}
    submit_ms = (time.time() - t_submit) * 1000
    print(f"  submit OK in {submit_ms:.0f} ms, TaskId={sub.task_id}")

    t_poll = time.time()
    seen_making_at: list[float] = []

    def on_progress(p, elapsed):
        if p.status == "MAKING" and not seen_making_at:
            seen_making_at.append(elapsed)
            print(f"  [t={elapsed:.0f}s] MAKING 开始（排队 {elapsed:.0f}s）")
        elif p.status == "COMMIT" and int(elapsed) % 16 == 0:
            print(f"  [t={elapsed:.0f}s] still in queue, ArrayCount={p.array_count}")

    try:
        done = wait_until_done(
            app_key=APP_KEY, access_token=ACCESS_TOKEN, task_id=sub.task_id,
            poll_interval=8, max_wait=1800,
            on_progress=on_progress,
        )
    except TencentAvatarError as e:
        print(f"  [FAIL] wait: {e}")
        return {"name": name, "status": "WAIT_FAIL", "error": str(e),
                "task_id": sub.task_id}

    total = time.time() - t_poll
    queue = seen_making_at[0] if seen_making_at else 0
    make = total - queue
    print(f"  [DONE] total={total:.0f}s (queue={queue:.0f}s + make={make:.0f}s)")
    print(f"  Duration = {done.duration_ms} ms ({done.duration_ms/1000:.1f}s)")
    print(f"  MediaUrl = {done.media_url}")
    if done.subtitles_url:
        print(f"  SubtitlesUrl = {done.subtitles_url}")

    out = OUT_DIR / f"{name}.json"
    out.write_text(json.dumps({
        "submit_ms": submit_ms,
        "total_s": total,
        "queue_s": queue,
        "make_s": make,
        "duration_ms": done.duration_ms,
        "media_url": done.media_url,
        "subtitles_url": done.subtitles_url,
        "word_ts_count": len(done.word_timestamps),
        "raw_progress": done.raw,
    }, ensure_ascii=False, indent=2), "utf-8")
    print(f"  → 已落盘 {out.relative_to(ROOT)}")

    return {
        "name": name,
        "status": "SUCCESS",
        "submit_ms": round(submit_ms, 0),
        "queue_s": round(queue, 0),
        "make_s": round(make, 0),
        "total_s": round(total, 0),
        "duration_s": round(done.duration_ms / 1000, 1),
        "media_url": done.media_url,
        "task_id": sub.task_id,
    }


def main() -> int:
    print(f"AppKey: {'<set>' if APP_KEY else '<MISSING>'}")
    print(f"AccessToken: {'<set>' if ACCESS_TOKEN else '<MISSING>'}")
    print(f"Public base: {PUBLIC_BASE}")
    print(f"Output dir: {OUT_DIR}")
    if not APP_KEY:
        print("[ABORT] TENCENT_AVATAR_APP_KEY 未配置")
        return 2
    if not ACCESS_TOKEN:
        print("[ABORT] TENCENT_AVATAR_ACCESS_TOKEN 未配置")
        print("  -> 控制台 https://xiaowei.cloud.tencent.com/ivh#/asserts_management 获取")
        return 2

    summary = []
    for case in MATRIX:
        summary.append(_run_one(case))

    print("\n========== 汇总 ==========")
    print(f"{'name':<30} {'status':<12} {'submit':<8} {'queue':<6} {'make':<6} "
          f"{'total':<6} {'video':<6}")
    for r in summary:
        if r["status"] != "SUCCESS":
            print(f"{r['name']:<30} {r['status']:<12} -        -      -      -      -")
        else:
            print(f"{r['name']:<30} {r['status']:<12} "
                  f"{int(r['submit_ms']):<7}ms "
                  f"{int(r['queue_s']):<5}s "
                  f"{int(r['make_s']):<5}s "
                  f"{int(r['total_s']):<5}s "
                  f"{r['duration_s']:<5}s")

    (OUT_DIR / "_summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), "utf-8"
    )
    print(f"\n汇总已落盘：{OUT_DIR / '_summary.json'}")
    print("浏览器打开 MediaUrl 看效果（7 天有效），重点对比：")
    print(" - 视频比例（9:16 还是 16:9 还是 1:1）")
    print(" - 嘴形精度（vs v1.1 Kling 同一段话术）")
    print(" - 字幕（SubtitlesUrl 是 SRT，可下载看时间戳精度）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
