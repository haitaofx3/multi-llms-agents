"""
summary.py — 运行摘要（Run Summary）

一次运行结束时，把「发生了什么」聚合成一份**机器可读 + 人可读**的摘要对象：

  {trace_id, session_id, 状态机终态, 迭代序号,
   轮次 / 收敛 / 任务完成度,
   每个模型席位的发言数、字符数、TTFT、生成速率、token 占比,
   token 与费用、裁判结果、中途指令统计、事件总线背压、异常列表,
   关键节点（highlights）与产物文件路径}

设计取舍：
  - 摘要**不落库**（可由事件日志 + 编排器状态随时重算），落库的只有 `runlog`
    索引里那份轻量记录。这样摘要字段可以随版本演进，不会出现「旧摘要缺字段、
    新前端读不到」的兼容负担。
  - 所有字段都用 `.get()` 逐层防御：摘要常被外部集成（飞书 / DSH / webhook）
    直接序列化，任何一个字段取不到都不该让整份摘要 500。
"""
from __future__ import annotations

import time
from typing import Any

from muti_llm_agent.config import settings
from muti_llm_agent.steering import KIND_LABELS
from muti_llm_agent.textutil import estimate_cost_rmb


def _safe(fn, default=None):
    """执行一个取值函数，异常时返回默认值（摘要构建全程容错）。"""
    try:
        return fn()
    except Exception:
        return default


def _agent_view(orch: Any, agent: Any) -> dict:
    """单个席位（模型）的运行视图。"""
    name = getattr(agent, "name", "")
    metrics = (getattr(orch, "agent_metrics", {}) or {}).get(name, {}) or {}
    # agent_metrics 只统计「有文本产出、拿到 TTFT」的调用（裁判评分用）；
    # llm_call_counts 统计所有成功返回的调用（含纯工具调用回合），复盘与费用分析看后者。
    metric_calls = int(metrics.get("calls", 0) or 0)
    llm_calls = int((getattr(orch, "llm_call_counts", {}) or {}).get(name, 0) or 0)
    ttft_sum = float(metrics.get("ttft_sum", 0.0) or 0.0)
    gen_sum = float(metrics.get("gen_sum", 0.0) or 0.0)
    model_config = getattr(agent, "model_config", {}) or {}

    speeches, chars = 0, 0
    for e in getattr(orch, "transcript", []) or []:
        # action = 只发工具调用、无正文的回合（已由编排器折叠成动作摘要）
        if e.get("speaker") == name and e.get("type") in ("speech", "action"):
            speeches += 1
            chars += len(e.get("text", "") or "")

    return {
        "name": name,
        "role": getattr(agent, "role", "") or "",
        "stance": getattr(agent, "stance", "") or "",
        "is_main": bool(getattr(agent, "is_main", False)),
        "provider": model_config.get("provider", "") or _provider_of(model_config),
        "model_id": model_config.get("model_id", "") or "",
        "base_url": model_config.get("base_url", "") or "",
        "speeches": speeches,
        "chars": chars,
        "llm_calls": llm_calls,
        "avg_ttft_ms": round(ttft_sum / metric_calls * 1000, 1) if metric_calls else None,
        "avg_gen_rate": round(gen_sum / metric_calls, 1) if metric_calls else None,
        "streamed_chars": int(metrics.get("chars", 0) or 0),
    }


def _provider_of(model_config: dict) -> str:
    """从 model_config 推断 provider 名（预设名优先，其次 base_url 主机名）。"""
    preset = model_config.get("preset") or model_config.get("provider")
    if preset:
        return str(preset)
    base = str(model_config.get("base_url", "") or "")
    for key, name in (("deepseek", "deepseek"), ("dashscope", "qwen"),
                      ("xiaomimimo", "mimo"), ("openai", "openai")):
        if key in base:
            return name
    return "custom"


def build_summary(orch: Any) -> dict:
    """构建一次运行的完整摘要（参数为 Orchestrator 实例）。"""
    now_ms = int(time.time() * 1000)
    started_at = getattr(orch, "_started_at", None)
    ended_at = getattr(orch, "ended_at", None)
    elapsed_ms = 0
    if started_at:
        elapsed_ms = int(((ended_at or time.time()) - started_at) * 1000)

    agents = [_agent_view(orch, a) for a in (getattr(orch, "agents", []) or [])]
    judge = getattr(orch, "judge", None)
    judgment = getattr(orch, "judgment", None)

    steering = getattr(orch, "steering", None)
    steering_stats = _safe(steering.stats, {}) if steering is not None else {}
    directive_items = _safe(steering.all, []) if steering is not None else []

    tasks = getattr(orch, "tasks", []) or []
    done_tasks = sum(1 for t in tasks if t.get("completed"))

    spent = int(getattr(orch, "spent", 0) or 0)
    output_tokens = int(getattr(orch, "output_tokens", 0) or 0)

    bus = getattr(orch, "event_bus", None)
    bus_stats = _safe(bus.stats, {}) if bus is not None else {}

    iterations = getattr(orch, "iterations", []) or []
    errors = list(getattr(orch, "errors", []) or [])

    summary: dict[str, Any] = {
        "kind": "run_summary",
        "session_id": getattr(orch, "session_id", "default"),
        "trace_id": getattr(orch, "trace_id", ""),
        "mode": getattr(orch, "mode", ""),
        "topic": getattr(orch, "topic", ""),
        "status": getattr(orch, "status", ""),
        "phase": getattr(orch, "phase", ""),
        "iteration": int(getattr(orch, "iteration_no", 1) or 1),
        "iterations": [
            {
                "iteration": it.get("iteration"),
                "trace_id": it.get("trace_id"),
                "status": it.get("status"),
                "rounds": it.get("rounds"),
                "output_tokens": it.get("output_tokens"),
                "cost_rmb": it.get("cost_rmb"),
                "conclusion_core": (it.get("conclusion_core") or "")[:600],
                "focus": it.get("focus", ""),
                "materials_chars": it.get("materials_chars", 0),
                "finished_at": it.get("finished_at"),
            }
            for it in iterations
        ],
        "started_at": int(started_at * 1000) if started_at else None,
        "ended_at": int(ended_at * 1000) if ended_at else now_ms,
        "elapsed_ms": elapsed_ms,
        "rounds_executed": int(getattr(orch, "round", 0) or 0),
        "max_rounds": int(getattr(orch, "max_rounds", 0) or 0),
        "convergence": {
            "mode": getattr(orch, "convergence_mode", ""),
            "streak": int(getattr(orch, "convergence_streak", 0) or 0),
            "triggered": int(getattr(orch, "convergence_streak", 0) or 0)
            >= int(getattr(settings, "CONVERGENCE_ROUNDS", 2)),
        },
        "agents": agents,
        "models_used": sorted({a["model_id"] for a in agents if a["model_id"]}),
        "providers_used": sorted({a["provider"] for a in agents if a["provider"]}),
        "tokens": {
            "total": spent,
            "output": output_tokens,
            "input_est": max(0, spent - output_tokens),
        },
        "cost_rmb": _safe(lambda: estimate_cost_rmb(spent, output_tokens), 0.0),
        "tasks": {
            "total": len(tasks),
            "completed": done_tasks,
            "pending": len(tasks) - done_tasks,
        },
        "judge": {
            "enabled": judge is not None,
            "name": getattr(judge, "name", "") if judge is not None else "",
            "winner": getattr(judgment, "winner", None) if judgment else None,
            "composite_scores": getattr(judgment, "composite_scores", {}) if judgment else {},
            "critique": (getattr(judgment, "critique", "") or "")[:800] if judgment else "",
        },
        "conclusion": {
            "core": ((getattr(orch, "conclusion", None) or {}).get("core") or "")[:1200],
            "pending": ((getattr(orch, "conclusion", None) or {}).get("pending") or "")[:800],
        },
        "steering": dict(steering_stats, items=directive_items),
        "events": {
            "subscribers": bus_stats.get("subscribers", 0),
            "dropped": bus_stats.get("dropped", 0),
            "transcript": len(getattr(orch, "transcript", []) or []),
        },
        "run_stats": dict(getattr(orch, "run_stats", {}) or {}),
        "errors": errors[-20:],
        "error_count": len(errors),
        "highlights": [],
        "files": {},
    }
    summary["highlights"] = build_highlights(orch, summary)
    return summary


def build_highlights(orch: Any, summary: dict) -> list[str]:
    """关键节点摘要（给复盘页 / 飞书卡片用的一眼可读结论）。"""
    out: list[str] = []
    rounds = summary.get("rounds_executed", 0)
    max_rounds = summary.get("max_rounds", 0)
    out.append(f"共 {rounds}/{max_rounds} 轮，{len(summary.get('models_used', []))} 个模型参与")
    if summary.get("convergence", {}).get("triggered"):
        out.append(f"第 {rounds} 轮触发收敛（连续 {summary['convergence']['streak']} 轮高相似）")
    steering = summary.get("steering", {}) or {}
    if steering.get("added"):
        kinds = steering.get("by_kind") or {}
        kind_text = "、".join(f"{KIND_LABELS.get(k, k)}×{v}" for k, v in kinds.items()) or "指令"
        out.append(f"中途注入 {steering.get('added')} 条指令（{kind_text}），"
                   f"已生效 {steering.get('injected')} 次")
    judge = summary.get("judge", {}) or {}
    if judge.get("enabled") and judge.get("winner"):
        out.append(f"裁判裁决胜方：{judge['winner']}")
    tasks = summary.get("tasks", {}) or {}
    if tasks.get("total"):
        out.append(f"任务 {tasks.get('completed', 0)}/{tasks.get('total', 0)} 完成")
    if summary.get("error_count"):
        out.append(f"运行期出现 {summary['error_count']} 条异常记录（详见 errors）")
    iterations = summary.get("iterations") or []
    if iterations:
        out.append(f"已完成 {len(iterations)} 次「补充材料续轮」")
    return out


def summary_line(summary: dict) -> str:
    """一句话摘要（出站 webhook / 飞书文本消息复用）。"""
    tokens = (summary.get("tokens") or {}).get("output", 0)
    return (
        f"【{summary.get('status', '?')}】{summary.get('topic', '')[:40]}"
        f"｜{len(summary.get('models_used', []))} 模型"
        f"｜{summary.get('rounds_executed', 0)}/{summary.get('max_rounds', 0)} 轮"
        f"｜输出 {tokens} tokens"
        f"｜≈¥{float(summary.get('cost_rmb', 0) or 0):.4f}"
        f"｜trace {summary.get('trace_id', '')}"
    )
