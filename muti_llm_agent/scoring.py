"""
scoring.py — 裁决打分（确定性计算，非 LLM）

从 orchestrator.py 抽出的评分层：
  - `score_ttft` / `score_rate`：把系统实测的 TTFT、生成速率换算成 1-10 分
  - `model_performance`：聚合各辩手流式实测指标（平均值 + 质量分）
  - `performance_text`：给裁判参考的性能文本
  - `compute_composite`：全局综合得分 / 综合评分 / 胜方推导
  - `judge_ruling_text`：权威裁决文本（防主持人转述失真）

设计要点（保持不变）：
  - 全局 `scores` 由后端按各辩手四维分数取均值，**不采信 LLM 给出的综合分**；
  - `composite`（内容 × 权重 + 模型质量 × 权重）**仅展示**；
  - **胜方只由内容四维均分决定**（纯内容判定，并列 DRAW）。
"""
from __future__ import annotations

import json
from collections.abc import Callable

from muti_llm_agent.config import settings
from muti_llm_agent.protocol import Judgment

DIMENSIONS = ("logic", "evidence", "rebuttal", "clarity")


def score_ttft(ttft_s: float | None) -> float:
    """TTFT（首 token 延迟，秒）→ 1-10 分。"""
    if ttft_s is None:
        return 5.0
    if ttft_s <= 1:
        return 10
    if ttft_s <= 5:
        return 8
    if ttft_s <= 15:
        return 6
    if ttft_s <= 30:
        return 4
    return 2


def score_rate(rate: float | None) -> float:
    """生成速率（字符/秒）→ 1-10 分。"""
    if rate is None:
        return 5.0
    if rate >= 80:
        return 10
    if rate >= 40:
        return 8
    if rate >= 20:
        return 6
    if rate >= 10:
        return 4
    return 2


def model_performance(agent_metrics: dict) -> dict:
    """聚合各辩手流式实测指标 → {辩手: {avg_ttft_ms, avg_gen_rate, performance_score}}。"""
    perf = {}
    for name, m in agent_metrics.items():
        if m["calls"] <= 0:
            continue
        avg_ttft = m["ttft_sum"] / m["calls"]
        avg_rate = m["chars"] / m["gen_sum"] if m["gen_sum"] > 0 else 0.0
        perf[name] = {
            "avg_ttft_ms": round(avg_ttft * 1000, 1),
            "avg_gen_rate": round(avg_rate, 1),
            "performance_score": round(
                (score_ttft(avg_ttft) + score_rate(avg_rate)) / 2, 1),
        }
    return perf


def performance_text(perf: dict, exclude_name: str = "") -> str:
    """供裁判提示词/评语参考的模型性能指标文本（默认剔除主持人）。"""
    if not perf:
        return ""
    lines = [f"- {n}：平均TTFT={v['avg_ttft_ms']}ms，平均生成速率={v['avg_gen_rate']}字/s"
             for n, v in perf.items() if n != exclude_name]
    return "\n".join(lines) if lines else ""


def judge_ruling_text(judgment: Judgment | None) -> str:
    """权威裁判裁决文本（基于 judgment 数据，防止主持人转述失真/编造）。"""
    if not judgment:
        return "（无裁判裁决）"
    parts = [f"- 胜方：{judgment.winner}",
             f"- 综合得分：{json.dumps(judgment.scores, ensure_ascii=False)}"]
    if judgment.agent_scores:
        parts.append(
            f"- 辩手评分：{json.dumps(judgment.agent_scores, ensure_ascii=False)}")
    if judgment.composite_scores:
        parts.append(
            f"- 综合评分（内容×{settings.JUDGE_CONTENT_WEIGHT} + "
            f"模型质量×{settings.JUDGE_PERFORMANCE_WEIGHT}）："
            f"{json.dumps(judgment.composite_scores, ensure_ascii=False)}")
    parts.append(f"- 收敛终止：{'是' if judgment.convergence_flag else '否'}")
    parts.append(f"- 评语：{judgment.critique or ''}")
    return "\n".join(parts)


def compute_composite(judgment: Judgment, names: list, perf: dict,
                      stance_of: Callable[[str], str]) -> Judgment:
    """计算全局综合得分、模型质量分与最终综合分，并依据内容分推导胜方。

    - 全局综合得分 scores：按各辩手四维分数取均值（确定性，不再依赖 LLM 输出），
      避免"综合得分 == 某辩手得分"的照抄问题。
    - composite = 内容均分 × 内容权重 + 模型质量分 × 性能权重（仅展示）。
    - 胜方仅由内容均分决定（纯内容判定）；模型质量分不参与胜负。
    - agent_scores 缺失（如裁判调用失败回退）时 composite 退化为模型质量分，胜方 DRAW。
    """
    judgment.model_performance = {
        n: perf.get(n, {"avg_ttft_ms": None, "avg_gen_rate": None,
                        "performance_score": 5.0})
        for n in names
    }
    # 全局综合得分 = 各辩手该维度分数均值
    if judgment.agent_scores:
        global_scores = {}
        for dim in DIMENSIONS:
            vals = [judgment.agent_scores[n][dim] for n in names
                    if judgment.agent_scores.get(n, {}).get(dim) is not None]
            if vals:
                global_scores[dim] = round(sum(vals) / len(vals), 1)
        judgment.scores = global_scores

    composite = {}
    content_avgs = {}
    for n in names:
        content = judgment.agent_scores.get(n)
        content_avg = (sum(content.values()) / len(content)) if content else None
        if content_avg is not None:
            content_avgs[n] = content_avg
        pscore = judgment.model_performance[n]["performance_score"]
        if content_avg is not None:
            comp = (settings.JUDGE_CONTENT_WEIGHT * content_avg
                    + settings.JUDGE_PERFORMANCE_WEIGHT * pscore)
        else:
            comp = pscore
        composite[n] = round(comp, 2)
    judgment.composite_scores = composite

    # 胜方：内容均分最高辩手所在方（PRO/CON）；并列最高取 DRAW（纯内容判定）
    if content_avgs:
        top = max(content_avgs.values())
        top_names = [n for n, v in content_avgs.items() if v == top]
        top_stances = {stance_of(n) for n in top_names}
        if len(top_stances) == 1 and top_stances <= {"PRO", "CON"}:
            judgment.winner = top_stances.pop()
        else:
            judgment.winner = "DRAW"
    else:
        judgment.winner = "DRAW"
    return judgment
