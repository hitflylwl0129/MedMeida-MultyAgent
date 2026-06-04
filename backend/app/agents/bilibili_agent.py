"""B站（哔哩哔哩）Web 投稿 Agent —— 短视频分发 Agent 的「真实跑通」链路。

完整复刻 B站 Web 端投稿协议（bilibili-API-collect 文档）：
    preupload（预上传，拿 auth/biz_id/chunk_size/endpoint/upos_uri）
      → POST ?uploads（初始化分片会话，拿 upload_id）
      → PUT 分片（逐片上传，拿每片 eTag）
      → POST complete（合片）
      → POST x/vu/web/add/v3（提交稿件，拿 aid / bvid）

鉴权：Cookie(SESSDATA + bili_jct)。**凭证只在后端 .env，绝不下发前端、不入版本库。**

对外暴露：
    credentials_ready(s)         -> bool        是否已配置 SESSDATA/bili_jct
    latest_local_video()         -> str|None    最近一条本地成片 out.mp4 路径（兜底素材源）
    publish_stream(...)          -> Generator   流式产出 stage/progress/done 事件（供 SSE）

安全：本模块会真实地向配置账号投稿。默认 only_self=1（仅自己可见），避免误发公开内容。
"""
from __future__ import annotations

import logging
import math
import time
import urllib.parse
from pathlib import Path
from typing import Generator, Optional

import httpx

from ..config import Settings, get_settings

log = logging.getLogger("video-agent.bilibili")

_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
# backend/.cache/jobs（与 main.py / orchestrator 保持一致）
_LOCAL_JOBS_DIR = Path(__file__).resolve().parents[2] / ".cache" / "jobs"


class BiliError(RuntimeError):
    """B站投稿链路的可读错误。"""


# --------------------------------------------------------------------------- #
# 凭证 / 素材
# --------------------------------------------------------------------------- #
def credentials_ready(s: Optional[Settings] = None) -> bool:
    s = s or get_settings()
    return bool(s.bili_sessdata and s.bili_jct)


def latest_local_video() -> Optional[str]:
    """返回最近一条本地成片 .cache/jobs/*/out.mp4（按 mtime）；无则 None。"""
    if not _LOCAL_JOBS_DIR.is_dir():
        return None
    cands = sorted(
        _LOCAL_JOBS_DIR.glob("*/out.mp4"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    return str(cands[0]) if cands else None


def _cookies(s: Settings) -> dict:
    c = {"SESSDATA": s.bili_sessdata, "bili_jct": s.bili_jct}
    if s.bili_buvid3:
        c["buvid3"] = s.bili_buvid3
    return c


def _merge(params: dict, put_query: str) -> dict:
    """把 preupload 返回的 put_query（urlencoded）合并进请求 params。"""
    if not put_query:
        return params
    extra = dict(urllib.parse.parse_qsl(put_query))
    merged = dict(extra)
    merged.update(params)  # 业务参数优先
    return merged


# --------------------------------------------------------------------------- #
# 投稿主流程（流式）
# --------------------------------------------------------------------------- #
def publish_stream(
    *,
    video_path: str,
    title: str,
    desc: str = "",
    tag: str = "",
    tid: int = 0,
    copyright: int = 0,
    cover: str = "",
    only_self: int = -1,
    settings: Optional[Settings] = None,
) -> Generator[dict, None, None]:
    """执行 B站投稿，yield 事件 dict：

      {"event":"stage",   "data":{"step":..,"msg":..}}
      {"event":"progress","data":{"phase":"upload","percent":N,"chunk":i,"chunks":n}}
      {"event":"done",    "data":{"aid":..,"bvid":..,"url":..,"title":..}}

    出错抛 BiliError，由调用方转成 failed 事件。
    """
    s = settings or get_settings()
    if not credentials_ready(s):
        raise BiliError("B站凭证未配置：请在 backend/.env 设置 BILI_SESSDATA / BILI_JCT")

    p = Path(video_path)
    if not p.is_file():
        raise BiliError(f"成片文件不存在：{video_path}")

    size = p.stat().st_size
    name = p.name
    profile = s.bili_upload_profile
    tid = tid or s.bili_default_tid
    copyright = copyright or s.bili_default_copyright
    tag = (tag or s.bili_default_tag).strip(",")
    only_self = s.bili_only_self if only_self < 0 else only_self
    title = (title or "未命名视频")[:80]
    # B站标签最多 10 个
    tag = ",".join([t.strip() for t in tag.split(",") if t.strip()][:10])

    yield {"event": "stage", "data": {"step": "auth",
           "msg": f"凭证校验通过，准备上传《{title}》（{size/1024/1024:.2f} MB · {name}）"}}

    headers = {"User-Agent": _UA, "Referer": "https://member.bilibili.com/"}
    with httpx.Client(timeout=s.bili_timeout_sec, headers=headers, cookies=_cookies(s)) as cli:
        # ---- 1) preupload 预上传 -------------------------------------------- #
        pre = cli.get(
            "https://member.bilibili.com/preupload",
            params={
                "name": name, "size": size, "r": "upos", "profile": profile,
                "ssl": "0", "version": "2.14.0", "build": "2140000",
                "upcdn": "bda2", "probe_version": "20221109",
            },
        )
        pre_j = pre.json()
        if pre_j.get("OK") != 1:
            raise BiliError(f"preupload 失败（可能凭证失效）：{pre_j}")
        auth = pre_j["auth"]
        biz_id = pre_j["biz_id"]
        chunk_size = int(pre_j["chunk_size"])
        endpoint = pre_j["endpoint"]
        upos_uri = pre_j["upos_uri"]
        put_query = pre_j.get("put_query", "")

        upload_url = "https:" + endpoint + "/" + upos_uri.replace("upos://", "")
        upos_basename = upos_uri.split("/")[-1]           # 形如 n2407...mp4
        filename = upos_basename.rsplit(".", 1)[0]         # add 接口要无后缀文件名
        upos_headers = {"X-Upos-Auth": auth}

        yield {"event": "stage", "data": {"step": "preupload",
               "msg": f"预上传完成 · 节点 {endpoint.strip('/')} · 分块 {chunk_size//1024//1024}MB"}}

        # ---- 2) 初始化分片会话 POST ?uploads -------------------------------- #
        init = cli.post(
            upload_url,
            params=_merge({
                "uploads": "", "output": "json", "profile": profile,
                "filesize": size, "partsize": chunk_size, "biz_id": biz_id,
            }, put_query),
            headers=upos_headers,
        )
        init_j = init.json()
        upload_id = init_j.get("upload_id")
        if not upload_id:
            raise BiliError(f"初始化分片失败：{init_j}")
        yield {"event": "stage", "data": {"step": "init",
               "msg": f"分片会话已建立 upload_id={str(upload_id)[:14]}…"}}

        # ---- 3) 分片上传 PUT ------------------------------------------------ #
        chunks = max(1, math.ceil(size / chunk_size))
        parts: list[dict] = []
        with p.open("rb") as f:
            for idx in range(chunks):
                start = idx * chunk_size
                data = f.read(chunk_size)
                end = start + len(data)
                r = cli.put(
                    upload_url,
                    params=_merge({
                        "partNumber": idx + 1, "uploadId": upload_id,
                        "chunk": idx, "chunks": chunks, "size": len(data),
                        "start": start, "end": end, "total": size,
                    }, put_query),
                    headers={**upos_headers, "Content-Type": "application/octet-stream"},
                    content=data,
                )
                if r.status_code != 200:
                    raise BiliError(f"分片 {idx+1}/{chunks} 上传失败：HTTP {r.status_code} {r.text[:200]}")
                etag = (r.headers.get("Etag") or r.headers.get("ETag") or "etag").strip('"') or "etag"
                parts.append({"partNumber": idx + 1, "eTag": etag})
                yield {"event": "progress", "data": {
                    "phase": "upload", "percent": round((idx + 1) / chunks * 100),
                    "chunk": idx + 1, "chunks": chunks}}

        # ---- 4) 合片 complete ---------------------------------------------- #
        comp = cli.post(
            upload_url,
            params=_merge({
                "output": "json", "name": name, "profile": profile,
                "uploadId": upload_id, "biz_id": biz_id,
            }, put_query),
            headers={**upos_headers, "Content-Type": "application/json"},
            json={"parts": parts},
        )
        comp_j = comp.json()
        if comp_j.get("OK") != 1:
            raise BiliError(f"合片失败：{comp_j}")
        yield {"event": "stage", "data": {"step": "complete",
               "msg": "分片合并完成，正在提交稿件…"}}

        # ---- 5) 提交稿件 add/v3 -------------------------------------------- #
        body = {
            "copyright": copyright,
            "source": "",
            "title": title,
            "tid": tid,
            "tag": tag,
            "desc_format_id": 9999,
            "desc": (desc or "")[:2000],
            "recreate": -1,
            "dynamic": "",
            "interactive": 0,
            "no_disturbance": 0,
            "no_reprint": 1,
            "subtitle": {"open": 0, "lan": ""},
            "dolby": 0,
            "lossless_music": 0,
            "up_selection_reply": False,
            "up_close_reply": False,
            "up_close_danmu": False,
            "web_os": 3,
            "is_only_self": only_self,
            "cover": cover or "",
            "videos": [{"filename": filename, "title": title, "desc": "", "cid": biz_id}],
            "csrf": s.bili_jct,
        }
        add = cli.post(
            "https://member.bilibili.com/x/vu/web/add/v3",
            params={"csrf": s.bili_jct, "ts": int(time.time() * 1000)},
            headers={"Content-Type": "application/json; charset=utf-8"},
            json=body,
        )
        add_j = add.json()
        if add_j.get("code") != 0:
            raise BiliError(f"投稿失败 code={add_j.get('code')}：{add_j.get('message')}")
        d = add_j.get("data") or {}
        bvid = d.get("bvid", "")
        aid = d.get("aid", "")
        log.info("B站投稿成功 aid=%s bvid=%s", aid, bvid)
        yield {"event": "done", "data": {
            "aid": aid, "bvid": bvid,
            "url": f"https://www.bilibili.com/video/{bvid}" if bvid else "",
            "title": title,
            "visibility": "仅自己可见" if only_self == 1 else "公开",
        }}
