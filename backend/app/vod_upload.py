"""医生形象图上传到 VOD，换取首帧 FileId。

两条路径（下个任务拿到素材后任选其一）：
  A) 本地文件直传：用 vod-python-sdk(qcloud_vod) 的 VodUploadClient
  B) URL 拉取上传：用 VOD 的 PullUpload 接口（异步，返回 TaskId 再轮询）

avatar_i2v 要求首帧为医生正面形象图（jpeg/jpg/png，≤10M）。
"""
from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Optional

from tencentcloud.common import credential
from tencentcloud.common.exception.tencent_cloud_sdk_exception import (
    TencentCloudSDKException,
)
from tencentcloud.vod.v20180717 import models, vod_client

from .config import Settings, get_settings
from .vod_client import VodError, describe_task

log = logging.getLogger("video-agent.upload")


def upload_local_file(file_path: str, settings: Optional[Settings] = None) -> str:
    """A) 本地文件直传（图/视频通用）→ 返回 FileId。需要 vod-python-sdk。

    底层走 VOD 分片上传协议，对 jpeg/jpg/png/mp4/mov 等均可。
    """
    p = Path(file_path)
    if not p.is_file():
        raise VodError(f"待上传文件不存在：{file_path}")

    try:
        from qcloud_vod.vod_upload_client import VodUploadClient
        from qcloud_vod.model import VodUploadRequest
    except ImportError as e:  # noqa: BLE001
        raise VodError(
            "本地直传需要 vod-python-sdk：pip install vod-python-sdk"
        ) from e

    s = settings or get_settings()
    if not s.credentials_ready:
        raise VodError("腾讯云密钥未配置")

    client = VodUploadClient(s.tencentcloud_secret_id, s.tencentcloud_secret_key)
    req = VodUploadRequest()
    req.MediaFilePath = str(p)
    req.SubAppId = s.vod_sub_app_id
    try:
        rsp = client.upload(s.vod_region, req)
    except Exception as e:  # noqa: BLE001
        raise VodError(f"本地文件上传失败：{e}") from e
    file_id = getattr(rsp, "FileId", "") or ""
    if not file_id:
        raise VodError("上传成功但未取得 FileId")
    log.info("本地文件已上传 FileId=%s (%s)", file_id, p.name)
    return file_id


# 向后兼容别名（旧调用 doctors.resolve_doctor_file_id 仍依赖这个名字）
upload_local_image = upload_local_file



def pull_upload_url(
    media_url: str, name: str = "doctor_first_frame", settings: Optional[Settings] = None
) -> str:
    """B) URL 拉取上传（异步）→ 轮询拉取任务直到完成 → 返回 FileId。"""
    s = settings or get_settings()
    if not s.credentials_ready:
        raise VodError("腾讯云密钥未配置")

    cred = credential.Credential(s.tencentcloud_secret_id, s.tencentcloud_secret_key)
    client = vod_client.VodClient(cred, s.vod_region)

    req = models.PullUploadRequest()
    req.from_json_string(
        json.dumps(
            {"MediaUrl": media_url, "MediaName": name, "SubAppId": s.vod_sub_app_id}
        )
    )
    try:
        resp = client.PullUpload(req)
    except TencentCloudSDKException as e:  # noqa: BLE001
        raise VodError(f"PullUpload 失败：{e.get_code()} {e.get_message()}") from e

    task_id = getattr(resp, "TaskId", "") or ""
    if not task_id:
        raise VodError("PullUpload 未返回 TaskId")

    # 轮询拉取任务，拿成片 FileId
    deadline = time.time() + 180
    while time.time() < deadline:
        detail = describe_task(task_id, s)
        status = str(detail.get("Status") or "").upper()
        if status == "FINISH":
            node = detail.get("PullUploadTask") or {}
            file_id = str(node.get("FileId") or "")
            if not file_id:
                raise VodError(f"PullUpload 完成但无 FileId：{detail}")
            log.info("URL 拉取上传完成 FileId=%s", file_id)
            return file_id
        if status == "ABORTED":
            raise VodError(f"PullUpload 任务终止：{detail}")
        time.sleep(3)
    raise VodError("PullUpload 轮询超时")


def ensure_doctor_file_id(
    *,
    file_id: Optional[str] = None,
    doctor_key: Optional[str] = None,
    url: Optional[str] = None,
    settings: Optional[Settings] = None,
) -> str:
    """解析医生形象图 FileId（优先级从高到低）：

    1) 显式传入的 file_id
    2) doctor_key 命中形象库 → 本地直传/缓存换 FileId
    3) 配置默认形象库 default_doctor → 本地直传/缓存
    4) 配置 doctor_image_fileid
    5) 入参/配置 URL 拉取上传
    """
    from . import doctors  # 延迟导入避免循环

    s = settings or get_settings()
    if file_id:
        return file_id

    # 形象库（入参 key 或配置默认 key）
    key = doctor_key or s.default_doctor
    if key and doctors.get_doctor(key):
        try:
            return doctors.resolve_doctor_file_id(key, settings=s)
        except (FileNotFoundError, ValueError) as e:
            log.warning("形象库解析失败(%s)，回退其他来源：%s", key, e)

    if s.doctor_image_fileid:
        return s.doctor_image_fileid
    target_url = url or s.doctor_image_url
    if target_url:
        return pull_upload_url(target_url, settings=s)
    return ""  # 调用方决定是否允许无首帧
