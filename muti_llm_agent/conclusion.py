"""
conclusion.py — 结构化结论（P0-1 四段式最终报告）

从 orchestrator.py 抽出的「报告规整层」：

  - `split_sections`            按一/二级 Markdown 标题切分
  - `fallback_core_conclusion`  核心结论的确定性兜底
  - `fallback_positions`        各方立场摘要的确定性兜底（基于发言记录）
  - `normalize_conclusion`     把主持人 LLM 输出规整为固定四段式，并返回结构化结论
  - `fallback_summary`         完全确定性汇总（不调 LLM，系统兜底路径）

关键约束（保持不变）：「裁判裁决」段一律用权威 `Judgment` 数据重写，
防止主持人转述失真或编造；其余段落缺失时用确定性数据回填。
"""
from __future__ import annotations

import re

from muti_llm_agent.protocol import Judgment
from muti_llm_agent.scoring import judge_ruling_text

CORE_TITLES = ("核心结论", "结论", "总结", "摘要")
POSITION_TITLES = ("各方立场", "立场与论点", "论点摘要", "观点摘要", "各方论点")
PENDING_TITLES = ("分歧与待办", "分歧", "待办")

EMPTY_PENDING = "（无明确列出的分歧或待办事项）"
FALLBACK_NOTE = ("> 注：本报告由系统按发言记录确定性生成（未调用主持人 LLM 汇总），"
                 "作为公平性治理下的兜底路径保留。\n\n")


def split_sections(text: str) -> dict:
    """按一级/二级 Markdown 标题把文本切分为 {标题: 正文}。"""
    sections: dict[str, list] = {}
    cur = None
    for line in (text or "").splitlines():
        m = re.match(r"^#{1,2}\s+(.+)$", line.strip())
        if m:
            cur = m.group(1).strip()
            sections.setdefault(cur, [])
        elif cur is not None:
            sections[cur].append(line)
    return {k: "\n".join(v).strip() for k, v in sections.items()}


def fallback_core_conclusion(judgment: Judgment | None) -> str:
    """确定性兜底：核心结论（优先引用裁判裁决）。"""
    if judgment:
        winner = "平局" if judgment.winner == "DRAW" else f"「{judgment.winner}」"
        base = f"整场讨论已结束，裁判裁决胜方为 {winner}。"
        if judgment.critique:
            base += f"\n裁判评语：{judgment.critique}"
        return base
    return "整场讨论已完成，各方完成多轮发言与交锋；未配置独立裁判，未产生裁决。"


def fallback_positions(transcript: list) -> str:
    """确定性兜底：各方立场与论点摘要（基于发言记录截取）。"""
    lines = []
    for e in transcript:
        if e.get("type") == "speech":
            text = e.get("text", "")
            snippet = text[:120] + ("…" if len(text) > 120 else "")
            lines.append(f"- **{e['speaker']}**（第{e['round']}轮）：{snippet}")
    return "\n".join(lines) or "（无发言记录）"


def _pick(sections: dict, *titles: str) -> str:
    """按候选标题（含包含匹配）取第一段非空正文。"""
    for t in titles:
        for k, v in sections.items():
            if t in k:
                return v
    return ""


def normalize_conclusion(text: str, judgment: Judgment | None,
                         transcript: list) -> tuple:
    """把主持人 LLM 输出规整为固定四段式最终报告。

    返回 `(report_markdown, conclusion_dict)`；`conclusion_dict` 供前端结构化展示。
    """
    parts = split_sections(text)
    core = _pick(parts, *CORE_TITLES) or fallback_core_conclusion(judgment)
    positions = _pick(parts, *POSITION_TITLES) or fallback_positions(transcript)
    ruling = judge_ruling_text(judgment)
    pending = _pick(parts, *PENDING_TITLES) or EMPTY_PENDING
    conclusion = {
        "core": core,
        "positions": positions,
        "judge_ruling": ruling,
        "pending": pending,
    }
    report = (f"## 核心结论\n\n{core}\n\n"
              f"## 各方立场与论点摘要\n\n{positions}\n\n"
              f"## 裁判裁决\n\n{ruling}\n\n"
              f"## 分歧与待办\n\n{pending}\n")
    return report, conclusion


def fallback_summary(judgment: Judgment | None, transcript: list) -> tuple:
    """确定性汇总：不调用 LLM，直接基于发言记录与裁决拼装四段式报告。"""
    report, conclusion = normalize_conclusion("", judgment, transcript)
    return FALLBACK_NOTE + report, conclusion
