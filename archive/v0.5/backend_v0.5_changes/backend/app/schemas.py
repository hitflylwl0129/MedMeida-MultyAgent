"""Agent 输入/输出契约（Pydantic）。

对齐前端 localStorage `sv_selected_script`（上游话术 Agent 产出）
与 `sv_selected_video`（本 Agent 下传分发 Agent）。
"""
from __future__ import annotations

import time
import uuid
from enum import Enum
from typing import Any, Optional

from pydantic import BaseModel, Field


# ---------- 上游输入：合规话术终稿 ----------
class ScriptInput(BaseModel):
    """镜像 sv_selected_script。字段尽量宽松，缺失给默认值。"""

    seg: str = ""
    text: str = ""
    duration: str = "≈22s"
    doctor: str = ""
    doctorEmoji: str = "👩\u200d⚕️"
    audienceTier: str = ""
    mainAge: str = ""
    platforms: str = "抖音 + 视频号"
    structure: str = "痛点→科普→带入→引导"
    meta: list[str] = Field(default_factory=list)
    rounds: int = 1


# ---------- 阶段②产出：分镜 ----------
class Shot(BaseModel):
    sc: str                      # 镜号
    sec: str                     # 时间区间，如 "0-3s"
    duration_sec: float = 0.0    # 该镜时长（秒）
    shot: str                    # 画面描述
    line: str                    # 口播台词
    cap: str                     # 字幕
    prompt: str = ""             # 转给生视频模型的画面 Prompt


class Storyboard(BaseModel):
    shots: list[Shot] = Field(default_factory=list)
    total_duration_sec: float = 0.0
    narration: str = ""          # 全片口播文本（字幕=口播=话术终稿，口径一致）


# ---------- 阶段③产出：成片 ----------
class VideoOutput(BaseModel):
    file_id: str = ""
    url: str = ""
    cover_url: str = ""
    duration_sec: float = 0.0
    width: int = 0
    height: int = 0


# ---------- 阶段④：合规结论 ----------
class ComplianceResult(BaseModel):
    passed: bool = False
    input_check: str = ""        # API InputComplianceCheck 结论
    output_check: str = ""       # API OutputComplianceCheck 结论
    detail: str = ""


# ---------- 任务状态机 ----------
class JobStatus(str, Enum):
    PENDING = "pending"          # 已创建，待执行
    STORYBOARD = "storyboard"    # 分镜拆解中
    SUBMITTING = "submitting"    # 提交生视频任务
    GENERATING = "generating"    # 生视频进行中（轮询）
    COMPLIANCE = "compliance"    # 合规复审
    DONE = "done"                # 完成，可移交分发
    FAILED = "failed"            # 失败


class VideoJob(BaseModel):
    id: str = Field(default_factory=lambda: uuid.uuid4().hex[:12])
    status: JobStatus = JobStatus.PENDING
    progress: int = 0            # 0-100
    message: str = ""

    script: ScriptInput
    storyboard: Optional[Storyboard] = None

    task_id: str = ""            # 腾讯云 CreateAigcVideoTask 返回的 TaskId
    session_id: str = ""         # 去重识别码
    output: Optional[VideoOutput] = None
    compliance: Optional[ComplianceResult] = None

    error: str = ""
    created_at: float = Field(default_factory=time.time)
    updated_at: float = Field(default_factory=time.time)

    def touch(self) -> None:
        self.updated_at = time.time()


# ---------- API 请求/响应 ----------
class CreateJobRequest(BaseModel):
    script: ScriptInput
    # 出镜医生形象库 key（senior/middle/young _ male/female）；不传用配置默认
    doctor_key: Optional[str] = None
    # 可覆盖默认医生首帧；不传则用配置里的 DOCTOR_IMAGE_FILEID
    doctor_image_fileid: Optional[str] = None
    doctor_image_url: Optional[str] = None


# ---------- 话术 Agent ----------
class GenerateScriptRequest(BaseModel):
    """话术 Agent 入参 —— 与 prototype script.html 的 localStorage 上游产物字段一致。

    product / doctor / audience 都用 dict 兼容，避免字段调整时反复改 schema；
    LLM 端真正消费的字段在 agents/script_agent.build_messages 里挑取。
    """
    product: dict = Field(default_factory=dict)
    doctor: dict = Field(default_factory=dict)
    audience: dict = Field(default_factory=dict)
    audience_key: str = ""           # 已弃用：原"话术受众侧重"，目标人群改取自上游 audience
    structure: str = "痛点→科普→产品自然带入→行动引导"
    target_duration_sec: int = 21


# ---------- 短视频分发 Agent · B站投稿 ----------
class BiliPublishRequest(BaseModel):
    """B站投稿入参。video 来源优先级：video_path > job_id 对应成片 > 最近一条本地成片。

    凭证（SESSDATA/bili_jct）只在后端 .env，前端不传、不持有。
    """
    job_id: str = ""                 # 指定成片任务，取 .cache/jobs/{job_id}/out.mp4
    video_path: str = ""             # 直接指定本地文件（最高优先级，调试用）
    title: str = ""
    desc: str = ""
    tag: str = ""                    # 逗号分隔；空则用 .env 默认
    tid: int = 0                     # 0=用 .env 默认分区
    copyright: int = 0               # 0=用 .env 默认
    cover: str = ""
    only_self: int = -1              # -1=用 .env 默认（默认仅自己可见）


class ProgressEvent(BaseModel):
    """SSE 推给前端的进度事件。"""

    job_id: str
    status: JobStatus
    progress: int
    message: str
    stage: str = ""              # 对应前端 st1..st5
    data: dict[str, Any] = Field(default_factory=dict)
