"""
turn.py — 单个 Agent 回合的执行器（LLM 往返 + 工具调用循环）

## 为什么要单独一个模块

`Orchestrator._agent_turn` 曾经把五件事写在一个近 300 行的方法里：纠偏注入、
消息装配、流式消费、工具循环、公平性截断、transcript 记账。改任何一件都要在
无关代码之间来回跳，也很难单独验证「工具循环」这一层。

这里只搬走**最独立、最好测**的那一段：与 LLM 的往返（流式 / 非流式 + 回退 + 重试）
以及工具调用的多轮循环。编排器仍然保留：

  - 纠偏指令注入、承接包注入（要读写 SteeringQueue / followup 状态）
  - system prompt 与 messages 装配（要读共享黑板与任务表）
  - 公平性 L1 截断与统计（要读写 FairnessGovernor）
  - transcript / history 记账（属于运行期状态）
  - 纯动作回合的兜底可见性（要合并 delegate/claim/report 三类结果）

## 设计取舍：为什么用回调而不是把 Orchestrator 传进来

本模块只通过 7 个显式回调与外界交互，每个都是一条真实的接缝：

  - `publish`              推送过程事件（speech_start / delta / tool_call_* / agent_error）
  - `log`                  结构化日志
  - `record_error`         记入运行期异常清单
  - `record_model_failure` 按 model_id 记模型健康度
  - `on_llm_result`        一次成功调用的记账（调用计数 / TTFT 指标 / token 遥测）
  - `stream_call`          流式 LLM 调用
  - `sync_call`            非流式 LLM 调用（含重试）

好处是 `Orchestrator` 不必为了被调用而暴露内部状态，本模块也能在测试里用几个
桩函数直接驱动，不需要起整个编排器。

## 不变式（改动时请保持）

  1. `speech_start` 只在「首次产出文本增量」或「首次出现工具调用」时推一次，
     保证前端气泡既不重复也不悬空；
  2. 流式中已经推过 `tool_call_start` 的工具索引（`tool_starts`）不再重复推；
  3. `delegate_task` / `report_result` / `claim_task` 是**终态动作**：同一条 assistant
     消息里的整批工具跑完后即结束本回合，不再发起下一轮 LLM 调用
     （否则模型会一边分派一边自说自话）；
  4. 普通工具的执行结果必须以 `role=tool` 消息回灌上下文，否则下一轮模型看不到结果。
"""
from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from muti_llm_agent.textutil import shorten as _shorten

if TYPE_CHECKING:  # pragma: no cover - 仅用于类型标注，避免与 agent.py 形成环
    from muti_llm_agent.agent import Agent

# 特殊动作工具名（终态：处理完即结束本回合）
DELEGATE_TOOL = "delegate_task"
REPORT_TOOL = "report_result"
CLAIM_TOOL = "claim_task"


@dataclass
class TurnOutcome:
    """一次 Agent 回合的执行结果（编排器据此做公平性截断与记账）。"""

    content: str = ""
    delegate: list[dict] = field(default_factory=list)
    report: dict | None = None
    claim: list[dict] = field(default_factory=list)
    #: 是否已向前端推过 speech_start（决定收尾要不要补 speech_end）
    emitted: bool = False
    #: 本回合累计 token 用量（只读遥测）
    token_cost: int = 0
    #: 最后一次调用报告的输出特征，供公平性 L1/L2 统计使用
    think_chars: int = 0
    raw_chars: int = 0


async def run_agent_turn(
    *,
    agent: Agent,
    messages: list[dict],
    tools: list[dict],
    round_no: int,
    stream: bool,
    temperature: float,
    top_p: float | None,
    max_loops: int,
    publish: Callable[[dict], Awaitable[None]],
    log: Callable[[str, str, str], None],
    record_error: Callable[[str, Exception], None],
    record_model_failure: Callable[[Agent, Exception], None],
    on_llm_result: Callable[[Agent, dict, dict | None], int],
    stream_call: Callable[..., Awaitable[dict]],
    sync_call: Callable[..., Awaitable[dict]],
) -> TurnOutcome:
    """驱动一次 Agent 回合：调用 LLM、执行工具、收集三类终态动作。

    Args:
        messages: 消息列表（**会被原地修改**：追加 assistant 的 tool_calls 消息
            与 `role=tool` 的执行结果，以维持多轮工具调用上下文）
        stream: 是否优先走流式；流式失败时自动回退到非流式
        max_loops: 工具调用循环上限，跑满仍未产出终态则按已有内容收尾
        on_llm_result: 每次成功调用后记账，返回本次 token 用量

    Returns:
        TurnOutcome
    """
    outcome = TurnOutcome()
    emitting = False  # 是否已推过 speech_start（闭包内用 nonlocal 维护）
    result: dict | None = None

    async def _open_speech() -> None:
        """首次产出内容/工具调用前，先补一条 speech_start。"""
        nonlocal emitting
        if emitting:
            return
        emitting = True
        await publish({
            "type": "speech_start",
            "speaker": agent.name,
            "round": round_no,
            "is_main": agent.is_main,
        })

    async def _emit_delta(delta: str) -> None:
        await _open_speech()
        await publish({
            "type": "speech_delta",
            "speaker": agent.name,
            "delta": delta,
        })

    async def _emit_tool_start(name: str, index: int = 0) -> None:
        # 纯工具回合（无文本）也要有气泡，所以同样先开 speech
        await _open_speech()
        await publish({
            "type": "tool_call_start",
            "speaker": agent.name,
            "round": round_no,
            "is_main": agent.is_main,
            "name": name,
            "index": index,
        })

    async def _emit_tool_end(name: str, summary: str = "") -> None:
        await publish({
            "type": "tool_call_end",
            "speaker": agent.name,
            "round": round_no,
            "name": name,
            "summary": _shorten(summary, 120),
        })

    loop_limit = max(1, int(max_loops))
    for _loop_idx in range(loop_limit):
        # ---- 1. 调用 LLM（流式优先，失败回退非流式） ----
        try:
            if stream:
                try:
                    result = await stream_call(
                        messages, agent.model_config, tools, _emit_delta,
                        on_tool_start=_emit_tool_start,
                        temperature=temperature, top_p=top_p)
                except Exception as e:
                    log(f"[LLM流式失败:{agent.name}] {e}，回退非流式",
                        "warning", "llm.stream_fallback")
                    result = await sync_call(
                        messages, agent.model_config, tools,
                        temperature=temperature, top_p=top_p)
            else:
                result = await sync_call(
                    messages, agent.model_config, tools,
                    temperature=temperature, top_p=top_p)
        except Exception as e:
            log(f"[LLM错误:{agent.name}] {e}", "error", "llm.error")
            record_error(f"llm:{agent.name}", e)
            # 模型级健康度：把失败按 model_id 记账，花名册 / UI 能直接看到
            # 「这个席位上次调用挂了、为什么」，不必翻日志。
            record_model_failure(agent, e)
            # 把错误同步推送到前端（醒目的错误气泡），避免只在控制台可见
            await publish({
                "type": "agent_error",
                "agent": agent.name,
                "model_id": agent.model_config.get("model_id", ""),
                "error": str(e),
            })
            break

        # ---- 2. 记账（调用计数 / TTFT 指标 / token 遥测） ----
        metrics = result.pop("metrics", None)
        outcome.token_cost += on_llm_result(agent, result, metrics)
        outcome.content = result["content"] or outcome.content
        raw_msg = result.get("assistant_message")
        tcs = result["tool_calls"] or []

        if not tcs:
            break

        # 把 assistant 消息（含 tool_calls）加入上下文，维持多轮工具调用
        if raw_msg is not None:
            messages.append(raw_msg)

        # ---- 3. 执行工具调用（特殊动作优先，命中即终态） ----
        # 流式中已展示过的工具索引；非流式 / 流式回退时为空 → 全部在执行前补发 start
        streamed = set(result.get("tool_starts") or [])
        special = False
        for i, tc in enumerate(tcs):
            if i not in streamed:
                await _emit_tool_start(tc["name"], i)
            if agent.is_main and tc["name"] == DELEGATE_TOOL:
                outcome.delegate.append(tc["arguments"])
                special = True
                await _emit_tool_end(
                    tc["name"],
                    f"分派任务 → {tc['arguments'].get('recipient', '?')}")
            elif (not agent.is_main) and tc["name"] == REPORT_TOOL:
                outcome.report = tc["arguments"]
                special = True
                await _emit_tool_end(tc["name"], "回报任务结果")
            elif (not agent.is_main) and tc["name"] == CLAIM_TOOL:
                outcome.claim.append(tc["arguments"])
                special = True
                await _emit_tool_end(tc["name"], "认领任务")
            else:
                tool_result = await asyncio.to_thread(
                    agent.mcp.call, tc["name"], tc["arguments"])
                messages.append({
                    "role": "tool",
                    "tool_call_id": tc["id"],
                    "content": tool_result,
                })
                await _emit_tool_end(tc["name"], tool_result)
        if special:
            break
    else:
        # for 循环跑满仍未 break：说明模型在持续调用普通工具（未产出终态）
        log(f"[工具循环] {agent.name} 达到工具调用上限 {loop_limit} 轮，按已有内容收尾",
            "warning", "llm.tool_loop_limit")
        await publish({
            "type": "agent_error",
            "agent": agent.name,
            "error": f"工具调用超过 {loop_limit} 轮上限，已按当前结果收尾",
        })

    # 输出特征：供公平性 L1（截断基数）与 L2（思考占比）统计
    if result:
        outcome.think_chars = int(result.get("think_chars") or 0)
        outcome.raw_chars = int(
            result.get("raw_chars") or (len(outcome.content) + outcome.think_chars))
    else:
        outcome.raw_chars = len(outcome.content)

    outcome.emitted = emitting
    return outcome


__all__ = ["CLAIM_TOOL", "DELEGATE_TOOL", "REPORT_TOOL", "TurnOutcome", "run_agent_turn"]
