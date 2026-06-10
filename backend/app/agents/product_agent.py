"""选品 Agent v2.0 —— 基于腾讯云 Agent Runtime 沙箱的"用户上传销量表→主推 Top N"。

业务编排（同进程 LangGraph 节点）：

    upload(用户上传 Excel)
       │
       ▼
    ① 需求理解（v2.0 MVP=Mock prompt）：从 brief 文本 + Excel 表头抽出业务关键词
       │
       ▼
    ② 数据解析（AGR 代码沙箱）：pandas 读 Excel → 标准化为 SKU 销量 DataFrame → 落 JSON
       │
       ▼
    ③ 行情抓取（v2.0 MVP=Mock）：返回每个 SKU 的"行情趋势分"（后续接药监局/百度指数）
       │
       ▼
    ④ 候选打分（AGR 代码沙箱）：在沙箱里跑 pandas 加权 → 选 Top N
       │
       ▼
    ⑤ 结论汇总（纯 LLM）：给 Top1 一句话推荐理由 + 整理成 ProductOutput

输出契约（v2.0 → 下游选医生 Agent）：
  - `ProductOutput.candidates[i]` 字段名与 prototype/product.html v1.0 的 PRODUCT_PROFILE 对齐
  - 前端把 Top1 写入 localStorage.sv_selected_product，下游 doctor.html 一行不改读取

设计要点：
  - 沙箱按 job 起、跑完即销毁（with 上下文管理器）；不依赖沙箱内文件做跨 job 持久化
  - 用户上传 Excel 走 backend/.cache/product_jobs/{job_id}/uploads/，沙箱内通过 sb.files.write 注入
  - 沙箱内**禁止**直接 cat/print 用户 Excel 完整内容（避免 LLM 提示词被注入），只走 schema 摘要
"""
from __future__ import annotations

import json
import logging
import textwrap
from pathlib import Path
from typing import Callable, Optional

from ..config import get_settings
from ..llm import complete_chat
from ..sandbox_executor import code_sandbox
from ..schemas import (
    ProductBriefRequest,
    ProductCandidate,
    ProductJob,
    ProductOutput,
)

log = logging.getLogger("video-agent.product_agent")

# 任务产物目录（与 v1.1 视频链路的 .cache/jobs 平级，独立命名空间）
_PRODUCT_JOBS_DIR = Path(__file__).resolve().parents[2] / ".cache" / "product_jobs"

# Emit 回调：每个步骤完成后，向前端推一次进度
EmitFn = Callable[[ProductJob, str], None]


# ----------------------------------------------------------------------------- #
# ① 需求理解（v2.0 MVP 简化版：Mock prompt，仅从 brief 文本抽关键词）
# ----------------------------------------------------------------------------- #
def _understand_brief(brief: ProductBriefRequest) -> dict:
    """v2.0 MVP：不走 LLM，只做关键词归一化。后续可换成 LLM 抽 BriefSpec(json)。"""
    txt = (brief.brief or "").lower()
    return {
        "raw": brief.brief,
        "structure_hint": brief.structure_hint,
        "tags": [t for t in ["儿童", "孕妇", "中老年", "白领", "学生",
                             "肠道", "免疫", "眼睛", "骨骼", "皮肤"]
                 if t.lower() in txt or t in brief.brief],
        "upload_name": brief.upload_name,
    }


# ----------------------------------------------------------------------------- #
# ② 数据解析（AGR 代码沙箱里跑 pandas）
# ----------------------------------------------------------------------------- #
# 沙箱内执行的 pandas runner —— 这段代码作为字符串发到沙箱里跑，不在主进程执行
# 用 __UPLOAD_PATH__ 占位符注入用户上传文件的实际沙箱路径（含真实扩展名），
# 避免硬写 sales.xlsx 导致 _read 按错扩展名解析。
_PARSE_RUNNER = textwrap.dedent("""
    import json, os
    import pandas as pd
    import numpy as np

    UPLOAD = __UPLOAD_PATH__  # 由主进程注入实际路径字符串

    # 兼容 xlsx / xls / csv（按扩展名）
    def _read(p):
        p_low = p.lower()
        if p_low.endswith(".csv"):
            return pd.read_csv(p)
        return pd.read_excel(p)

    df = _read(UPLOAD)

    # 列名 fuzzy match：找到"产品/SKU"、"销量/销售量"、"金额/销售额"等列
    def _find(cands):
        for c in df.columns:
            for k in cands:
                if k in str(c):
                    return c
        return None

    col_name  = _find(["产品", "品名", "SKU", "商品", "名称", "药品", "产品名", "商品名"])
    col_qty   = _find(["销量", "销售量", "数量", "件数", "盒数", "支数", "瓶数", "qty", "sales", "volume"])
    col_amt   = _find(["金额", "销售额", "营收", "收入", "GMV", "成交", "总额", "amount", "revenue"])
    col_dept  = _find(["科室", "类别", "分类", "品类", "类目", "department"])

    summary = {
        "rows": int(len(df)),
        "cols": [str(c) for c in df.columns],
        "mapped": {"name": col_name, "qty": col_qty, "amt": col_amt, "dept": col_dept},
    }

    if not col_name:
        # 列名识别失败，把首列当 name 兜底
        col_name = df.columns[0]

    # 聚合到 SKU 维度（同名 SKU 求和）
    agg = {}
    if col_qty:
        agg[col_qty] = "sum"
    if col_amt:
        agg[col_amt] = "sum"
    if agg:
        g = df.groupby(col_name, as_index=False).agg(agg)
    else:
        # 没销量/金额列，按出现次数计数
        g = df[col_name].value_counts().reset_index()
        g.columns = [col_name, "_count"]
        col_qty = "_count"

    # 标准化字段名 → name / qty / amt / dept
    g = g.rename(columns={col_name: "name"})
    if col_qty: g = g.rename(columns={col_qty: "qty"})
    if col_amt: g = g.rename(columns={col_amt: "amt"})

    # 若没有 amt 列，用 qty 兜底
    if "amt" not in g.columns and "qty" in g.columns:
        g["amt"] = g["qty"]

    skus = g.head(50).to_dict(orient="records")  # 截断防爆
    summary["sku_count"] = int(len(g))

    # 一次性回吐：用 print(json.dumps) 让上层用 stdout 解析
    print("__SKU_JSON_BEGIN__")
    print(json.dumps({"summary": summary, "skus": skus}, ensure_ascii=False, default=str))
    print("__SKU_JSON_END__")
""").strip()


def _parse_excel_in_sandbox(upload_path: Path, sandbox_ids: list[str]) -> dict:
    """打开沙箱 → 投递文件 → 跑 pandas runner → 解析 stdout → 销毁。"""
    if not upload_path.is_file():
        raise FileNotFoundError(f"上传文件不存在：{upload_path}")

    # 保留真实扩展名（避免 csv 被当 xlsx 解析）
    suffix = upload_path.suffix.lower() or ".xlsx"
    sandbox_path = f"/home/user/uploads/sales{suffix}"

    with code_sandbox() as sb:
        sandbox_ids.append(sb.sandbox_id)
        # 把本地文件写到沙箱
        with upload_path.open("rb") as f:
            sb.files.write(sandbox_path, f.read())

        # 装 pandas + openpyxl（公网模式拉包）
        pip_r = sb.run_code(
            "import subprocess, sys; "
            "subprocess.run([sys.executable, '-m', 'pip', 'install', '-q', "
            "'pandas', 'openpyxl'], check=True); print('installed')"
        )
        if pip_r.error:
            raise RuntimeError(f"沙箱 pip install 失败：{pip_r.error}")

        # 把真实路径以 Python 字面量注入到 runner 里
        runner = _PARSE_RUNNER.replace("__UPLOAD_PATH__", repr(sandbox_path))
        r = sb.run_code(runner, timeout=get_settings().agr_code_run_timeout_sec)

    if r.error:
        raise RuntimeError(f"沙箱 runner 异常：{r.error}")
    stdout = "\n".join(r.logs.stdout or [])
    stderr = "\n".join(r.logs.stderr or [])
    if "__SKU_JSON_BEGIN__" not in stdout or "__SKU_JSON_END__" not in stdout:
        raise RuntimeError(
            f"沙箱未返回标准 SKU JSON：stdout={stdout[:400]!r} stderr={stderr[:400]!r}"
        )
    payload = stdout.split("__SKU_JSON_BEGIN__", 1)[1].split("__SKU_JSON_END__", 1)[0].strip()
    return json.loads(payload)


# ----------------------------------------------------------------------------- #
# ③ 行情抓取（v2.0 MVP=Mock；后续接药监局/百度指数）
# ----------------------------------------------------------------------------- #
def _mock_market_signals(skus: list[dict]) -> dict[str, float]:
    """v2.0 MVP：用 SKU 名字哈希出一个稳定的 0.5~1.0 趋势分。
    后续要切真实抓取时，换成 browser_sandbox + 白名单域名。
    """
    import hashlib
    out = {}
    for s in skus:
        h = int(hashlib.md5(str(s.get("name", "")).encode("utf-8")).hexdigest(), 16)
        out[str(s.get("name"))] = round(0.5 + (h % 500) / 1000, 3)  # [0.5, 1.0)
    return out


# ----------------------------------------------------------------------------- #
# ④ 候选打分（AGR 代码沙箱里跑 pandas）
# ----------------------------------------------------------------------------- #
_SCORE_RUNNER = textwrap.dedent("""
    import json
    import pandas as pd

    skus = json.loads(__SKUS_JSON__)
    trends = json.loads(__TRENDS_JSON__)

    df = pd.DataFrame(skus)
    # 调试可观测：打印实际拿到的列，下次踩坑能立刻定位
    print("__DEBUG__cols:", list(df.columns))

    # 归一化 qty / amt（避免量纲拉爆）。
    # 注意：df.get(col, default) 当列缺失时返回 default 原值（int 0），不是 Series；
    # 因此这里显式分支取 Series，避免 'int' object has no attribute 'fillna'。
    def _norm(s):
        s = pd.to_numeric(s, errors="coerce").fillna(0)
        if s.max() == s.min():
            return s * 0 + 0.5
        return (s - s.min()) / (s.max() - s.min())

    if "amt" in df.columns:
        sales_src = df["amt"]
    elif "qty" in df.columns:
        sales_src = df["qty"]
    else:
        # 完全没有量化列：销售分全部 0.5（纯靠 trend_score 区分）
        sales_src = pd.Series([0.5] * len(df))

    sales = _norm(sales_src)
    df["sales_score"] = sales.round(3)
    if "name" in df.columns:
        df["trend_score"] = df["name"].map(trends).fillna(0.5).round(3)
    else:
        df["trend_score"] = 0.5
    # 加权：销量 0.7 + 趋势 0.3
    df["final_score"] = (df["sales_score"] * 0.7 + df["trend_score"] * 0.3).round(3)

    top = df.sort_values("final_score", ascending=False).head(5)
    print("__TOP_JSON_BEGIN__")
    print(json.dumps(top.to_dict(orient="records"), ensure_ascii=False, default=str))
    print("__TOP_JSON_END__")
""").strip()


def _score_in_sandbox(skus: list[dict], trends: dict[str, float], sandbox_ids: list[str]) -> list[dict]:
    # 用 repr() 把 JSON 字符串变成合法 Python 字符串字面量（自动处理引号转义）
    skus_lit = repr(json.dumps(skus, ensure_ascii=False))
    trends_lit = repr(json.dumps(trends, ensure_ascii=False))
    runner = (_SCORE_RUNNER
              .replace("__SKUS_JSON__", skus_lit)
              .replace("__TRENDS_JSON__", trends_lit))
    with code_sandbox() as sb:
        sandbox_ids.append(sb.sandbox_id)
        pip_r = sb.run_code(
            "import subprocess, sys; "
            "subprocess.run([sys.executable, '-m', 'pip', 'install', '-q', 'pandas'], check=True); print('installed')"
        )
        if pip_r.error:
            raise RuntimeError(f"沙箱 pip install 失败：{pip_r.error}")
        r = sb.run_code(runner, timeout=get_settings().agr_code_run_timeout_sec)

    if r.error:
        raise RuntimeError(f"沙箱 score runner 异常：{r.error}")
    stdout = "\n".join(r.logs.stdout or [])
    stderr = "\n".join(r.logs.stderr or [])
    if "__TOP_JSON_BEGIN__" not in stdout:
        raise RuntimeError(
            f"沙箱未返回 TOP JSON：stdout={stdout[:400]!r} stderr={stderr[:400]!r}"
        )
    payload = stdout.split("__TOP_JSON_BEGIN__", 1)[1].split("__TOP_JSON_END__", 1)[0].strip()
    return json.loads(payload)


# ----------------------------------------------------------------------------- #
# ⑤ LLM 汇总（给 Top1 一句话推荐理由 + 字段补全）
# ----------------------------------------------------------------------------- #
_CANDIDATE_PROMPT = textwrap.dedent("""
    你是医药营销选品助手。基于销量与行情数据给出的 Top {n} 候选品，请为**每个候选品**输出一行 JSON，
    字段严格如下，不要多不要少：

      {{"name": "...", "emoji": "<1个emoji>", "category": "保健食品/普通食品/日化/运动营养/其它",
        "dept": "推荐挂的科室，例如 '消化内科 / 营养科'", "domain": "健康域，例如 '肠道 / 消化健康'",
        "applicable": "适用人群一句话", "risk": "合规风险一句话",
        "appeal": "短视频诉求方向，例如 '专业科普向'",
        "chips": ["2-3个关键词标签"],
        "rationale": "为何排这位次的一句话理由（必须引用销量分/行情分）"}}

    Top 候选数据（已经按 final_score 倒序）：
    {tops_json}

    每行一个 JSON，**只输出 JSON 行，不要其它任何文字**。
""").strip()


def _llm_dress_candidates(tops: list[dict]) -> list[dict]:
    """让 LLM 补全 emoji / category / dept / risk / appeal / chips / rationale。
    LLM 不可用时（无 KEY 或调用失败）用 Mock 字典兜底，不阻塞流程。
    """
    s = get_settings()
    if not s.llm_api_key:
        return [_mock_dress(t, i) for i, t in enumerate(tops)]

    prompt = _CANDIDATE_PROMPT.format(n=len(tops),
                                      tops_json=json.dumps(tops, ensure_ascii=False, indent=2))
    try:
        text = complete_chat(
            [
                {"role": "system", "content": "你是医药营销选品助手，严格输出 JSON 行。"},
                {"role": "user", "content": prompt},
            ],
            settings=s, temperature=0.3, max_tokens=900,
        )
    except Exception as e:  # noqa: BLE001
        log.warning("LLM dress 失败，走 Mock 兜底：%s", e)
        return [_mock_dress(t, i) for i, t in enumerate(tops)]

    dressed = []
    for line in (text or "").splitlines():
        line = line.strip().lstrip(",")
        if not line.startswith("{") or not line.endswith("}"):
            continue
        try:
            dressed.append(json.loads(line))
        except json.JSONDecodeError:
            continue

    # 数量对不上时用 Mock 补
    if len(dressed) < len(tops):
        for i in range(len(dressed), len(tops)):
            dressed.append(_mock_dress(tops[i], i))
    return dressed[:len(tops)]


def _mock_dress(top: dict, idx: int) -> dict:
    """LLM 不可用时的兜底字段。保持下游契约不破。"""
    name = str(top.get("name") or f"候选品{idx + 1}")
    return {
        "name": name,
        "emoji": "🛒",
        "category": "保健食品",
        "dept": "营养科 / 全科",
        "domain": "日常补充 / 营养",
        "applicable": "目标人群（待补全）",
        "risk": "保健品·疗效宣称受限",
        "appeal": "日常科普向",
        "chips": ["日常补充", "营养"],
        "rationale": (
            f"销量分 {top.get('sales_score', 0)} / 行情分 {top.get('trend_score', 0)} 综合排第 {idx + 1}。"
        ),
    }


# ----------------------------------------------------------------------------- #
# 整体编排：在 worker 线程里同步跑（参考 v1.1 video graph 思路）
# ----------------------------------------------------------------------------- #
def run_product_pipeline(job: ProductJob, emit: EmitFn) -> None:
    """同步跑完整条 v2.0 流水线。worker 用 asyncio.to_thread 包装调用。"""
    from ..schemas import JobStatus  # 延迟导入避免循环

    job_dir = _PRODUCT_JOBS_DIR / job.id
    job_dir.mkdir(parents=True, exist_ok=True)

    try:
        # ① 需求理解
        job.status = JobStatus.STORYBOARD  # 复用枚举：解析阶段
        job.progress = 8
        job.message = "理解选品需求（解析关键词）…"
        emit(job, "st1")
        spec = _understand_brief(job.brief)
        (job_dir / "brief.json").write_text(
            json.dumps(spec, ensure_ascii=False, indent=2), encoding="utf-8"
        )

        # ② 数据解析（沙箱）
        job.status = JobStatus.SUBMITTING
        job.progress = 20
        job.message = "沙箱解析销量表（pandas）…"
        emit(job, "st2")
        upload_path = Path(job.brief.upload_path)
        if not upload_path.is_absolute():
            upload_path = Path(__file__).resolve().parents[2] / job.brief.upload_path
        parsed = _parse_excel_in_sandbox(upload_path, job.sandbox_ids)
        (job_dir / "skus.json").write_text(
            json.dumps(parsed, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        skus = parsed.get("skus", [])
        if not skus:
            raise RuntimeError("销量表解析为空，请检查文件内容")

        # ③ 行情抓取（Mock）
        job.status = JobStatus.GENERATING
        job.progress = 45
        job.message = "行情抓取（v2.0 MVP=Mock）…"
        emit(job, "st3")
        trends = _mock_market_signals(skus)

        # ④ 沙箱打分
        job.progress = 60
        job.message = "沙箱打分（pandas 加权 Top 5）…"
        emit(job, "st4")
        tops = _score_in_sandbox(skus, trends, job.sandbox_ids)

        # ⑤ LLM 汇总
        job.status = JobStatus.COMPLIANCE  # 复用：汇总阶段
        job.progress = 82
        job.message = "LLM 汇总推荐理由…"
        emit(job, "st5")
        dressed = _llm_dress_candidates(tops)

        # 组装 ProductCandidate
        cands: list[ProductCandidate] = []
        for i, (raw, dr) in enumerate(zip(tops, dressed)):
            cands.append(ProductCandidate(
                id=f"v2_{job.id}_{i}",
                emoji=str(dr.get("emoji") or "🛒"),
                name=str(dr.get("name") or raw.get("name", "")),
                category=str(dr.get("category") or "保健食品"),
                dept=str(dr.get("dept") or ""),
                domain=str(dr.get("domain") or ""),
                applicable=str(dr.get("applicable") or ""),
                risk=str(dr.get("risk") or "保健品·疗效宣称受限"),
                appeal=str(dr.get("appeal") or "专业科普向"),
                chips=list(dr.get("chips") or []),
                sales_score=float(raw.get("sales_score") or 0),
                trend_score=float(raw.get("trend_score") or 0),
                final_score=float(raw.get("final_score") or 0),
                rationale=str(dr.get("rationale") or ""),
            ))

        job.output = ProductOutput(
            candidates=cands,
            top1_id=cands[0].id if cands else "",
            data_summary=parsed.get("summary", {}),
            strat="agent_v2",
        )
        job.status = JobStatus.DONE
        job.progress = 100
        job.message = f"选品完成，Top {len(cands)} 已生成"
        emit(job, "done")

    except Exception as e:  # noqa: BLE001
        log.exception("product agent 失败 job=%s", job.id)
        job.status = JobStatus.FAILED
        job.error = str(e)
        job.message = f"失败：{e}"
        emit(job, "failed")
