"""
textutil.py — 纯文本 / 数值工具（无状态，可直接单测）

从 orchestrator.py 抽出的确定性工具函数：
  - `shorten`                    事件展示用的文本截断
  - `estimate_cost_rmb`          按 token 遥测估算整场费用（人民币）
  - `text_similarity`            字符级相似度（收敛检测在嵌入不可用时的降级路径）
  - `extract_argument_sentences` 从发言原文抽取关键论据句（对手定向反驳用）

这些函数不依赖编排器状态，独立成模块后可单独测试与复用（导出、评估脚本等）。
"""
from __future__ import annotations

import difflib
import json
import re

from muti_llm_agent.config import settings

# 论据句启发式关键词（§5.4）
ARG_MARKER_WORDS = ("首先", "其次", "再次", "最后", "第一", "第二", "第三", "第四",
                    "其一", "其二", "其三", "一方面", "另一方面", "一是", "二是", "三是")
ARG_KEY_WORDS = ("指出", "认为", "表明", "证明", "说明", "因为", "因此", "所以",
                 "优势", "劣势", "缺陷", "问题", "局限", "不可替代", "证据", "数据",
                 "研究", "导致", "意味着", "必须", "应当", "应该", "然而", "反而")


def shorten(text, limit: int = 120) -> str:
    """截断工具调用摘要到 limit 字符（用于前端 tool_call_end 展示，避免刷屏）。"""
    if text is None:
        return ""
    if not isinstance(text, str):
        try:
            text = json.dumps(text, ensure_ascii=False)
        except Exception:
            text = str(text)
    text = text.strip()
    if len(text) <= limit:
        return text
    return text[:limit] + "…"


def estimate_cost_rmb(total_tokens, output_tokens) -> float:
    """按配置单价估算整场讨论费用（人民币 ¥）。

    输入 token = 累计用量 - 输出 token；输入/输出分别按每百万单价计价。
    仅用于展示估算，不参与任何约束（与预算系统移除后的只读遥测一致）。
    """
    total = max(0, int(total_tokens or 0))
    output = max(0, int(output_tokens or 0))
    input_t = max(0, total - output)
    return (input_t / 1e6 * settings.COST_INPUT_PER_1M
            + output / 1e6 * settings.COST_OUTPUT_PER_1M)


def text_similarity(a: str, b: str) -> float:
    """字符级文本相似度（difflib），用于嵌入模型不可用时的收敛检测降级（P0-3）。

    去除全部空白后比较，避免换行/空格等排版差异干扰；返回 [0, 1]。
    """
    a = "".join((a or "").split())
    b = "".join((b or "").split())
    if not a or not b:
        return 0.0
    return difflib.SequenceMatcher(None, a, b).ratio()


def extract_argument_sentences(text: str, max_chars: int) -> str:
    """从发言原文中抽取关键论据句（截断到 max_chars 字）。

    按句号/感叹/问号/分号/换行切句后，只保留带序号标记（首先/第一/1.…）
    或论据性关键词（指出/表明/因为…）的句子，优先取标记多者；
    原文无任何标记时回退最长句，避免信息完全丢失。超出 max_chars 时整体截断。
    """
    if not text or max_chars <= 0:
        return ""
    sentences = [s.strip() for s in re.split(r"[。！？!?;\n]", text) if s.strip()]
    if not sentences:
        return ""

    def _score(s: str) -> int:
        sc = 0
        if any(w in s for w in ARG_MARKER_WORDS):
            sc += 3
        sc += sum(1 for w in ARG_KEY_WORDS if w in s)
        return sc

    scored = [(_score(s), -len(s), s) for s in sentences if _score(s) > 0]
    if not scored:
        # 无任何标记/关键词：回退取最长句，保留最大信息量
        scored = [(1, -len(s), s) for s in sentences]
    scored.sort(reverse=True)
    kept: list[str] = []
    total = 0
    for _, _, s in scored:
        if total + len(s) > max_chars:
            if not kept:
                kept.append(s[:max_chars])
            break
        kept.append(s)
        total += len(s)
    return "；".join(kept)[:max_chars]
