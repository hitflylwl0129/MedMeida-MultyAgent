"""访问统计存储层 —— SQLite 单文件，与业务 jobs.db 隔离。

设计要点（详见 访问统计_技术路线与原型.md §4 数据模型）：
- 单表 access_events，append-only，少量索引
- 写入轻量（单条 INSERT），查询走聚合
- 跨进程并发：sqlite3 connect timeout=5s + check_same_thread=False
- 调用方传明文 IP，本层不做脱敏（脱敏发生在 admin API 层，由 access_ip_mask_default 控制）

读路径（admin 端用）：
- get_kpis(since_ts):    PV / UV / 在线 / 平均停留
- get_timeline(since_ts, bucket_sec): 时间序列
- get_section_dist(since_ts):         5 主板块 + marketing 子板块分布
- get_top_ips(since_ts, n):           IP TOP N（含历史板块）
- list_events(since_ts, page, size, filters): 明细分页
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterable, Optional

# 单进程内写锁，避免高并发 visit/heartbeat 互相阻塞
_WRITE_LOCK = threading.Lock()

_SCHEMA = """
CREATE TABLE IF NOT EXISTS access_events (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    ip           TEXT NOT NULL,
    ip_city      TEXT,
    ip_isp       TEXT,
    ua           TEXT,
    ua_browser   TEXT,
    ua_os        TEXT,
    ua_device    TEXT,
    session_id   TEXT NOT NULL,
    referrer     TEXT,
    section      TEXT NOT NULL,
    subsection   TEXT,
    page         TEXT NOT NULL,
    title        TEXT,
    event        TEXT NOT NULL,
    ts           INTEGER NOT NULL,
    dur_sec      INTEGER DEFAULT 0,
    raw          TEXT
);

CREATE INDEX IF NOT EXISTS idx_events_ts      ON access_events(ts);
CREATE INDEX IF NOT EXISTS idx_events_ip      ON access_events(ip);
CREATE INDEX IF NOT EXISTS idx_events_session ON access_events(session_id);
CREATE INDEX IF NOT EXISTS idx_events_section ON access_events(section);
CREATE INDEX IF NOT EXISTS idx_events_event   ON access_events(event);
"""


def _resolve_db_path(rel_or_abs: str) -> Path:
    p = Path(rel_or_abs)
    if not p.is_absolute():
        # 相对 backend/
        p = Path(__file__).resolve().parent.parent / rel_or_abs
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


_DB_INIT_DONE: dict[str, bool] = {}


def _init_db(db_path: Path) -> None:
    key = str(db_path)
    if _DB_INIT_DONE.get(key):
        return
    with sqlite3.connect(db_path, timeout=5.0, check_same_thread=False) as conn:
        conn.executescript(_SCHEMA)
    _DB_INIT_DONE[key] = True


@contextmanager
def _conn(db_path: str | Path):
    p = _resolve_db_path(str(db_path))
    _init_db(p)
    c = sqlite3.connect(p, timeout=5.0, check_same_thread=False)
    c.row_factory = sqlite3.Row
    try:
        yield c
    finally:
        c.close()


# --------------------------------------------------------------------------- #
# 写
# --------------------------------------------------------------------------- #
def insert_event(
    db_path: str,
    *,
    ip: str,
    session_id: str,
    section: str,
    page: str,
    event: str,
    ts: int,
    subsection: str = "",
    ip_city: str = "",
    ip_isp: str = "",
    ua: str = "",
    ua_browser: str = "",
    ua_os: str = "",
    ua_device: str = "",
    referrer: str = "",
    title: str = "",
    dur_sec: int = 0,
    raw: dict | None = None,
) -> int:
    """单条 INSERT。返回新行 id。"""
    raw_json = json.dumps(raw, ensure_ascii=False, default=str) if raw else None
    with _WRITE_LOCK, _conn(db_path) as c:
        cur = c.execute(
            """
            INSERT INTO access_events
              (ip, ip_city, ip_isp, ua, ua_browser, ua_os, ua_device,
               session_id, referrer, section, subsection, page, title,
               event, ts, dur_sec, raw)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                ip[:64], ip_city[:32] or None, ip_isp[:32] or None,
                ua[:200] or None, ua_browser[:32] or None, ua_os[:32] or None, ua_device[:16] or None,
                session_id[:64], referrer[:500] or None,
                section[:32], subsection[:32] or None, page[:300], title[:200] or None,
                event[:16], int(ts), int(dur_sec or 0),
                raw_json,
            ),
        )
        c.commit()
        return int(cur.lastrowid or 0)


def update_leave(
    db_path: str,
    *,
    session_id: str,
    page: str,
    leave_ts: int,
) -> bool:
    """leave 事件：找到该 session 在该 page 的最近一条 visit，更新 dur_sec。

    若当前事件已经是 leave 用 INSERT 即可；这里提供一个"补刀"接口
    用于服务端兜底（心跳超时时把 visit 直接结算）。
    """
    with _WRITE_LOCK, _conn(db_path) as c:
        row = c.execute(
            """
            SELECT id, ts FROM access_events
            WHERE session_id=? AND page=? AND event='visit'
            ORDER BY ts DESC LIMIT 1
            """,
            (session_id, page),
        ).fetchone()
        if not row:
            return False
        dur = max(0, int(leave_ts) - int(row["ts"]))
        c.execute(
            "UPDATE access_events SET dur_sec=? WHERE id=?",
            (dur, row["id"]),
        )
        c.commit()
        return True


# --------------------------------------------------------------------------- #
# 读
# --------------------------------------------------------------------------- #
def get_kpis(db_path: str, since_ts: int, online_window_sec: int) -> dict:
    """KPI：PV / UV / 在线 / 平均停留。"""
    now = int(time.time())
    with _conn(db_path) as c:
        # PV：visit 事件总数
        pv = c.execute(
            "SELECT COUNT(*) AS n FROM access_events WHERE event='visit' AND ts>=?",
            (since_ts,),
        ).fetchone()["n"]
        # UV：distinct(ip) ；UA 维度可选（合规友好下我们只用 IP）
        uv = c.execute(
            "SELECT COUNT(DISTINCT ip) AS n FROM access_events WHERE event='visit' AND ts>=?",
            (since_ts,),
        ).fetchone()["n"]
        # 在线：最近 online_window_sec 内有事件的 distinct session
        online_since = now - online_window_sec
        online = c.execute(
            "SELECT COUNT(DISTINCT session_id) AS n FROM access_events WHERE ts>=?",
            (online_since,),
        ).fetchone()["n"]
        # 平均停留：dur_sec > 0 的事件平均
        avg_row = c.execute(
            "SELECT AVG(dur_sec) AS d FROM access_events WHERE ts>=? AND dur_sec>0",
            (since_ts,),
        ).fetchone()
        avg_dur = int(avg_row["d"] or 0)
    return {
        "pv": int(pv or 0),
        "uv": int(uv or 0),
        "online": int(online or 0),
        "avg_dur_sec": avg_dur,
    }


def get_timeline(db_path: str, since_ts: int, bucket_sec: int = 3600) -> list[dict]:
    """时间序列：[{ts, pv, uv}, ...]，bucket_sec 默认 1 小时。"""
    with _conn(db_path) as c:
        rows = c.execute(
            f"""
            SELECT (ts / {bucket_sec}) * {bucket_sec} AS bucket,
                   COUNT(*) AS pv,
                   COUNT(DISTINCT ip) AS uv
            FROM access_events
            WHERE event='visit' AND ts>=?
            GROUP BY bucket
            ORDER BY bucket
            """,
            (since_ts,),
        ).fetchall()
    return [{"ts": int(r["bucket"]), "pv": int(r["pv"]), "uv": int(r["uv"])} for r in rows]


def get_section_dist(db_path: str, since_ts: int) -> dict:
    """板块分布：{main: [{section, pv}], sub: [{subsection, pv}]}。子板块只统计 marketing 下的。"""
    with _conn(db_path) as c:
        main_rows = c.execute(
            """
            SELECT section, COUNT(*) AS pv
            FROM access_events WHERE event='visit' AND ts>=?
            GROUP BY section ORDER BY pv DESC
            """,
            (since_ts,),
        ).fetchall()
        sub_rows = c.execute(
            """
            SELECT subsection, COUNT(*) AS pv
            FROM access_events
            WHERE event='visit' AND ts>=? AND section='marketing'
              AND subsection IS NOT NULL AND subsection!=''
            GROUP BY subsection ORDER BY pv DESC
            """,
            (since_ts,),
        ).fetchall()
    return {
        "main": [{"section": r["section"], "pv": int(r["pv"])} for r in main_rows],
        "sub":  [{"subsection": r["subsection"], "pv": int(r["pv"])} for r in sub_rows],
    }


def get_top_ips(db_path: str, since_ts: int, n: int = 10) -> list[dict]:
    """IP TOP N，附带"曾访问过的板块" + 总停留秒数。"""
    with _conn(db_path) as c:
        rows = c.execute(
            """
            SELECT ip,
                   COALESCE(MAX(ip_city),'') AS city,
                   COUNT(*) AS pv,
                   COALESCE(SUM(dur_sec),0) AS dur,
                   GROUP_CONCAT(DISTINCT section) AS secs
            FROM access_events
            WHERE event='visit' AND ts>=?
            GROUP BY ip
            ORDER BY pv DESC
            LIMIT ?
            """,
            (since_ts, n),
        ).fetchall()
    out = []
    for r in rows:
        secs = (r["secs"] or "").split(",") if r["secs"] else []
        out.append({
            "ip": r["ip"],
            "city": r["city"] or "",
            "pv": int(r["pv"]),
            "dur_sec": int(r["dur"] or 0),
            "sections": [s for s in secs if s],
        })
    return out


def list_events(
    db_path: str,
    *,
    since_ts: int,
    page: int = 1,
    size: int = 50,
    section: Optional[str] = None,
    keyword: Optional[str] = None,
) -> dict:
    """明细分页查询。返回 {total, page, size, rows}。"""
    page = max(1, int(page))
    size = max(1, min(500, int(size)))
    where = ["event='visit'", "ts>=?"]
    args: list[Any] = [since_ts]
    if section:
        where.append("(section=? OR subsection=?)")
        args.extend([section, section])
    if keyword:
        kw = f"%{keyword}%"
        where.append("(ip LIKE ? OR ip_city LIKE ? OR ua LIKE ? OR page LIKE ?)")
        args.extend([kw, kw, kw, kw])
    where_sql = " AND ".join(where)
    with _conn(db_path) as c:
        total = c.execute(
            f"SELECT COUNT(*) AS n FROM access_events WHERE {where_sql}", args,
        ).fetchone()["n"]
        rows = c.execute(
            f"""
            SELECT id, ip, ip_city, ip_isp, ua_browser, ua_os, ua_device,
                   session_id, referrer, section, subsection, page, title,
                   ts, dur_sec
            FROM access_events
            WHERE {where_sql}
            ORDER BY ts DESC
            LIMIT ? OFFSET ?
            """,
            args + [size, (page - 1) * size],
        ).fetchall()
    return {
        "total": int(total or 0),
        "page": page, "size": size,
        "rows": [dict(r) for r in rows],
    }


def get_footer_summary(db_path: str, online_window_sec: int) -> dict:
    """页脚条：今日 PV / UV、累计 PV、当前在线。"""
    now = int(time.time())
    today_start = int(time.mktime(time.localtime(now)[:3] + (0, 0, 0, 0, 0, -1)))
    online_since = now - online_window_sec
    with _conn(db_path) as c:
        today_pv = c.execute(
            "SELECT COUNT(*) AS n FROM access_events WHERE event='visit' AND ts>=?",
            (today_start,),
        ).fetchone()["n"]
        today_uv = c.execute(
            "SELECT COUNT(DISTINCT ip) AS n FROM access_events WHERE event='visit' AND ts>=?",
            (today_start,),
        ).fetchone()["n"]
        total_pv = c.execute(
            "SELECT COUNT(*) AS n FROM access_events WHERE event='visit'",
        ).fetchone()["n"]
        online = c.execute(
            "SELECT COUNT(DISTINCT session_id) AS n FROM access_events WHERE ts>=?",
            (online_since,),
        ).fetchone()["n"]
    return {
        "today_pv": int(today_pv or 0),
        "today_uv": int(today_uv or 0),
        "total_pv": int(total_pv or 0),
        "online": int(online or 0),
    }


# --------------------------------------------------------------------------- #
# 维护
# --------------------------------------------------------------------------- #
def purge_older_than(db_path: str, before_ts: int) -> int:
    """删除早于 before_ts 的记录；返回删除条数。PR-4 定时任务用。"""
    with _WRITE_LOCK, _conn(db_path) as c:
        cur = c.execute("DELETE FROM access_events WHERE ts<?", (before_ts,))
        c.commit()
        return cur.rowcount or 0


def export_csv(db_path: str, since_ts: int, fp) -> int:
    """导出 since_ts 之后的事件到 CSV file-like。返回写入行数。"""
    import csv
    with _conn(db_path) as c:
        rows = c.execute(
            """
            SELECT id, ip, ip_city, ua_browser, ua_os, ua_device,
                   session_id, section, subsection, page, ts, dur_sec
            FROM access_events
            WHERE ts>=? ORDER BY ts ASC
            """,
            (since_ts,),
        ).fetchall()
    if not rows:
        return 0
    w = csv.writer(fp)
    w.writerow(rows[0].keys())
    for r in rows:
        w.writerow([r[k] for k in r.keys()])
    return len(rows)
