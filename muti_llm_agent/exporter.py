"""
exporter.py — 讨论记录导出（Markdown）

从 orchestrator.py 抽出的「渲染层」：把一次讨论的元信息、运行摘要、公平性
治理报告、发言记录、裁判裁决、模型性能、任务结果与记忆快照渲染成 Markdown。

之所以单独成模块：这是纯粹的"读状态 → 出文本"逻辑（约 100 行），
和编排流程没有任何耦合，却长期与编排代码挤在同一个文件里；抽出来后
编排器只剩一行委托，导出格式的调整也不必再碰编排逻辑。
"""
from __future__ import annotations

import json
import time
from typing import Any

from muti_llm_agent.config import settings
from muti_llm_agent.prompts import STANCE_LABEL
from muti_llm_agent.textutil import estimate_cost_rmb


def render_markdown(orch: Any) -> str:
    """渲染一场讨论的完整 Markdown 记录（参数为 Orchestrator 实例）。"""
    lines = []
    # 双模式：标题按讨论模式切换
    lines.append("# 多模型协同辩论记录\n" if orch.mode == "debate"
                 else "# 多模型协同记录\n")
    lines.append(f"- 主题：{orch.topic}")
    lines.append(f"- 轮次：{orch.round}/{orch.max_rounds}")
    lines.append(f"- 状态：{orch.status} / 阶段：{orch.phase}")
    lines.append(f"- Trace ID：{orch.trace_id}")
    lines.append(f"- Token 用量（只读遥测）：累计 {orch.spent} / 输出 {orch.output_tokens}")
    lines.append(f"- 时间：{time.strftime('%Y-%m-%d %H:%M:%S')}")
    lines.append("- 参与 Agent：")
    for a in orch.agents:
        tag = "（主）" if a.is_main else ""
        lines.append(f"  - {a.name}{tag} | 角色：{a.role} | 立场："
                     f"{STANCE_LABEL.get(a.stance, a.stance)} | "
                     f"模型：{a.model_config['model_id']} @ {a.model_config['base_url']}")
    if orch.judge:
        lines.append(f"- 独立裁判：{orch.judge.name} | "
                     f"模型：{orch.judge.model_config['model_id']} @ "
                     f"{orch.judge.model_config['base_url']}")
    lines.append("")
    lines.append("## 运行摘要\n")
    elapsed = (time.time() - orch.run_stats.get("started_at", time.time())) \
        if orch.run_stats.get("started_at") else 0.0
    conv_flag = orch.convergence_streak >= settings.CONVERGENCE_ROUNDS
    done_tasks = sum(1 for t in orch.tasks if t["completed"])
    conv_text = f"是（连续{orch.convergence_streak}轮相似）" if conv_flag else "否"
    conv_mode_text = ("文本相似度（降级，嵌入模型不可用）"
                      if orch.convergence_mode == "text-similarity"
                      else "向量余弦相似度（嵌入模型）")
    lines.append(f"- 持续时间：{elapsed:.1f} 秒")
    lines.append(f"- 实际轮次：{orch.round}/{orch.max_rounds}")
    if orch.mode == "debate":
        lines.append(f"- 收敛触发：{conv_text}")
        lines.append(f"- 收敛检测模式：{conv_mode_text}")
    else:
        lines.append("- 收敛触发：不适用（合作模式无收敛检测）")
    lines.append(f"- RAG 证据轮数：{len(orch.round_evidence)}")
    lines.append(f"- 任务：{len(orch.tasks)}（完成 {done_tasks}）")
    lines.append(f"- 历史压缩次数：{orch.run_stats.get('compressions', 0)}")
    lines.append(f"- Token 用量（只读遥测）：累计 {orch.spent} / 输出 {orch.output_tokens}"
                 f"（不再作为约束）")
    lines.append(f"- 预估费用（P1-6）：≈ ¥{estimate_cost_rmb(orch.spent, orch.output_tokens):.4f}")
    # 公平性治理报告（替代旧的预算规划摘要）
    fair = orch.governor.report()
    lines.append(f"- 公平性治理：{'全部通过' if fair['ok'] else '存在违约'}"
                 f"（{len(fair['warnings'])} 警告 / {len(fair['violations'])} 违约）")
    for layer in fair["layers"]:
        lines.append(f"  - {layer['layer']} {layer['name']}：{layer['status']} — {layer['detail']}")
    lines.append("")
    lines.append("## 发言记录\n")
    for e in orch.transcript:
        if e["type"] == "speech":
            lines.append(f"**{e['speaker']}**（第{e['round']}轮）：\n\n{e['text']}\n")
        elif e["type"] == "action":
            # 纯工具调用回合（只有动作没有正文）：保留下来，否则复盘会显示「0 次发言」
            lines.append(f"**{e['speaker']}**（第{e['round']}轮 · 动作）：\n\n{e['text']}\n")
        elif e["type"] == "directive":
            lines.append(f"**{e['speaker']}**（第{e['round']}轮 · 收到中途指令）：\n\n{e['text']}\n")
        elif e["type"] == "summary":
            lines.append(f"## 最终讨论结果（{e['speaker']} 整理）\n\n{e['text']}\n")
    if orch.judgment:
        lines.append("## 裁判裁决\n")
        j = orch.judgment
        lines.append(f"- 得分：{json.dumps(j.scores, ensure_ascii=False)}")
        if j.agent_scores:
            lines.append("- 辩手评分：")
            for name, sc in j.agent_scores.items():
                lines.append(f"  - {name}：{json.dumps(sc, ensure_ascii=False)}")
        lines.append(f"- 胜方：{j.winner}")
        lines.append(f"- 收敛终止：{'是' if j.convergence_flag else '否'}")
        lines.append(f"- 评语：{j.critique}")
        lines.append("")
    if orch.judgment and orch.judgment.model_performance:
        lines.append("## 模型性能与综合评分\n")
        lines.append("| 辩手 | 平均TTFT | 平均生成速率 | 模型质量分 | 综合评分 |")
        lines.append("| --- | --- | --- | --- | --- |")
        for name, p in orch.judgment.model_performance.items():
            ttft = f"{p['avg_ttft_ms']}ms" if p.get("avg_ttft_ms") is not None else "N/A"
            rate = f"{p['avg_gen_rate']}字/s" if p.get("avg_gen_rate") is not None else "N/A"
            comp = orch.judgment.composite_scores.get(name, "N/A")
            lines.append(f"| {name} | {ttft} | {rate} | {p['performance_score']} | {comp} |")
        lines.append("")
    lines.append("## 任务结果\n")
    if orch.tasks:
        lines.append("| 任务# | 接收方 | 变量 | 完成 | 结果 |")
        lines.append("| --- | --- | --- | --- | --- |")
        for t in orch.tasks:
            lines.append(f"| {t['id']} | {t['recipient']} | {t['var_name']} | "
                         f"{'是' if t['completed'] else '否'} | "
                         f"{t['result_value'] or t['result_message']} |")
    else:
        lines.append("（本次讨论未产生任务分派）")
    lines.append("")
    lines.append("## 各 Agent 记忆快照\n")
    for a in orch.agents:
        snap = a.memory.snapshot()
        lines.append(f"- {a.name}：短期 {snap['short_term']} / 长期 {snap['long_term']} "
                     f"/ 情境 {snap['contextual']}")
    return "\n".join(lines)
