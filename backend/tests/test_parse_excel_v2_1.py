"""选品 Agent v2.1 内部工具的单元测试（无外部依赖：纯 AST + 字符串）。

只覆盖可离线验证的三个关键部件：
1. extract_code_block：从 LLM 输出抠 ```python 代码块
2. lint_generated_code：AST 围栏（黑名单 import / eval / open 写文件）
3. validate_skus：SKU JSON 业务校验（合计行 / 数值占比 / 必填 name）

运行：
    cd backend
    python -m pytest tests/test_parse_excel_v2_1.py -v
"""
from __future__ import annotations

import textwrap

import pytest

from app.agents.parse_excel_v2_1 import (
    extract_code_block,
    lint_generated_code,
    validate_skus,
)


# ============================================================================ #
# extract_code_block
# ============================================================================ #
class TestExtractCodeBlock:
    def test_standard_python_block(self):
        text = textwrap.dedent("""
            这是 LLM 多嘴的废话
            ```python
            import json
            print("__SKU_JSON_BEGIN__")
            ```
            尾部废话
        """)
        code = extract_code_block(text)
        assert "import json" in code
        assert "废话" not in code

    def test_py_alias_block(self):
        # ```py 也算 python
        text = "```py\nimport pandas\n```"
        assert "import pandas" in extract_code_block(text)

    def test_no_marker_fallback(self):
        # 不带 ``` 标记时返回 strip 后全文（后续 lint/exec 会自然失败重试）
        text = "import json\nprint('x')"
        assert "import json" in extract_code_block(text)

    def test_empty(self):
        assert extract_code_block("") == ""
        assert extract_code_block(None) == ""  # type: ignore[arg-type]


# ============================================================================ #
# lint_generated_code
# ============================================================================ #
class TestLintGeneratedCode:
    def test_clean_pandas(self):
        src = textwrap.dedent("""
            import json
            import pandas as pd
            import numpy as np
            df = pd.read_excel("/home/user/uploads/sales.xlsx")
            print("ok")
        """)
        assert lint_generated_code(src) == []

    def test_banned_import_os(self):
        src = "import os\nprint(os.listdir('/'))"
        v = lint_generated_code(src)
        assert any("os" in x for x in v)

    def test_banned_import_subprocess(self):
        v = lint_generated_code("import subprocess\nsubprocess.run(['ls'])")
        assert any("subprocess" in x for x in v)

    def test_banned_from_requests(self):
        v = lint_generated_code("from requests import get\nget('http://x')")
        assert any("requests" in x for x in v)

    def test_banned_eval(self):
        v = lint_generated_code("import json\neval('1+1')")
        assert any("eval" in x for x in v)

    def test_banned_exec(self):
        v = lint_generated_code("exec('print(1)')")
        assert any("exec" in x for x in v)

    def test_banned___import__(self):
        v = lint_generated_code("__import__('os')")
        assert any("__import__" in x for x in v)

    def test_open_write_to_root_blocked(self):
        v = lint_generated_code("open('/etc/passwd', 'w').write('x')")
        assert any("/etc/passwd" in x for x in v)

    def test_open_write_to_tmp_allowed(self):
        # 写 /tmp 允许
        v = lint_generated_code("open('/tmp/out.json', 'w').write('x')")
        assert v == []

    def test_open_write_to_home_user_allowed(self):
        v = lint_generated_code("open('/home/user/out.json', 'w').write('x')")
        assert v == []

    def test_open_read_no_check(self):
        # 只读 open() 不限制路径（业务确实要读 /home/user/uploads/...）
        v = lint_generated_code("open('/etc/hostname').read()")
        assert v == []

    def test_keyword_pip_install(self):
        # 字符串关键词兜底（即使写在字符串里也拒）
        v = lint_generated_code("x = 'pip install pandas'")
        assert any("pip" in x.lower() for x in v)

    def test_keyword_os_system(self):
        v = lint_generated_code("os.system('ls')")  # 没 import 但走关键字兜底
        assert any("os.system" in x for x in v)

    def test_syntax_error(self):
        v = lint_generated_code("def foo(:\n  pass")
        assert any("语法错误" in x for x in v)


# ============================================================================ #
# validate_skus
# ============================================================================ #
class TestValidateSkus:
    def test_clean_payload(self):
        payload = {
            "summary": {"rows": 3},
            "skus": [
                {"name": "复方甘草口服液", "qty": 1280, "amt": 38400},
                {"name": "维生素C泡腾片",   "qty":  980, "amt": 14700},
                {"name": "板蓝根颗粒",      "qty":  650, "amt":  9750},
            ],
        }
        ok, reason = validate_skus(payload)
        assert ok, reason

    def test_not_dict(self):
        ok, reason = validate_skus("haha")  # type: ignore[arg-type]
        assert not ok and "dict" in reason

    def test_skus_not_list(self):
        ok, reason = validate_skus({"skus": "x"})
        assert not ok and "list" in reason

    def test_empty_skus(self):
        ok, reason = validate_skus({"skus": []})
        assert not ok and "为空" in reason

    def test_missing_name(self):
        payload = {"skus": [{"qty": 100}]}
        ok, reason = validate_skus(payload)
        assert not ok and "name" in reason

    def test_summary_row_not_filtered(self):
        payload = {"skus": [
            {"name": "合计", "qty": 9999, "amt": 99999},
            {"name": "维生素C", "qty": 100, "amt": 1500},
        ]}
        ok, reason = validate_skus(payload)
        assert not ok and ("合计" in reason or "小计" in reason or "Total" in reason)

    def test_total_row_english(self):
        payload = {"skus": [{"name": "TOTAL", "qty": 100, "amt": 1000}]}
        ok, reason = validate_skus(payload)
        assert not ok

    def test_qty_amt_low_numeric_ratio(self):
        # 4/5 行 qty 是字符串 → 数值占比 20% < 50% → 拒
        payload = {"skus": [
            {"name": "A", "qty": "未知"},
            {"name": "B", "qty": "未知"},
            {"name": "C", "qty": "未知"},
            {"name": "D", "qty": "未知"},
            {"name": "E", "qty": 100},
        ]}
        ok, reason = validate_skus(payload)
        assert not ok and ("占比" in reason or "qty" in reason or "amt" in reason)

    def test_only_qty_filled_passes(self):
        # 只有 qty 没 amt 也 OK（业务允许）
        payload = {"skus": [
            {"name": "A", "qty": 100},
            {"name": "B", "qty": 200},
            {"name": "C", "qty": 300},
        ]}
        ok, reason = validate_skus(payload)
        assert ok, reason


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
