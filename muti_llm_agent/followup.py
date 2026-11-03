"""
followup.py — 会话结束后的「补充材料续轮」

为什么需要它：一场多模型协同跑到结束时，结论往往不是「错了」，而是「缺料」——
用户看了输出才想起来手头还有访谈记录、竞品数据、上一版评审意见没给。传统做法是
重开一场，把老结论和新材料一起贴进主题里，结果既丢掉了上下文，又浪费了已经花掉的
token 与轮次。

本模块把「续轮」做成一次**受控的迭代**：

  第 N 轮运行结束（status=done/stopped）
    → 用户提交补充材料 + 本轮焦点
    → 上一轮的结论 / 分歧 / 裁判裁决 / 关键发言被压缩成「承接包」（carryover）
    → 编排器开一轮新迭代（新 trace_id、轮次清零、席位与模型不变）
    → 承接包 + 新材料作为「本轮前提」注入主持人指令，各模型在此之上做增量优化
    → 迭代链记录在 Orchestrator.iterations 里，复盘页可对比每轮差异

本模块只做**纯函数**部分（校验、压缩承接包、渲染 seed 文本），
真正的状态切换在 `Orchestrator.start_followup()` 里完成。
"""
from __future__ import annotations

from typing import Any

from muti_llm_agent.config import settings

# 允许续轮的终态（运行中 / 暂停中不允许，避免与主循环抢状态）
FOLLOWUP_ALLOWED_STATUS = ("done", "stopped", "interrupted", "idle")


def normalize_materials(materials: Any) -> tuple[str, int]:
    """把材料入参归一为一段文本。

    支持三种形态：
      - str：直接使用
      - list[str] / list[dict]：逐条拼接为 Markdown 列表
      - dict：取其中的 text / content 字段（兼容前端直接透传文件解析结果）

    返回 (文本, 字符数)；超长按 FOLLOWUP_MAX_MATERIAL_CHARS 截断。
    """
    text = ""
    if isinstance(materials, str):
        text = materials
    elif isinstance(materials, (list, tuple)):
        parts = []
        for item in materials:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                body = item.get("text") or item.get("content") or ""
                src = item.get("source") or item.get("name") or ""
                parts.append(f"- 材料（{src}）：{body}" if src else f"- {body}")
        text = "\n".join(parts)
    elif isinstance(materials, dict):
        text = str(materials.get("text") or materials.get("content") or "")
    text = (text or "").strip()
    limit = int(getattr(settings, "FOLLOWUP_MAX_MATERIAL_CHARS", 12000))
    if len(text) > limit:
        text = text[:limit] + f"\n\n（材料过长，已截断至 {limit} 字）"
    return text, len(text)


def validate_followup(orch: Any) -> None:
    """校验能否续轮；不满足时抛 ValueError（由接口层转成 400）。"""
    if not bool(getattr(settings, "FOLLOWUP_ENABLED", True)):
        raise ValueError("补充材料续轮功能已关闭（FOLLOWUP_ENABLED=0）")
    if getattr(orch, "is_active", False):
        raise ValueError("当前运行仍在进行（running/paused），请先结束再补充材料续轮")
    if not (getattr(orch, "agents", None) or []):
        raise ValueError("该会话尚未配置席位，无法续轮")
    if not (getattr(orch, "transcript", None) or []):
        raise ValueError("该会话还没有可承接的讨论内容，请先完成一轮讨论")
    iteration_no = int(getattr(orch, "iteration_no", 1) or 1)
    max_iter = int(getattr(settings, "MAX_ITERATIONS", 5) or 5)
    if iteration_no >= max_iter:
        raise ValueError(f"已达最大迭代次数 {max_iter}，请开启新的运行")


def _truncate(text: str, limit: int) -> str:
    text = (text or "").strip()
    if limit and len(text) > limit:
        return text[:limit] + "…"
    return text


def build_carryover(orch: Any) -> str:
    """把上一轮运行压缩成「承接包」文本（结论 / 分歧 / 关键发言 / 任务结果）。"""
    limit = int(getattr(settings, "FOLLOWUP_CARRYOVER_CHARS", 4000))
    conclusion = getattr(orch, "conclusion", None) or {}
    lines: list[str] = []

    core = (conclusion.get("core") or "").strip()
    if core:
        lines.append("【上一轮核心结论】")
        lines.append(_truncate(core, max(400, limit // 3)))

    positions = (conclusion.get("positions") or "").strip()
    if positions:
        lines.append("")
        lines.append("【各方立场与论点】")
        lines.append(_truncate(positions, max(300, limit // 4)))

    judge_ruling = (conclusion.get("judge_ruling") or "").strip()
    if judge_ruling:
        lines.append("")
        lines.append("【裁判裁决】")
        lines.append(_truncate(judge_ruling, max(200, limit // 6)))

    pending = (conclusion.get("pending") or "").strip()
    if pending:
        lines.append("")
        lines.append("【上一轮遗留的分歧与待办】")
        lines.append(_truncate(pending, max(200, limit // 6)))

    # 末轮发言要点：只取最后一轮每个席位的发言前若干字，避免承接包过长
    last_round = int(getattr(orch, "round", 0) or 0)
    per_agent = max(120, limit // (2 * max(1, len(getattr(orch, "agents", []) or []))))
    tail: list[str] = []
    for e in reversed(getattr(orch, "transcript", []) or []):
        if e.get("type") != "speech":
            continue
        if int(e.get("round", 0) or 0) != last_round:
            continue
        tail.append(f"- {e.get('speaker', '')}：{_truncate(e.get('text', ''), per_agent)}")
        if len(tail) >= len(getattr(orch, "agents", []) or []):
            break
    if tail:
        lines.append("")
        lines.append("【末轮发言要点】")
        lines.extend(reversed(tail))

    # 任务结果（合作模式的关键产出）
    tasks = getattr(orch, "tasks", []) or []
    done = [t for t in tasks if t.get("completed")]
    if done:
        lines.append("")
        lines.append("【已交付的任务结果】")
        for t in done[-6:]:
            value = _truncate(t.get("result_value") or t.get("result_message") or "", 200)
            lines.append(f"- {t.get('var_name', '')}（{t.get('recipient') or '任务池'}）：{value}")

    text = "\n".join(lines).strip()
    if len(text) > limit:
        text = text[:limit] + "\n（承接包已按上限截断）"
    return text


def build_seed_block(materials: str, focus: str = "", carryover: str = "") -> str:
    """渲染注入主持人指令的「本轮前提」文本块。"""
    blocks: list[str] = []
    if carryover:
        blocks.append("## 上一轮承接\n" + carryover)
    if materials:
        blocks.append("## 本轮补充材料\n" + materials)
    if focus:
        blocks.append("## 本轮聚焦\n" + focus.strip())
    if not blocks:
        return ""
    blocks.append(
        "## 本轮要求\n"
        "1. 以上一轮结论为起点做**增量优化**，不要重复上一轮已经确认过的内容；\n"
        "2. 明确说明本轮补充材料改动了哪些判断，以及为什么；\n"
        "3. 若新材料与上一轮结论冲突，必须在发言中显式指出冲突点并给出取舍理由；\n"
        "4. 交付物要比上一轮更具体（可直接使用），而不是更抽象。")
    return "\n\n".join(blocks)


def snapshot_iteration(orch: Any) -> dict:
    """把「当前这一轮迭代」的快照收进迭代链（续轮开始时调用）。"""
    import time

    summary_core = ""
    try:
        from muti_llm_agent.summary import build_summary
        summary_core = (build_summary(orch).get("conclusion") or {}).get("core", "")
    except Exception:
        summary_core = (getattr(orch, "conclusion", None) or {}).get("core", "")
    return {
        "iteration": int(getattr(orch, "iteration_no", 1) or 1),
        "trace_id": getattr(orch, "trace_id", ""),
        "status": getattr(orch, "status", ""),
        "rounds": int(getattr(orch, "round", 0) or 0),
        "max_rounds": int(getattr(orch, "max_rounds", 0) or 0),
        "output_tokens": int(getattr(orch, "output_tokens", 0) or 0),
        "spent_tokens": int(getattr(orch, "spent", 0) or 0),
        "cost_rmb": round(_cost(orch), 4),
        "conclusion_core": summary_core or "",
        "focus": getattr(orch, "followup_focus", "") or "",
        "materials_chars": int(getattr(orch, "followup_materials_chars", 0) or 0),
        "finished_at": int(time.time() * 1000),
    }


def _cost(orch: Any) -> float:
    try:
        from muti_llm_agent.textutil import estimate_cost_rmb
        return float(estimate_cost_rmb(
            int(getattr(orch, "spent", 0) or 0),
            int(getattr(orch, "output_tokens", 0) or 0)))
    except Exception:
        return 0.0
