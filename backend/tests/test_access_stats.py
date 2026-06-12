"""访问统计 v1.0 单元测试。

只覆盖可离线验证的纯逻辑：
1. _mask_ip：IPv4/v6 末段脱敏
2. parse_ua：UA 解析（含 fallback 正则）
3. 限流 _check_rate：超出阈值返回 False
4. SQLite store：写入 → 查询往返
5. 板块字典 + 白名单（通过 _SECTIONS_OK / _SUBSECTIONS_OK）

运行：
    cd backend
    .venv/bin/python -m pytest tests/test_access_stats.py -v
"""
from __future__ import annotations

import io
import time
from pathlib import Path

import pytest

from app import access_router as ar
from app import access_store as st


# ============================================================================ #
# IP 脱敏
# ============================================================================ #
class TestMaskIp:
    def test_ipv4(self):
        assert ar._mask_ip("192.168.1.42") == "192.168.1.***"
        assert ar._mask_ip("8.8.8.8") == "8.8.8.***"

    def test_ipv6(self):
        assert ar._mask_ip("2001:db8::1") == "2001:db8::***"

    def test_invalid_passthrough(self):
        # 非法输入不抛异常
        assert ar._mask_ip("") == ""
        assert ar._mask_ip("not-an-ip") == "not-an-ip"


# ============================================================================ #
# UA 解析
# ============================================================================ #
class TestParseUa:
    def test_chrome_mac(self):
        ua = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
              "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36")
        b, o, d = ar.parse_ua(ua)
        assert "Chrome" in b or "Mac" in o
        assert d in ("desktop", "mobile", "tablet")

    def test_safari_ios(self):
        ua = ("Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
              "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1")
        b, o, d = ar.parse_ua(ua)
        assert d == "mobile"

    def test_empty(self):
        assert ar.parse_ua("") == ("", "", "desktop")


# ============================================================================ #
# 限流
# ============================================================================ #
class TestRateLimit:
    def setup_method(self):
        ar._RATE_BUCKETS.clear()

    def test_under_limit(self):
        for _ in range(5):
            assert ar._check_rate("1.2.3.4", per_sec=5) is True

    def test_over_limit(self):
        for _ in range(5):
            ar._check_rate("9.9.9.9", per_sec=5)
        assert ar._check_rate("9.9.9.9", per_sec=5) is False

    def test_isolated_per_ip(self):
        for _ in range(5):
            ar._check_rate("1.1.1.1", per_sec=5)
        # 另一个 IP 不受影响
        assert ar._check_rate("2.2.2.2", per_sec=5) is True


# ============================================================================ #
# SQLite store
# ============================================================================ #
@pytest.fixture
def tmp_db(tmp_path):
    """每个测试一个独立 db 文件。"""
    p = tmp_path / "access.db"
    # store 模块按 path 字符串缓存 init 状态，确保新路径触发新建表
    st._DB_INIT_DONE.clear()
    return str(p)


class TestStore:
    def test_insert_and_kpis(self, tmp_db):
        now = int(time.time())
        for i in range(3):
            st.insert_event(
                tmp_db, ip=f"1.2.3.{i}", session_id=f"sid_{i}",
                section="marketing", subsection="product",
                page="/product.html", event="visit", ts=now - i,
                ua_browser="Chrome 121", ua_os="macOS", ua_device="desktop",
            )
        # leave 事件带 dur
        st.insert_event(
            tmp_db, ip="1.2.3.0", session_id="sid_0",
            section="marketing", subsection="product",
            page="/product.html", event="leave", ts=now, dur_sec=120,
        )
        kpis = st.get_kpis(tmp_db, since_ts=now - 100, online_window_sec=300)
        assert kpis["pv"] == 3            # 只算 visit
        assert kpis["uv"] == 3            # 3 个不同 IP
        assert kpis["online"] >= 1        # 5 分钟内必有
        assert kpis["avg_dur_sec"] == 120 # 唯一 dur>0 的事件

    def test_section_dist(self, tmp_db):
        now = int(time.time())
        for sec, sub, n in [
            ("marketing", "product", 5),
            ("marketing", "video",   3),
            ("ocr",        "",       2),
            ("asr",        "",       1),
        ]:
            for i in range(n):
                st.insert_event(
                    tmp_db, ip=f"10.0.0.{i}", session_id=f"s_{sec}_{i}",
                    section=sec, subsection=sub,
                    page=f"/{sec}", event="visit", ts=now - i,
                )
        dist = st.get_section_dist(tmp_db, since_ts=now - 100)
        # 主板块 marketing 应该最多（5+3=8）
        assert dist["main"][0]["section"] == "marketing"
        assert dist["main"][0]["pv"] == 8
        # marketing 下钻：product > video
        assert dist["sub"][0]["subsection"] == "product"
        assert dist["sub"][0]["pv"] == 5

    def test_top_ips(self, tmp_db):
        now = int(time.time())
        # IP A: 5 次访问；IP B: 2 次
        for i in range(5):
            st.insert_event(
                tmp_db, ip="200.1.1.1", session_id="a", section="marketing",
                subsection="home", page="/", event="visit", ts=now - i,
                ip_city="北京", dur_sec=10,
            )
        for i in range(2):
            st.insert_event(
                tmp_db, ip="200.2.2.2", session_id="b", section="ocr",
                page="/ocr/", event="visit", ts=now - i,
                ip_city="上海",
            )
        top = st.get_top_ips(tmp_db, since_ts=now - 100, n=5)
        assert top[0]["ip"] == "200.1.1.1"
        assert top[0]["pv"] == 5
        assert top[0]["city"] == "北京"
        assert "marketing" in top[0]["sections"]

    def test_list_events_pagination(self, tmp_db):
        now = int(time.time())
        for i in range(25):
            st.insert_event(
                tmp_db, ip=f"172.16.0.{i}", session_id=f"s{i}",
                section="marketing", subsection="product",
                page=f"/product.html?i={i}", event="visit", ts=now - i,
            )
        page1 = st.list_events(tmp_db, since_ts=now - 100, page=1, size=10)
        assert page1["total"] == 25
        assert len(page1["rows"]) == 10
        page3 = st.list_events(tmp_db, since_ts=now - 100, page=3, size=10)
        assert len(page3["rows"]) == 5  # 25 - 20

    def test_list_events_filter_by_section(self, tmp_db):
        now = int(time.time())
        st.insert_event(tmp_db, ip="1.1.1.1", session_id="a", section="marketing",
                        subsection="product", page="/p", event="visit", ts=now)
        st.insert_event(tmp_db, ip="1.1.1.2", session_id="b", section="ocr",
                        subsection="", page="/ocr", event="visit", ts=now)
        # section=ocr 应只返回 1 条
        r = st.list_events(tmp_db, since_ts=now - 100, section="ocr")
        assert r["total"] == 1
        assert r["rows"][0]["section"] == "ocr"
        # section=product（subsection 也匹配）
        r2 = st.list_events(tmp_db, since_ts=now - 100, section="product")
        assert r2["total"] == 1
        assert r2["rows"][0]["subsection"] == "product"

    def test_footer_summary(self, tmp_db):
        now = int(time.time())
        for i in range(7):
            st.insert_event(
                tmp_db, ip=f"5.5.5.{i}", session_id=f"sid_{i}",
                section="marketing", subsection="home",
                page="/", event="visit", ts=now - i,
            )
        s = st.get_footer_summary(tmp_db, online_window_sec=300)
        assert s["today_pv"] == 7
        assert s["today_uv"] == 7
        assert s["total_pv"] == 7
        assert s["online"] >= 1


# ============================================================================ #
# 白名单（防 SQL/数据污染）
# ============================================================================ #
class TestWhitelist:
    def test_section_whitelist(self):
        assert "marketing" in ar._SECTIONS_OK
        assert "ocr" in ar._SECTIONS_OK
        assert "asr" in ar._SECTIONS_OK
        assert "raw" in ar._SECTIONS_OK
        assert "qc" in ar._SECTIONS_OK
        assert "unknown" in ar._SECTIONS_OK
        # 不在白名单里的（包括 SQL 注入尝试）
        assert "DROP TABLE" not in ar._SECTIONS_OK
        assert "<script>" not in ar._SECTIONS_OK

    def test_subsection_whitelist(self):
        for s in ("home", "product", "doctor", "script", "video",
                  "distribute", "audience", "admin", "index", ""):
            assert s in ar._SUBSECTIONS_OK


# ============================================================================ #
# GIF 像素响应
# ============================================================================ #
class TestPixel:
    def test_pixel_is_valid_gif(self):
        # GIF89a magic
        assert ar._PIXEL_GIF.startswith(b"GIF89a") or ar._PIXEL_GIF.startswith(b"GIF87a")
        # 1px 透明 GIF 大小约 43 字节
        assert 30 <= len(ar._PIXEL_GIF) <= 80


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
