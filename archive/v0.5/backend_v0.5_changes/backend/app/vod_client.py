"""腾讯云点播（VOD）客户端封装。

封装两件事：
  1) CreateAigcVideoTask —— 提交 AIGC 生视频任务，拿 TaskId
  2) DescribeTaskDetail  —— 轮询任务详情，容错解析成片输出

鉴权由官方 SDK 自动签名；SecretId/SecretKey 仅从配置(env) 读取，绝不硬编码。
依赖文档：
  - CreateAigcVideoTask: https://cloud.tencent.com/document/product/266/126239
  - DescribeTaskDetail : https://cloud.tencent.com/document/product/266/33431
"""
from __future__ import annotations

import json
import logging
from typing import Any, Optional

from tencentcloud.common import credential
from tencentcloud.common.exception.tencent_cloud_sdk_exception import (
    TencentCloudSDKException,
)
from tencentcloud.common.profile.client_profile import ClientProfile
from tencentcloud.common.profile.http_profile import HttpProfile
from tencentcloud.vod.v20180717 import models, vod_client

from .config import Settings, get_settings
from .schemas import Storyboard, VideoOutput

log = logging.getLogger("video-agent.vod")

VOD_ENDPOINT = "vod.tencentcloudapi.com"
VOD_VERSION = "2018-07-17"


class VodError(RuntimeError):
    """VOD 调用异常（含腾讯云错误码）。"""


def _build_client(s: Settings) -> vod_client.VodClient:
    if not s.credentials_ready:
        raise VodError(
            "腾讯云密钥/SubAppId 未配置，请在 backend/.env 填入 "
            "TENCENTCLOUD_SECRET_ID / SECRET_KEY / VOD_SUB_APP_ID"
        )
    cred = credential.Credential(s.tencentcloud_secret_id, s.tencentcloud_secret_key)
    http_profile = HttpProfile(endpoint=VOD_ENDPOINT, reqTimeout=30)
    client_profile = ClientProfile(httpProfile=http_profile)
    return vod_client.VodClient(cred, s.vod_region, client_profile)


# --------------------------------------------------------------------------- #
# 提交：CreateAigcVideoTask
# --------------------------------------------------------------------------- #
def create_aigc_video_task(
    *,
    prompt: str,
    doctor_file_id: str = "",
    doctor_url: str = "",
    session_id: str = "",
    session_context: str = "",
    settings: Optional[Settings] = None,
) -> str:
    """提交 Kling/avatar_i2v 数字人生视频任务，返回 TaskId。

    医生形象图作为首帧（FileInfos[].Usage=FirstFrame）。
    口播由 API 生成（OutputConfig.AudioGeneration=Enabled），
    口播/画面意图通过 Prompt 注入。
    """
    s = settings or get_settings()
    client = _build_client(s)

    req = models.CreateAigcVideoTaskRequest()

    payload: dict[str, Any] = {
        "SubAppId": s.vod_sub_app_id,
        "ModelName": s.aigc_model_name,
        "ModelVersion": s.aigc_model_version,
        "SceneType": s.aigc_scene_type,
        "Prompt": prompt,
        "OutputConfig": {
            "StorageMode": s.aigc_storage_mode,
            "AspectRatio": s.aigc_aspect_ratio,
            "AudioGeneration": s.aigc_audio_generation,
            "PersonGeneration": s.aigc_person_generation,
            "InputComplianceCheck": s.aigc_input_compliance,
            "OutputComplianceCheck": s.aigc_output_compliance,
        },
    }

    # 首帧：医生形象图（优先 FileId；否则用 URL，需对应字段支持）
    if doctor_file_id:
        payload["FileInfos"] = [{"FileId": doctor_file_id, "Usage": "FirstFrame"}]
    elif doctor_url:
        # 注意：FileInfos 是否支持 Url 取决于数据结构定义，留作兜底；
        # 推荐先把医生图上传成 FileId（见 ensure_doctor_file_id）。
        payload["FileInfos"] = [{"Url": doctor_url, "Usage": "FirstFrame"}]

    if session_id:
        payload["SessionId"] = session_id[:50]
    if session_context:
        payload["SessionContext"] = session_context[:1000]

    req.from_json_string(json.dumps(payload, ensure_ascii=False))

    try:
        resp = client.CreateAigcVideoTask(req)
    except TencentCloudSDKException as e:  # noqa: BLE001
        raise VodError(f"CreateAigcVideoTask 失败：{e.get_code()} {e.get_message()}") from e

    task_id = getattr(resp, "TaskId", "") or ""
    if not task_id:
        raise VodError(f"CreateAigcVideoTask 未返回 TaskId：{resp.to_json_string()}")
    log.info("AIGC 生视频任务已提交 TaskId=%s", task_id)
    return task_id


# --------------------------------------------------------------------------- #
# 提交：CreateAigcVideoTask（SceneType=motion_control，动作迁移）
# --------------------------------------------------------------------------- #
def create_motion_control_task(
    *,
    character_file_id: str,
    motion_ref_file_id: str = "",
    motion_ref_url: str = "",
    prompt: str = "",
    session_id: str = "",
    session_context: str = "",
    settings: Optional[Settings] = None,
) -> str:
    """提交 Kling/motion_control 动作迁移任务，返回 TaskId。

    输入：角色图（首帧，FileId）+ 参考动作视频（FileInfos[1]，Category=Video, Usage=Reference）。
    输出：把参考视频里的动作迁移到角色图上的"无声"视频；后续与 TTS mp3 在 ffmpeg 中合并出口播成片。

    参考视频可走 FileId（推荐：先上传 VOD）或 Url（公网可达）。Prompt 可选，
    用于轻微引导画面（例如"医生轻松微笑、自然讲话"），motion_control 主要由参考视频驱动。
    """
    if not character_file_id:
        raise VodError("缺少角色图 FileId")
    if not (motion_ref_file_id or motion_ref_url):
        raise VodError("缺少参考动作视频（FileId 或 Url）")

    s = settings or get_settings()
    client = _build_client(s)

    file_infos: list[dict[str, Any]] = [
        {"FileId": character_file_id, "Usage": "FirstFrame"},
    ]
    if motion_ref_file_id:
        file_infos.append({
            "Type": "File", "Category": "Video",
            "FileId": motion_ref_file_id, "Usage": "Reference",
        })
    else:
        file_infos.append({
            "Type": "Url", "Category": "Video",
            "Url": motion_ref_url, "Usage": "Reference",
        })

    payload: dict[str, Any] = {
        "SubAppId": s.vod_sub_app_id,
        "ModelName": s.motion_model_name,
        "ModelVersion": s.motion_model_version,
        "SceneType": "motion_control",
        "FileInfos": file_infos,
        "OutputConfig": {
            "StorageMode": "Temporary",
            "AspectRatio": s.motion_aspect_ratio,
        },
    }
    if prompt:
        payload["Prompt"] = prompt
    if session_id:
        payload["SessionId"] = session_id[:50]
    if session_context:
        payload["SessionContext"] = session_context[:1000]

    req = models.CreateAigcVideoTaskRequest()
    req.from_json_string(json.dumps(payload, ensure_ascii=False))
    try:
        resp = client.CreateAigcVideoTask(req)
    except TencentCloudSDKException as e:  # noqa: BLE001
        raise VodError(f"CreateAigcVideoTask(motion_control) 失败：{e.get_code()} {e.get_message()}") from e

    task_id = getattr(resp, "TaskId", "") or ""
    if not task_id:
        raise VodError(f"motion_control 未返回 TaskId：{resp.to_json_string()}")
    log.info("motion_control 任务已提交 TaskId=%s", task_id)
    return task_id


# --------------------------------------------------------------------------- #
# 查询：DescribeTaskDetail（容错解析）
# --------------------------------------------------------------------------- #
def describe_task(task_id: str, settings: Optional[Settings] = None) -> dict[str, Any]:
    """查询任务详情，返回原始 dict（已转 JSON）。"""
    s = settings or get_settings()
    client = _build_client(s)

    req = models.DescribeTaskDetailRequest()
    req.from_json_string(
        json.dumps({"TaskId": task_id, "SubAppId": s.vod_sub_app_id})
    )
    try:
        resp = client.DescribeTaskDetail(req)
    except TencentCloudSDKException as e:  # noqa: BLE001
        raise VodError(f"DescribeTaskDetail 失败：{e.get_code()} {e.get_message()}") from e
    return json.loads(resp.to_json_string())


def _first(d: dict[str, Any], *keys: str) -> Any:
    """从 dict 里按多个候选键名取第一个非空值（字段名容错）。"""
    for k in keys:
        if k in d and d[k] not in (None, "", 0):
            return d[k]
    return None


def parse_task_detail(detail: dict[str, Any]) -> tuple[str, Optional[VideoOutput], str]:
    """解析 DescribeTaskDetail 返回。

    返回 (status, output, message)：
      status ∈ WAITING/PROCESSING/FINISH/ABORTED/UNKNOWN

    腾讯云返回有“双层状态”：
      - 外层 detail.Status=FINISH 仅代表任务调度结束，不等于成功；
      - 内层 AigcVideoTask.ErrCode!=0 才是真实失败（曾经把失败误判为完成）。
    因此外层 FINISH 时还须复核内层 ErrCode；非 0 一律改写为 ABORTED 上抛，
    让上游统一走失败分支。
    成片输出实际位于 AigcVideoTask.Output.FileInfos[0]（FileId/Url 等），
    同时对历史可能出现的字段名（MediaUrl/VideoUrl 等）做容错。
    """
    status = str(detail.get("Status") or "UNKNOWN").upper()
    aigc = detail.get("AigcVideoTask") or {}
    msg = str(_first(aigc, "Message", "ErrCodeExt") or detail.get("Message") or "")

    if status != "FINISH":
        return status, None, msg

    # 双层状态校验：外层 FINISH 不代表成功，必须看内层 ErrCode
    err_code = aigc.get("ErrCode")
    if err_code not in (None, 0, "0", ""):
        err_ext = aigc.get("ErrCodeExt") or ""
        err_msg = aigc.get("Message") or ""
        combined = f"ErrCode={err_code} {err_ext}: {err_msg}".strip()
        # 改写为 ABORTED，让上游 graph.py 已有的失败分支接管
        return "ABORTED", None, combined

    # 真实成片：AigcVideoTask.Output.FileInfos[0]
    output_node: Any = aigc.get("Output") or aigc.get("OutputSet") or aigc.get("MediaInfo") or {}
    if isinstance(output_node, list):
        output_node = output_node[0] if output_node else {}

    file_node: dict[str, Any] = {}
    file_infos = output_node.get("FileInfos") if isinstance(output_node, dict) else None
    if isinstance(file_infos, list) and file_infos:
        first = file_infos[0]
        if isinstance(first, dict):
            file_node = first
    # 兜底：部分历史/异构返回可能直接把成片字段挂在 output_node 上
    if not file_node and isinstance(output_node, dict):
        file_node = output_node

    vo = VideoOutput(
        file_id=str(_first(file_node, "FileId", "MediaId") or ""),
        url=str(
            _first(file_node, "Url", "MediaUrl", "VideoUrl", "FileUrl", "PlayUrl")
            or ""
        ),
        cover_url=str(_first(file_node, "CoverUrl", "Cover") or ""),
        duration_sec=float(_first(file_node, "Duration", "DurationSec") or 0) or 0.0,
        width=int(_first(file_node, "Width") or 0),
        height=int(_first(file_node, "Height") or 0),
    )
    return status, vo, msg

