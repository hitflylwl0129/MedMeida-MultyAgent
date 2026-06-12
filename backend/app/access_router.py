"""访问统计 API 路由 —— ingest（公开）+ admin（BasicAuth）。

设计要点（详见 访问统计_技术路线与原型.md §7）：
- ingest 走 GET /api/track/p.gif?event=visit&...（1×1 透明 GIF）
  → 浏览器 <img> / Image() 触发 → 0 CORS 限制 / 0 OPTIONS 预检
- leave 用 navigator.sendBeacon('/api/track/leave', body) → POST，无阻塞
- admin 全部受 BasicAuth 保护；user/pass 来自 .env
- IP 取 X-Real-IP（nginx 已转发），不信任 X-Forwarded-For
- UA 解析 lazy import user-agents，缺包时降级用正则猜
- GeoIP 解析 lazy import geoip2.database，无 mmdb 时跳过
- 限流：单 IP 5 req/sec（内存 token bucket，进程内即可）
"""
from __future__ import annotations

import base64
import io
import logging
import re
import secrets
import time
from collections import defaultdict
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from fastapi.responses import JSONResponse, Response as RawResponse, StreamingResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials

from . import access_store
from .config import get_settings

log = logging.getLogger("video-agent.access")

router = APIRouter(prefix="/api", tags=["access"])

# 1×1 透明 GIF（43 字节）
_PIXEL_GIF = base64.b64decode(
    "R0lGODlhAQABAIAAAAAAAP///yH5BAEAAAAALAAAAAABAAEAAAIBRAA7"
)

# ============================================================================ #
# 辅助函数
# ============================================================================ #
def _client_ip(request: Request) -> str:
    """nginx 已配 X-Real-IP；本地直连用 client.host。不信任 X-Forwarded-For。"""
    return (
        request.headers.get("x-real-ip")
        or (request.client.host if request.client else "0.0.0.0")
    )


def _mask_ip(ip: str) -> str:
    """IP 脱敏：IPv4 末段 → ***，IPv6 末 4 段 → ::***。"""
    if "." in ip:
        parts = ip.split(".")
        if len(parts) == 4:
            return ".".join(parts[:3]) + ".***"
    if ":" in ip:
        return ip.rsplit(":", 1)[0] + ":***"
    return ip


# ---- UA 解析（lazy + fallback）---------------------------------------------- #
_UA_PARSER = None
_UA_PARSER_INIT = False


def _get_ua_parser():
    global _UA_PARSER, _UA_PARSER_INIT
    if _UA_PARSER_INIT:
        return _UA_PARSER
    _UA_PARSER_INIT = True
    try:
        from user_agents import parse  # type: ignore
        _UA_PARSER = parse
        log.info("UA parser ready (user-agents)")
    except Exception as e:  # noqa: BLE001
        log.warning("user-agents 包未装，UA 解析降级到正则：%s", e)
        _UA_PARSER = None
    return _UA_PARSER


_UA_BROWSER_RE = re.compile(
    r"(Edg|Edge|OPR|Chrome|Firefox|Safari|MSIE|Trident)/?([\d.]*)",
    re.I,
)
_UA_OS_RE = re.compile(
    r"(Windows NT [\d.]+|Mac OS X [\d_]+|Android [\d.]+|iPhone OS [\d_]+|Linux)",
    re.I,
)


def parse_ua(ua: str) -> tuple[str, str, str]:
    """返回 (browser, os, device)。"""
    if not ua:
        return ("", "", "desktop")
    parse = _get_ua_parser()
    if parse:
        try:
            u = parse(ua)
            browser = f"{u.browser.family} {u.browser.version_string or ''}".strip()
            os_str = f"{u.os.family} {u.os.version_string or ''}".strip()
            device = "mobile" if u.is_mobile else ("tablet" if u.is_tablet else "desktop")
            return (browser[:32], os_str[:32], device)
        except Exception:  # noqa: BLE001
            pass
    # fallback 正则
    bm = _UA_BROWSER_RE.search(ua)
    om = _UA_OS_RE.search(ua)
    browser = (bm.group(1) + " " + (bm.group(2) or "")).strip() if bm else ""
    os_str = om.group(1) if om else ""
    is_mobile = bool(re.search(r"Mobile|Android|iPhone|iPad", ua, re.I))
    device = "mobile" if is_mobile else "desktop"
    return (browser[:32], os_str[:32], device)


# ---- GeoIP（lazy + fallback）------------------------------------------------ #
_GEOIP_READER = None
_GEOIP_READER_INIT = False


def _get_geoip_reader():
    global _GEOIP_READER, _GEOIP_READER_INIT
    if _GEOIP_READER_INIT:
        return _GEOIP_READER
    _GEOIP_READER_INIT = True
    s = get_settings()
    p = Path(s.access_geoip_path)
    if not p.is_absolute():
        p = Path(__file__).resolve().parent.parent / s.access_geoip_path
    if not p.is_file():
        log.warning("GeoIP mmdb 不存在：%s（城市解析将跳过）", p)
        return None
    try:
        import geoip2.database  # type: ignore
        _GEOIP_READER = geoip2.database.Reader(str(p))
        log.info("GeoIP mmdb 就绪：%s", p)
    except Exception as e:  # noqa: BLE001
        log.warning("GeoIP 初始化失败：%s", e)
    return _GEOIP_READER


def lookup_city(ip: str) -> str:
    reader = _get_geoip_reader()
    if not reader:
        return ""
    try:
        r = reader.city(ip)
        # 中文优先 → 英文兜底
        names = (r.city.names or {})
        return names.get("zh-CN") or names.get("en") or ""
    except Exception:  # noqa: BLE001
        return ""


# ---- 限流（内存 token bucket）---------------------------------------------- #
_RATE_BUCKETS: dict[str, list[float]] = defaultdict(list)  # ip -> 最近 1 秒内的时间戳列表
_RATE_LOCK_LAST_GC = 0.0


def _check_rate(ip: str, per_sec: int) -> bool:
    """超出限额返回 False。简单滑动窗口，进程内即可（多 worker 时各自计数也够用）。"""
    now = time.time()
    bucket = _RATE_BUCKETS[ip]
    # 清掉 > 1 秒前的
    cutoff = now - 1.0
    bucket[:] = [t for t in bucket if t > cutoff]
    if len(bucket) >= per_sec:
        return False
    bucket.append(now)
    # 偶尔 GC 整张表，防止键无限增长
    global _RATE_LOCK_LAST_GC
    if now - _RATE_LOCK_LAST_GC > 60:
        _RATE_LOCK_LAST_GC = now
        for k in list(_RATE_BUCKETS.keys()):
            if not _RATE_BUCKETS[k]:
                _RATE_BUCKETS.pop(k, None)
    return True


# ---- BasicAuth -------------------------------------------------------------- #
_basic = HTTPBasic()


def admin_auth(creds: HTTPBasicCredentials = Depends(_basic)) -> str:
    s = get_settings()
    ok_user = secrets.compare_digest(creds.username, s.access_admin_user)
    ok_pass = secrets.compare_digest(creds.password, s.access_admin_pass)
    if not (ok_user and ok_pass):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="用户名或密码错误",
            headers={"WWW-Authenticate": 'Basic realm="admin"'},
        )
    return creds.username


# ============================================================================ #
# Ingest 端点（公开）
# ============================================================================ #
# 允许的 event 与 section 白名单（防注入到表里乱七八糟值）
_EVENTS_OK = {"visit", "heartbeat", "leave"}
_SECTIONS_OK = {"marketing", "ocr", "asr", "raw", "qc", "unknown"}
_SUBSECTIONS_OK = {
    "home", "product", "doctor", "script", "video",
    "distribute", "audience", "admin", "index",
    "",  # 非 marketing 板块时为空
}


@router.get("/track/p.gif")
def track_pixel(
    request: Request,
    e: str = Query("visit", description="事件：visit / heartbeat / leave"),
    s: str = Query("unknown", description="主板块"),
    ss: str = Query("", description="子板块（marketing.* 才有）"),
    p: str = Query("/", description="page pathname"),
    sid: str = Query("", description="session_id（前端 sessionStorage）"),
    r: str = Query("", description="referrer"),
    t: str = Query("", description="document.title"),
    d: int = Query(0, description="dur_sec（leave 时填）"),
):
    """1×1 透明 GIF ingest。

    为什么 GET 而非 POST：浏览器 <img>/Image() 触发，0 CORS / 0 OPTIONS / 0 阻塞。
    """
    settings = get_settings()
    ip = _client_ip(request)

    # 限流（写库前挡住）
    if not _check_rate(ip, settings.access_ingest_rate_per_sec):
        # 限流也返回 GIF，避免触发浏览器报错
        return _gif_response()

    # 白名单校验，命中走兜底值
    event = e if e in _EVENTS_OK else "visit"
    section = s if s in _SECTIONS_OK else "unknown"
    subsection = ss if ss in _SUBSECTIONS_OK else ""

    ua = request.headers.get("user-agent") or ""
    browser, os_str, device = parse_ua(ua)
    city = lookup_city(ip)

    try:
        access_store.insert_event(
            settings.access_db_path,
            ip=ip,
            session_id=sid or f"_{int(time.time())}",
            section=section,
            subsection=subsection,
            page=p[:300] or "/",
            event=event,
            ts=int(time.time()),
            dur_sec=int(d or 0),
            ip_city=city,
            ua=ua,
            ua_browser=browser,
            ua_os=os_str,
            ua_device=device,
            referrer=r[:500] or "",
            title=t[:200] or "",
        )
    except Exception as ex:  # noqa: BLE001
        # 统计模块绝不能影响主业务，吞错
        log.warning("访问统计写库失败：%s", ex)

    return _gif_response()


@router.post("/track/leave")
async def track_leave(request: Request):
    """sendBeacon 入口（POST）。读 body 取 sid/page/dur，写一条 leave 事件。

    sendBeacon 发送时不带自定义 header，body 是 JSON 字符串。
    """
    settings = get_settings()
    ip = _client_ip(request)
    if not _check_rate(ip, settings.access_ingest_rate_per_sec):
        return JSONResponse({"ok": False, "reason": "rate_limit"}, status_code=429)
    try:
        import json
        raw = (await request.body()).decode("utf-8", errors="replace")
        data = json.loads(raw or "{}")
    except Exception:  # noqa: BLE001
        return JSONResponse({"ok": False, "reason": "bad_json"}, status_code=400)

    event = "leave"
    section = data.get("s") or "unknown"
    subsection = data.get("ss") or ""
    page = (data.get("p") or "/")[:300]
    sid = (data.get("sid") or "")[:64]
    d = int(data.get("d") or 0)
    ua = request.headers.get("user-agent") or ""
    browser, os_str, device = parse_ua(ua)
    city = lookup_city(ip)
    if section not in _SECTIONS_OK:
        section = "unknown"
    if subsection not in _SUBSECTIONS_OK:
        subsection = ""
    try:
        access_store.insert_event(
            settings.access_db_path,
            ip=ip, session_id=sid or f"_{int(time.time())}",
            section=section, subsection=subsection, page=page,
            event=event, ts=int(time.time()), dur_sec=d,
            ip_city=city, ua=ua,
            ua_browser=browser, ua_os=os_str, ua_device=device,
        )
    except Exception as ex:  # noqa: BLE001
        log.warning("leave 写库失败：%s", ex)
    return JSONResponse({"ok": True})


@router.get("/track/footer")
def track_footer(request: Request):
    """页脚条数据：今日 PV / UV / 累计 / 在线 / 您的 IP（脱敏后展示）。"""
    settings = get_settings()
    ip = _client_ip(request)
    summary = access_store.get_footer_summary(
        settings.access_db_path, settings.access_online_window_sec,
    )
    summary["ip"] = _mask_ip(ip) if settings.access_ip_mask_default else ip
    return summary


# 1×1 透明 GIF 响应（含禁缓存 header）
def _gif_response() -> Response:
    return Response(
        content=_PIXEL_GIF,
        media_type="image/gif",
        headers={
            "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
            "Pragma": "no-cache",
        },
    )


# ============================================================================ #
# Admin 端点（BasicAuth）
# ============================================================================ #
# 时间窗口快捷映射
_RANGE_SEC = {
    "1h": 3600, "today": 86400, "7d": 86400 * 7, "30d": 86400 * 30,
}


def _resolve_since(rng: str) -> int:
    """rng → since_ts。'today' 用本地零点。"""
    now = int(time.time())
    if rng == "today":
        t = time.localtime(now)
        return int(time.mktime(t[:3] + (0, 0, 0, 0, 0, -1)))
    sec = _RANGE_SEC.get(rng, 86400)
    return now - sec


@router.get("/admin/stats/overview")
def admin_overview(rng: str = "today", _user: str = Depends(admin_auth)):
    """KPI + 趋势 + 板块分布 + IP TOP 一次取齐（admin 首屏）。"""
    settings = get_settings()
    since = _resolve_since(rng)
    bucket = 3600 if rng in ("today", "1h") else 86400  # 1 小时 / 1 天
    return {
        "rng": rng,
        "since_ts": since,
        "bucket_sec": bucket,
        "kpis": access_store.get_kpis(
            settings.access_db_path, since, settings.access_online_window_sec,
        ),
        "timeline": access_store.get_timeline(settings.access_db_path, since, bucket),
        "sections": access_store.get_section_dist(settings.access_db_path, since),
        "top_ips":  access_store.get_top_ips(settings.access_db_path, since, 10),
    }


@router.get("/admin/stats/events")
def admin_events(
    rng: str = "today",
    page: int = 1,
    size: int = 50,
    section: Optional[str] = None,
    keyword: Optional[str] = None,
    mask_ip: Optional[bool] = None,
    _user: str = Depends(admin_auth),
):
    """明细分页查询（前端表格）。`mask_ip` 不传时用 .env 默认。"""
    settings = get_settings()
    since = _resolve_since(rng)
    page_data = access_store.list_events(
        settings.access_db_path,
        since_ts=since, page=page, size=size,
        section=section, keyword=keyword,
    )
    do_mask = settings.access_ip_mask_default if mask_ip is None else bool(mask_ip)
    if do_mask:
        for r in page_data["rows"]:
            r["ip"] = _mask_ip(r.get("ip", ""))
    return page_data


@router.get("/admin/stats/export")
def admin_export(
    rng: str = "today",
    _user: str = Depends(admin_auth),
):
    """导出 CSV。"""
    settings = get_settings()
    since = _resolve_since(rng)
    buf = io.StringIO()
    n = access_store.export_csv(settings.access_db_path, since, buf)
    buf.seek(0)
    return StreamingResponse(
        iter([buf.getvalue()]),
        media_type="text/csv; charset=utf-8",
        headers={
            "Content-Disposition": f'attachment; filename="access_{rng}_{n}rows.csv"',
        },
    )
