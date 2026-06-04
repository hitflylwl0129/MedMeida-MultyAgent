"""异步 Worker + SSE 事件总线。

- 每个 job 在后台线程执行 LangGraph 整图（生视频是长任务，避免阻塞事件循环）。
- 节点通过 emit 回调把进度事件发布到事件总线；FastAPI 的 SSE 端点订阅消费。
- emit 在 Worker 线程触发，用 loop.call_soon_threadsafe 安全投递到 asyncio.Queue。
"""
from __future__ import annotations

import asyncio
import logging
from typing import Optional

from .config import get_settings
from .orchestrator.graph import run_pipeline
from .schemas import JobStatus, ProgressEvent, VideoJob
from . import store

log = logging.getLogger("video-agent.worker")


class EventBus:
    def __init__(self) -> None:
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._subs: dict[str, list[asyncio.Queue]] = {}
        self._history: dict[str, list[ProgressEvent]] = {}

    def bind_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop

    def subscribe(self, job_id: str) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue()
        self._subs.setdefault(job_id, []).append(q)
        # 回放历史事件，避免订阅晚于进度
        for ev in self._history.get(job_id, []):
            q.put_nowait(ev)
        return q

    def unsubscribe(self, job_id: str, q: asyncio.Queue) -> None:
        if job_id in self._subs and q in self._subs[job_id]:
            self._subs[job_id].remove(q)

    def publish(self, event: ProgressEvent) -> None:
        """线程安全发布（可能从 Worker 线程调用）。"""
        self._history.setdefault(event.job_id, []).append(event)

        def _deliver() -> None:
            for q in self._subs.get(event.job_id, []):
                q.put_nowait(event)

        if self._loop and self._loop.is_running():
            self._loop.call_soon_threadsafe(_deliver)
        else:
            _deliver()


bus = EventBus()


def _make_emit():
    def emit(job: VideoJob, stage: str) -> None:
        store.save(job)
        bus.publish(
            ProgressEvent(
                job_id=job.id,
                status=job.status,
                progress=job.progress,
                message=job.message,
                stage=stage,
                data={
                    "task_id": job.task_id,
                    "output": job.output.model_dump() if job.output else None,
                    "compliance": job.compliance.model_dump() if job.compliance else None,
                    "storyboard": job.storyboard.model_dump() if job.storyboard else None,
                    "error": job.error,
                },
            )
        )

    return emit


async def start_job(
    job: VideoJob,
    doctor_key: str = "",
    doctor_file_id: str = "",
    doctor_url: str = "",
) -> None:
    """提交后台执行。立即返回，进度走 SSE。"""
    store.save(job)
    settings = get_settings()
    emit = _make_emit()

    def _run() -> None:
        try:
            run_pipeline(job, settings, emit, doctor_key, doctor_file_id, doctor_url)
        except Exception as e:  # noqa: BLE001
            log.exception("pipeline 异常")
            job.status = JobStatus.FAILED
            job.error = str(e)
            job.message = f"内部错误：{e}"
            emit(job, "st3")

    # 长任务丢到线程池，避免阻塞事件循环
    asyncio.create_task(asyncio.to_thread(_run))
