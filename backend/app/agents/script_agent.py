"""话术 Agent：融合"产品受控口径 + 医生口播风格 + 目标人群 + 话术结构/目标 + 受众侧重"
组装 Prompt → 调 LLM → 输出合规话术 narration。

输入是三个上游 Agent 的产物（与 prototype/script.html 的 localStorage 契约一致），
加上"受众侧重" key（women/mom/worker 或自定义）。

合规策略：本模块只在 system 注入合规口径约束 + 在终态做"硬约束"清洗（违禁词替换）。
LLM 已经被强约束，但模型不可控，所以我们保留一道"事后清洗"作为兜底，避免泄漏到下游。
"""
from __future__ import annotations

import logging
import re
from typing import Generator, Optional

from ..config import Settings, get_settings
from ..llm import stream_chat

log = logging.getLogger("video-agent.script_agent")


# --------------------------------------------------------------------------- #
# 目标人群（直接取自上游 ③目标人群 Agent 的产物；已停用"话术受众侧重"画像）
# --------------------------------------------------------------------------- #
def _audience_block(audience: Optional[dict]) -> dict:
    """直接用上游回传的目标人群字段（mainAge / topInterest / tier / reach …）构造画像。"""
    audience = audience or {}
    name = audience.get("name") or audience.get("mainAge") or "目标人群"
    if audience.get("desc"):
        desc = audience["desc"]
    else:
        parts = []
        if audience.get("mainAge"):
            parts.append(audience["mainAge"])
        if audience.get("topInterest"):
            parts.append(f"核心兴趣 {audience['topInterest']}")
        if audience.get("tier"):
            parts.append(f"{audience['tier']}档")
        if audience.get("reach"):
            parts.append(f"可触达 {audience['reach']}")
        desc = " · ".join(parts)
    return {
        "name": name,
        "desc": desc,
        "tone": "口语化、亲和、科普向；不卖弄专业术语",
    }


# --------------------------------------------------------------------------- #
# 合规口径（按产品功效域动态映射；与 prototype/script.html 的 PRODUCT_COMPLIANCE_PROFILES 保持同步）
# --------------------------------------------------------------------------- #
# key 必须与 product.html 里 PRODUCT_PROFILE.domain 完全一致（中文含空格）
COMPLIANCE_BY_DOMAIN: dict[str, dict] = {
    "肠道 / 消化健康":   {"claim": "有助于维持肠道菌群平衡", "noun": "肠道"},
    "眼健康 / 视疲劳":   {"claim": "有助于缓解视疲劳",       "noun": "眼睛/视疲劳"},
    "口腔 / 牙周护理":   {"claim": "帮助清新口气、保持口腔清洁", "noun": "口腔"},
    "皮肤 / 美容养颜":   {"claim": "有助于皮肤健康",         "noun": "皮肤"},
    "免疫 / 日常补充":   {"claim": "有助于增强免疫力",       "noun": "身体免疫"},
    "骨骼 / 营养补充":   {"claim": "有助于增加骨密度",       "noun": "骨骼"},
    "增肌 / 运动营养":   {"claim": "帮助补充蛋白质，搭配运动促进健康", "noun": "体能"},
    "肠道 / 轻断食":     {"claim": "有助于润肠通便",         "noun": "肠道"},
}
_DEFAULT_COMPLIANCE = {"claim": "仅可使用蓝帽子目录内允许的功能声称", "noun": "身体"}


def compliance_for_product(product: dict) -> dict:
    """按 product.domain 命中合规口径；未命中回退到通用模板。"""
    dom = (product or {}).get("domain") or ""
    return COMPLIANCE_BY_DOMAIN.get(dom, _DEFAULT_COMPLIANCE)


def build_compliance_rules(product: dict) -> str:
    """生成 system 注入的合规红线段落（含产品域专属允许口径）。"""
    cp = compliance_for_product(product)
    return f"""\
合规红线（《广告法》§9/§16/§18 + 保健食品功能声称目录）：
1. 严禁出现疗效用语：治疗、治愈、根治、有效、缓解症状、改善 XX 病（针对 {cp['noun']} 等任何器官/疾病均不可宣称）。
2. 严禁医生代言式表述：『我作为医生强烈推荐』『医生亲测』。
3. 严禁绝对化用语：最、第一、唯一、彻底、100%、永远。
4. 严禁时限承诺：『N 天见效』『一周改善』。
5. 仅可使用蓝帽子允许的功能声称：『{cp['claim']}』。
6. 不得诱导焦虑或暗示疾病发病率。
"""



VIOLATION_PATTERNS: list[tuple[str, str]] = [
    # (违禁正则, 合规替换/留空表示直接删除)
    (r"治愈|根治|治疗", "调理"),
    (r"我作为医生(强烈)?推荐", "建议大家可以了解一下"),
    (r"医生亲测|临床验证", "成分透明"),
    (r"最好|第一|唯一|彻底|100%", "更适合"),
    (r"\d+\s*天就?(见效|根治|彻底)|\d+\s*周(就?)(见效|改善)", "日常持续养护"),
]


def sanitize(text: str) -> tuple[str, list[str]]:
    """硬约束清洗：把命中违禁词的片段替换/删除，返回(清洗后文本, 违规项列表)。"""
    hits: list[str] = []
    cleaned = text
    for pat, repl in VIOLATION_PATTERNS:
        matches = re.findall(pat, cleaned)
        if matches:
            hits.extend([m if isinstance(m, str) else "".join(m) for m in matches])
            cleaned = re.sub(pat, repl, cleaned)
    return cleaned, hits


# --------------------------------------------------------------------------- #
# Prompt 组装
# --------------------------------------------------------------------------- #
def build_messages(
    *,
    product: dict,
    doctor: dict,
    audience: dict,
    structure: str = "痛点→科普→产品自然带入→行动引导",
    target_duration_sec: int = 21,
) -> list[dict]:
    """根据产品 / 医生 / 上游目标人群 / 结构 / 时长构造 messages。"""
    aud = _audience_block(audience)
    cp = compliance_for_product(product)
    compliance_rules = build_compliance_rules(product)

    # 字数估算：5 字/秒（中文 TTS 常用速度），留 ±5% 缓冲
    target_chars = round(target_duration_sec * 5)
    lo, hi = round(target_chars * 0.92), round(target_chars * 1.05)

    system = f"""\
你是资深医疗健康内容编导，专门为短视频口播撰写合规话术。

# 角色与风格
- 出镜医生：{doctor.get('name','主任医师')}（{doctor.get('dept','—')}，{doctor.get('fans','—')}）
- 风格指纹：专业亲和、科普向、设问开场、收尾固定使用『科学养护，从日常做起』。
- 不卖弄专业术语，必要时用类比解释。

# {compliance_rules}

# 输出硬约束
1. 必须是【完整一段话】，不分镜号、不带 emoji、不带任何 Markdown 标记。
2. 总字数 {lo}-{hi} 个汉字（口播节奏 {target_duration_sec}±2 秒）。
3. 第二人称为主（『你/朋友们』），口语化短句。
4. 严格遵循结构：{structure}。
5. 收尾必须自然出现『科学养护，从日常做起。』
6. 不出现任何被合规红线列出的禁用表达。
7. 只输出话术正文，不要任何额外说明、引号、前缀（如『话术：』）。
"""

    user = f"""\
请基于以下输入产出一段合规口播话术：

【产品】{product.get('name','—')}（{product.get('category','—')}）
- 功效域：{product.get('domain','—')}
- 适用人群：{product.get('applicable','—')}
- 合规口径：仅可宣称『{cp['claim']}』
- 诉求方向：{product.get('appeal','科普向')}

【医生】{doctor.get('name','—')}
- 类型：{doctor.get('type','—')}
- 受众基底：{doctor.get('audience','—')}

【目标人群】{aud['name']}
- 画像：{aud['desc']}
- 语气提示：{aud['tone']}
- 投放平台：{audience.get('platforms','抖音 + 视频号')}

【结构】{structure}
【目标时长】{target_duration_sec} 秒（约 {lo}-{hi} 个汉字）
"""

    return [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]


# --------------------------------------------------------------------------- #
# 对外入口：流式 / 一次性
# --------------------------------------------------------------------------- #
def stream_generate(
    *,
    product: dict,
    doctor: dict,
    audience: dict,
    structure: str = "痛点→科普→产品自然带入→行动引导",
    target_duration_sec: int = 21,
    settings: Optional[Settings] = None,
) -> Generator[str, None, None]:
    """流式生成话术（yield token 增量）。调用方负责拼接 + 事后清洗。"""
    s = settings or get_settings()
    msgs = build_messages(
        product=product, doctor=doctor, audience=audience,
        structure=structure, target_duration_sec=target_duration_sec,
    )
    yield from stream_chat(msgs, settings=s)


def generate(
    *,
    product: dict,
    doctor: dict,
    audience: dict,
    structure: str = "痛点→科普→产品自然带入→行动引导",
    target_duration_sec: int = 21,
    settings: Optional[Settings] = None,
) -> dict:
    """一次性生成：返回 {text, raw_text, violations, audience_name, char_count}。"""
    parts = list(stream_generate(
        product=product, doctor=doctor, audience=audience,
        structure=structure, target_duration_sec=target_duration_sec,
        settings=settings,
    ))
    raw = "".join(parts).strip()
    cleaned, hits = sanitize(raw)
    aud_name = audience.get("name") or audience.get("mainAge") or "目标受众"
    return {
        "text": cleaned,
        "raw_text": raw,
        "violations": hits,
        "audience_name": aud_name,
        "char_count": len(cleaned),
    }
