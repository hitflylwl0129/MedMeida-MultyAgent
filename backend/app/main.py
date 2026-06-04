"""短视频制作 Agent —— FastAPI 入口（形态 A：单进程内编排）。

路由：
  GET  /api/health                   健康检查 + 配置就绪状态
  POST /api/video/jobs               从话术终稿创建生视频任务（立即返回 job_id）
  GET  /api/video/jobs/{id}          查询任务快照
  GET  /api/video/jobs/{id}/events   SSE 进度流
  GET  /api/video/jobs/{id}/file     回放本地链路（路线 Y）成片 mp4
  GET  /api/video/jobs               最近任务列表
"""
from __future__ import annotations

import asyncio
import logging
from pathlib import Path

# 把 backend/.env 的非密钥型变量注入 os.environ，让 composer.py / tts.py 这类
# 直接读 env var 的工具能拿到（pydantic-settings 默认只填 Settings 对象，不入 environ）。
# 在 import config / 任何业务模块之前执行。
from dotenv import load_dotenv as _load_dotenv
_load_dotenv(Path(__file__).resolve().parent.parent / ".env", override=False)

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from sse_starlette.sse import EventSourceResponse

from .config import get_settings
from .schemas import BiliPublishRequest, CreateJobRequest, GenerateScriptRequest, JobStatus, VideoJob
from . import doctors, store
from .agents import bilibili_agent, script_agent
from .worker import bus, start_job


logging.basicConfig(
    level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)
log = logging.getLogger("video-agent")

# 本地链路成片目录（与 orchestrator/graph.py 保持一致）
_LOCAL_JOBS_DIR = Path(__file__).resolve().parent.parent / ".cache" / "jobs"


app = FastAPI(title="短视频制作 Agent", version="0.1.0")

# 前端原型用 file:// 或本地静态服务(localhost:8848) 打开，放开 CORS 便于联调
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.on_event("startup")
async def _bind_loop() -> None:
    bus.bind_loop(asyncio.get_running_loop())


@app.get("/api/health")
async def health() -> JSONResponse:
    s = get_settings()
    catalog = doctors.list_doctors()
    ready_doctors = [d.key for d in catalog if d.exists]
    return JSONResponse(
        {
            "ok": True,
            "credentials_ready": s.credentials_ready,
            "sub_app_id": s.vod_sub_app_id,
            "model": f"{s.aigc_model_name}/{s.aigc_model_version}/{s.aigc_scene_type}",
            # 形象库就绪即可生成（首帧来自本地素材库），兼容旧的 fileid/url 配置
            "doctor_image_ready": bool(
                ready_doctors or s.doctor_image_fileid or s.doctor_image_url
            ),
            "default_doctor": s.default_doctor,
            "doctors_ready": ready_doctors,
            "doctors_total": len(catalog),
        }
    )


@app.get("/api/doctors")
async def list_doctors() -> list[dict]:
    """医生形象库清单（供前端选择器渲染）。"""
    from . import motion_ref
    return [
        {
            "key": d.key,
            "name": d.name,
            "gender": d.gender,
            "age": d.age,
            "emoji": d.emoji,
            "available": d.exists,
            "cached": bool(doctors.get_cached_file_id(d.key)),
            "thumb": f"/api/doctors/{d.key}/image",
            # 是否配有该医生专属的参考动作视频（motion_control 强化用）
            "has_motion_ref": motion_ref.has_per_doctor_ref(d.name),
        }
        for d in doctors.list_doctors()
    ]


@app.get("/api/doctors/{key}/image")
async def doctor_image(key: str):
    """医生形象图缩略（首帧素材）。"""
    d = doctors.get_doctor(key)
    if not d or not d.exists:
        raise HTTPException(404, "医生形象图不存在")
    return FileResponse(str(d.path), media_type="image/png")


@app.post("/api/video/jobs")
async def create_job(req: CreateJobRequest) -> dict:
    s = get_settings()
    if not s.credentials_ready:
        raise HTTPException(500, "腾讯云密钥/SubAppId 未配置（backend/.env）")

    job = VideoJob(script=req.script)
    store.save(job)
    await start_job(
        job,
        doctor_key=req.doctor_key or "",
        doctor_file_id=req.doctor_image_fileid or "",
        doctor_url=req.doctor_image_url or "",
    )
    return {"job_id": job.id, "status": job.status}


@app.get("/api/video/jobs/{job_id}")
async def get_job(job_id: str) -> VideoJob:
    job = store.get(job_id)
    if not job:
        raise HTTPException(404, "任务不存在")
    return job


@app.get("/api/video/jobs/{job_id}/file")
async def get_job_file(job_id: str):
    """回放本地链路（路线 Y）的成片 mp4。

    路径固定 backend/.cache/jobs/{job_id}/out.mp4。任务存在但尚未生成完成时返回 404。
    """
    if not store.get(job_id):
        raise HTTPException(404, "任务不存在")
    path = _LOCAL_JOBS_DIR / job_id / "out.mp4"
    if not path.is_file():
        raise HTTPException(404, "成片尚未生成或已清理")
    return FileResponse(
        str(path), media_type="video/mp4",
        # 让浏览器把 mp4 当播放源，而不是下载
        headers={"Accept-Ranges": "bytes"},
    )


# 中间产物对外暴露：供第三方 API（可灵 Kling identify-face/lip-sync）按 URL 拉取
# job 目录里的基础视频/音频等。按后缀白名单 + 防目录穿越限制可访问范围。
_ARTIFACT_MEDIA_TYPES = {
    ".mp4": "video/mp4",
    ".mov": "video/quicktime",
    ".mp3": "audio/mpeg",
    ".wav": "audio/wav",
    ".m4a": "audio/mp4",
    ".ass": "text/x-ssa; charset=utf-8",
    ".png": "image/png",
    ".jpg": "image/jpeg",
}


@app.get("/api/video/jobs/{job_id}/artifact/{name}")
async def get_job_artifact(job_id: str, name: str):
    """通用产物端点：访问 job 目录下指定名称的中间产物（按后缀白名单）。"""
    # 防目录穿越：只允许纯文件名
    if "/" in name or "\\" in name or ".." in name:
        raise HTTPException(400, "非法文件名")
    suffix = Path(name).suffix.lower()
    media_type = _ARTIFACT_MEDIA_TYPES.get(suffix)
    if media_type is None:
        raise HTTPException(404, "不支持的产物类型")
    if not store.get(job_id):
        raise HTTPException(404, "任务不存在")
    path = (_LOCAL_JOBS_DIR / job_id / name).resolve()
    # 二次校验解析后的路径仍在该 job 目录内
    job_root = (_LOCAL_JOBS_DIR / job_id).resolve()
    if job_root not in path.parents:
        raise HTTPException(400, "非法路径")
    if not path.is_file():
        raise HTTPException(404, f"{name} 尚未生成或已清理")
    return FileResponse(
        str(path), media_type=media_type,
        headers={"Accept-Ranges": "bytes"},
    )


@app.get("/api/video/jobs/{job_id}/prompt")
async def get_job_prompt(job_id: str) -> dict:
    """返回该 job 实际提交给腾讯云 VOD 接口的 Prompt 全文（写入 prompt.txt 的快照）。

    用于前端「分镜面板」下方完整展示 Prompt 文本，便于复核口径。
    任务存在但 prompt.txt 尚未落盘（提交前）时返回空串。
    """
    if not store.get(job_id):
        raise HTTPException(404, "任务不存在")
    path = _LOCAL_JOBS_DIR / job_id / "prompt.txt"
    if not path.is_file():
        return {"prompt": "", "ready": False}
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as e:  # noqa: BLE001
        raise HTTPException(500, f"读取 prompt.txt 失败：{e}") from e
    return {"prompt": text, "ready": True}



@app.get("/api/video/jobs")
async def list_jobs() -> list[VideoJob]:
    return store.list_recent()


@app.get("/api/video/jobs/{job_id}/events")
async def job_events(job_id: str):
    job = store.get(job_id)
    if not job:
        raise HTTPException(404, "任务不存在")

    queue = bus.subscribe(job_id)

    async def gen():
        try:
            # 终态判断：连续收到 done/failed 后结束流
            while True:
                ev = await queue.get()
                yield {"event": "progress", "data": ev.model_dump_json()}
                if ev.status in (JobStatus.DONE, JobStatus.FAILED):
                    break
        finally:
            bus.unsubscribe(job_id, queue)

    return EventSourceResponse(gen())


# --------------------------------------------------------------------------- #
# 话术 Agent —— 真实 LLM 流式生成
# --------------------------------------------------------------------------- #
@app.post("/api/script/generate")
async def generate_script(req: GenerateScriptRequest):
    """SSE 流式生成话术：边出 token 边推前端，结束时推 done + 最终结果。

    事件类型：
      token  : {"piece":"…"}             逐 token 增量
      done   : {"text":"…","violations":[],"audience_name":"…","char_count":N}
      failed : {"error":"…"}
    """
    s = get_settings()
    if not s.llm_api_key:
        raise HTTPException(500, "LLM_API_KEY 未配置（backend/.env）")

    # 用线程跑同步生成器，asyncio.Queue 桥接到 SSE
    queue: asyncio.Queue = asyncio.Queue()
    SENTINEL = object()

    def _producer():
        buf: list[str] = []
        try:
            for piece in script_agent.stream_generate(
                product=req.product, doctor=req.doctor, audience=req.audience,
                structure=req.structure,
                target_duration_sec=req.target_duration_sec, settings=s,
            ):
                buf.append(piece)
                queue.put_nowait(("token", {"piece": piece}))
            raw = "".join(buf).strip()
            cleaned, hits = script_agent.sanitize(raw)
            aud_name = (
                req.audience.get("name") or req.audience.get("mainAge") or "目标受众"
            )
            queue.put_nowait((
                "done",
                {
                    "text": cleaned,
                    "raw_text": raw,
                    "violations": hits,
                    "audience_name": aud_name,
                    "char_count": len(cleaned),
                },
            ))
        except Exception as e:  # noqa: BLE001
            log.exception("script_agent 失败")
            queue.put_nowait(("failed", {"error": str(e)}))
        finally:
            queue.put_nowait(SENTINEL)

    # 启动后台任务
    asyncio.create_task(asyncio.to_thread(_producer))

    async def gen():
        import json as _json
        while True:
            item = await queue.get()
            if item is SENTINEL:
                break
            event, data = item
            yield {"event": event, "data": _json.dumps(data, ensure_ascii=False)}
            if event in ("done", "failed"):
                break

    return EventSourceResponse(gen())


# --------------------------------------------------------------------------- #
# 短视频分发 Agent —— B站真实投稿
# --------------------------------------------------------------------------- #
@app.get("/api/distribute/bilibili/status")
async def bilibili_status() -> dict:
    """B站投稿能力就绪状态（供前端判断走真实链路还是模拟态）。"""
    s = get_settings()
    latest = bilibili_agent.latest_local_video()
    return {
        "configured": bilibili_agent.credentials_ready(s),
        "has_video": bool(latest),
        "latest_video": latest or "",
        "default_tid": s.bili_default_tid,
        "default_tag": s.bili_default_tag,
        "only_self": s.bili_only_self,
    }


@app.post("/api/distribute/bilibili")
async def bilibili_publish(req: BiliPublishRequest):
    """SSE 流式执行 B站投稿：边走流程边推进度。

    事件类型：
      stage    : {"step":"preupload|init|complete...", "msg":"…"}
      progress : {"phase":"upload","percent":N,"chunk":i,"chunks":n}
      done     : {"aid":..,"bvid":..,"url":..,"title":..,"visibility":..}
      failed   : {"error":"…"}
    """
    s = get_settings()
    if not bilibili_agent.credentials_ready(s):
        raise HTTPException(500, "B站凭证未配置（backend/.env 设 BILI_SESSDATA / BILI_JCT）")

    # 解析成片来源：video_path > job_id 对应成片 > 最近一条本地成片
    video_path = req.video_path
    if not video_path and req.job_id:
        cand = _LOCAL_JOBS_DIR / req.job_id / "out.mp4"
        if cand.is_file():
            video_path = str(cand)
    if not video_path:
        video_path = bilibili_agent.latest_local_video() or ""
    if not video_path:
        raise HTTPException(404, "未找到可投稿的本地成片（请先在短视频制作 Agent 生成成片）")

    queue: asyncio.Queue = asyncio.Queue()
    SENTINEL = object()

    def _producer():
        try:
            for ev in bilibili_agent.publish_stream(
                video_path=video_path, title=req.title, desc=req.desc,
                tag=req.tag, tid=req.tid, copyright=req.copyright,
                cover=req.cover, only_self=req.only_self, settings=s,
            ):
                queue.put_nowait((ev["event"], ev["data"]))
        except Exception as e:  # noqa: BLE001
            log.exception("B站投稿失败")
            queue.put_nowait(("failed", {"error": str(e)}))
        finally:
            queue.put_nowait(SENTINEL)

    asyncio.create_task(asyncio.to_thread(_producer))

    async def gen():
        import json as _json
        while True:
            item = await queue.get()
            if item is SENTINEL:
                break
            event, data = item
            yield {"event": event, "data": _json.dumps(data, ensure_ascii=False)}
            if event in ("done", "failed"):
                break

    return EventSourceResponse(gen())



if __name__ == "__main__":
    import uvicorn

    s = get_settings()
    uvicorn.run("app.main:app", host=s.app_host, port=s.app_port, reload=False)
