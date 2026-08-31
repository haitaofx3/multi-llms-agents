"""
orchestrator.py — 多 Agent 辩论系统编排器（Supervisor Agent 实现）

对齐《多Agent辩论系统架构设计文档》：
  - Supervisor 职责：任务编排、状态机流转、收敛触发、结果聚合；不亲自生成论点
  - 状态机：Idle → Preparing → Debating → Judging → Finished
  - 并行立论：第 1 轮所有辩手并发立论（asyncio.gather）
  - 串行反驳：后续轮次辩手基于最新交锋内容逐一反驳/辩护
  - 终止条件：max_rounds / 语义收敛检测 / 手动结束（公平性治理下无 token 预算熔断）
  - RAG 核查：每轮结束后异步事实核查，证据注入下一轮上下文
  - Judge 裁决：终止后单次调用裁判，输出 Judgment（scores/winner/critique/convergence_flag）
  - 公平性治理：L1 输出上限 / L2 有效回答比例 / L3 输入对称 / L4 轮次对称 /
    L5 采样参数对齐 / L6 长度中性评审（详见 app.fairness.py）
  - 消息协议：任务分派/回报事件带 task_id(UUID)、timestamp、token_cost
  - 可观测性：trace_id 贯穿事件与导出文件；统一日志
"""
import asyncio
import json
import os
import re
import threading
import time
from typing import ClassVar

from app.agent import Agent
from app.config import settings
from app.fairness import FairnessGovernor
from app.llm import chat, chat_stream, summarize_text
from app.protocol import Judgment, new_trace_id, new_uuid, now_ms
from app.rag import rag_service

# ---- 专用工具 schema（任务分派 / 回报） ----
DELEGATE_SCHEMA = {
    "type": "function",
    "function": {
        "name": "delegate_task",
        "description": "把一项任务分派给某个协作 Agent。明确说明任务内容与期望效果，"
                       "并提供用于接收结果的变量名(var_name)。",
        "parameters": {
            "type": "object",
            "properties": {
                "recipient": {"type": "string", "description": "接收任务的协作 Agent 名称"},
                "content": {"type": "string", "description": "任务内容说明"},
                "expected_outcome": {"type": "string", "description": "期望达到的效果"},
                "var_name": {"type": "string", "description": "用于接收结果的变量名(任务接收方变量)"},
            },
            "required": ["recipient", "content", "expected_outcome", "var_name"],
        },
    },
}

REPORT_SCHEMA = {
    "type": "function",
    "function": {
        "name": "report_result",
        "description": "向主 Agent 回报任务最终结果，并填充任务接收方变量。",
        "parameters": {
            "type": "object",
            "properties": {
                "var_name": {"type": "string", "description": "主 Agent 指定的变量名"},
                "var_value": {"type": "string", "description": "回填到该变量的结果内容"},
                "completed": {"type": "boolean", "description": "任务是否完成"},
                "message": {"type": "string", "description": "结果摘要（仅最终结论，不含中间过程）"},
            },
            "required": ["var_name", "var_value", "completed", "message"],
        },
    },
}

# 立场显示名
STANCE_LABEL = {"PRO": "正方", "CON": "反方", "NEUTRAL": "中立", "HOST": "主持人"}

# 架构状态机（对齐架构文档 §4.1）
PHASES = ("idle", "preparing", "debating", "judging", "finished")


class EventBus:
    def __init__(self):
        self.subs = []

    def subscribe(self):
        q = asyncio.Queue()
        self.subs.append(q)
        return q

    async def publish(self, event: dict):
        for q in list(self.subs):
            await q.put(event)

    def unsubscribe(self, q):
        if q in self.subs:
            self.subs.remove(q)


# 持有事件推送等后台 asyncio 任务的强引用，防止任务被 GC 提前回收（RUF006）
_background_tasks: set[asyncio.Task] = set()


def _spawn_task(coro):
    """创建并登记一个后台任务，任务结束后自动从集合移除。"""
    task = asyncio.create_task(coro)
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)
    return task


class Orchestrator:
    def __init__(self):
        self.agents = []
        self.main = None
        self.judge = None
        self.collaborators = []
        self.shared_context = {}      # var_name -> {"by","value","task_id"}
        self.tasks = []
        self.task_counter = 0
        self.round = 0
        self.max_rounds = 8
        self.topic = ""
        self.status = "idle"          # idle/running/paused/done/stopped（UI 状态）
        self.phase = "idle"           # idle/preparing/debating/judging/finished（架构状态机）
        self.trace_id = ""
        self.event_bus = EventBus()
        self.transcript = []          # 发言记录（用于导出 markdown）
        self._pause_event = asyncio.Event()
        self._pause_event.set()
        self._run_task = None

        # ---- 遥测（只读）：token 用量不再作为硬约束，仅保留观测 ----
        self.spent = 0                         # 累计 token 用量（含输入/输出，只读遥测）
        self.output_tokens = 0                 # 累计输出 token（实时显示，只读遥测）
        # ---- 多层公平性治理（FairnessGovernor，替代旧的 token 预算总控） ----
        self.governor = FairnessGovernor()

        # ---- 辩论状态 ----
        self._started_at = None
        self.convergence_streak = 0
        self.round_arguments = {}     # round -> {speaker: text}
        self.latest_arguments = {}    # speaker -> {"text","round"}
        self.round_evidence = {}      # round -> {speaker: [EvidenceItem]}
        self.judgment = None
        self.last_main_message = ""
        self.last_debate_speaker = None  # 上一轮/上一位实际发言的辩手（用于针对性反驳）
        self.run_stats = {"compressions": 0}  # 运行摘要统计（优化 A4）
        self.agent_metrics = {}  # 模型质量指标（流式发言实测）：{name: {calls, ttft_sum, gen_sum, chars}}

    # ---------------- 配置 ----------------
    def configure(self, agent_configs: list, topic: str = "", max_rounds: int = 8,
                  judge_config: dict | None = None):
        if self.status == "running":
            raise ValueError("讨论进行中，请先结束或等待后再重新配置")
        self.agents = []
        self.main = None
        self.judge = None
        self.collaborators = []
        self.shared_context = {}
        self.tasks = []
        self.task_counter = 0
        self.round = 0
        self.last_main_message = topic
        self.last_debate_speaker = None
        self.max_rounds = max(1, int(max_rounds))
        self.topic = topic
        self.transcript = []
        self.trace_id = new_trace_id()
        self.spent = 0                    # 累计 token 用量（只读遥测）
        self.output_tokens = 0            # 累计输出 token（实时显示，只读遥测）
        self.governor = FairnessGovernor()  # 公平性治理器（每场全新）
        self.phase = "idle"
        self.convergence_streak = 0
        self.round_arguments = {}
        self.latest_arguments = {}
        self.round_evidence = {}
        self.judgment = None
        self.last_debate_speaker = None
        self._opponent_prep = {}            # 对手定向反驳上下文（需求 2/3）
        self._opponent_summary_cache = {}   # (对手,轮次) -> 摘要，防同轮重复调用
        self.run_stats = {"compressions": 0}
        self.agent_metrics = {}

        for i, cfg in enumerate(agent_configs):
            from app.llm import resolve_model_config
            model_config = resolve_model_config(
                cfg.get("provider", "default"),
                cfg.get("base_url", ""),
                cfg.get("api_key", ""),
                cfg.get("model_id", ""),
            )
            # 立场：显式指定优先；否则主位=主持人，其余按 PRO/CON 交替
            stance = (cfg.get("stance", "") or "").upper()
            if not stance:
                stance = "HOST" if i == 0 else ("PRO" if i % 2 == 1 else "CON")
            agent = Agent(
                agent_id=cfg.get("agent_id", f"agent_{i+1}"),
                name=cfg.get("name", f"Agent_{i+1}"),
                role=cfg.get("role", ""),
                personality=cfg.get("personality", ""),
                model_config=model_config,
                is_main=(i == 0),
                forget_config=cfg.get("forget_config"),
                stance=stance,
            )
            self.agents.append(agent)
            if i == 0:
                self.main = agent
            else:
                self.collaborators.append(agent)
        # 独立裁判 Agent（不占席位、不参与辩论，仅裁决阶段调用）
        self.judge = self._build_judge(judge_config or {})
        self.status = "idle"
        self._log(f"配置完成，席位 {len(self.agents)} 个"
                  + (f"，独立裁判：{self.judge.name}" if self.judge else "，裁判回退使用主 Agent 模型"),
                  event="configure", level="info")

    # ---------------- 独立裁判（Judge Agent） ----------------
    def _build_judge(self, judge_config: dict):
        """根据 judge_config / .env 的 JUDGE_* 构建独立裁判 Agent。

        - judge_config 非空（前端已选择裁判模型）或 .env 配置了 JUDGE_* 时创建独立裁判；
        - 完全未配置时返回 None，_judge_round 回退使用主 Agent 的模型配置（保持向后兼容）。
        裁判不进入 self.agents / self.collaborators，因此不占席位、不参与辩论、不接收任务。
        """
        from app.llm import resolve_model_config
        judge_cfg = {k: v for k, v in (judge_config or {}).items() if v}
        provider = judge_cfg.get("provider") or settings.JUDGE_PROVIDER
        base_url = judge_cfg.get("base_url") or settings.JUDGE_BASE_URL
        api_key = judge_cfg.get("api_key") or settings.JUDGE_API_KEY
        model_id = judge_cfg.get("model_id") or settings.JUDGE_MODEL_ID
        has_env = any([settings.JUDGE_PROVIDER, settings.JUDGE_BASE_URL,
                       settings.JUDGE_API_KEY, settings.JUDGE_MODEL_ID])
        if not (judge_cfg or has_env):
            return None
        model_config = resolve_model_config(
            provider or "default", base_url or "", api_key or "", model_id or "")
        name = judge_cfg.get("name") or settings.JUDGE_NAME
        return Agent(
            agent_id="judge",
            name=name,
            role="独立辩论裁判",
            personality="客观、中立、严谨，不参与辩论",
            model_config=model_config,
            is_main=False,
            stance="NEUTRAL",
        )

    def get_agent(self, name):
        for a in self.agents:
            if a.name == name:
                return a
        return None

    # ---------------- 控制 ----------------
    def pause(self):
        if self.status == "running":
            self.status = "paused"
            self._pause_event.clear()
            _spawn_task(self.event_bus.publish(self._status_event()))

    def resume(self):
        if self.status == "paused":
            self.status = "running"
            self._pause_event.set()
            _spawn_task(self.event_bus.publish(self._status_event()))

    def end(self):
        if self.status in ("running", "paused"):
            self.status = "stopped"
            self._pause_event.set()
            _spawn_task(self.event_bus.publish(self._status_event()))

    # ---------------- 任务状态文本 ----------------
    def _task_status_text(self):
        if not self.tasks:
            return "（暂无任务）"
        lines = []
        for t in self.tasks:
            state = "✅已完成" if t["completed"] else "⏳进行中"
            lines.append(f"- [{state}] 任务#{t['id']} → {t['recipient']} | "
                         f"变量:{t['var_name']}\n  内容：{t['content']}\n  期望：{t['expected_outcome']}")
        return "\n".join(lines)

    def _log(self, text, level="info", event=""):
        """结构化日志（优化 A4）：同时写入控制台 JSON 日志并推送到前端。"""
        from app.logger import log, set_trace_id
        set_trace_id(self.trace_id)
        log(level, event or "orchestrator.event", text, trace_id=self.trace_id)
        _spawn_task(self.event_bus.publish({
            "type": "log", "text": f"[{self.trace_id}] {text}",
            "level": level, "event": event or "orchestrator.event",
        }))

    # ---------------- 单次 Agent 回合（含工具调用循环） ----------------
    async def _consume_stream(self, messages, model_config, tools, on_delta=None,
                              temperature=0.7, top_p=None):
        """
        在后台线程运行流式生成，把增量事件桥接回事件循环。

        返回完整结果 dict（与 chat() 一致：content / tool_calls / assistant_message / usage）。
        on_delta(delta: str) 为可选回调，每次收到文本增量时调用。
        整体超时受 settings.STREAM_TIMEOUT 约束（架构文档 §4.5 单步超时）。
        """
        q = asyncio.Queue()
        loop = asyncio.get_running_loop()
        t_start = time.time()
        metrics = {"ttft_s": None, "gen_s": 0.0, "chars": 0}

        def _run():
            try:
                for ev in chat_stream(messages, model_config, tools,
                                      temperature=temperature, top_p=top_p):
                    loop.call_soon_threadsafe(q.put_nowait, ev)
            except Exception as e:
                loop.call_soon_threadsafe(q.put_nowait, {"type": "error", "error": str(e)})

        threading.Thread(target=_run, daemon=True).start()

        async def _drain():
            first = True
            while True:
                ev = await q.get()
                t = ev.get("type")
                if t == "final":
                    # 附加流式性能实测：TTFT / 生成耗时 / 输出字符数（供模型质量评分）
                    if metrics["ttft_s"] is not None:
                        metrics["gen_s"] = max(0.0, time.time() - t_start - metrics["ttft_s"])
                        ev["result"]["metrics"] = dict(metrics)
                    return ev["result"]
                if t == "error":
                    raise RuntimeError(ev["error"])
                if t == "content":
                    if first and metrics["ttft_s"] is None:
                        metrics["ttft_s"] = time.time() - t_start
                        first = False
                    metrics["chars"] += len(ev["delta"])
                    if on_delta is not None:
                        await on_delta(ev["delta"])

        try:
            return await asyncio.wait_for(_drain(), timeout=settings.STREAM_TIMEOUT)
        except asyncio.TimeoutError:
            raise RuntimeError(f"流式响应超时（>{settings.STREAM_TIMEOUT}s）") from None

    async def _run_llm_retry(self, messages, model_config, tools=None, temperature=0.7,
                             top_p=None):
        """带指数退避重试的非流式 LLM 调用（架构文档 §4.5：重试 2 次，间隔 1s/3s）。"""
        delays = list(settings.LLM_RETRY_DELAYS)
        last_exc = None
        for attempt in range(max(1, settings.LLM_RETRY_ATTEMPTS)):
            try:
                return await asyncio.to_thread(
                    chat, messages, model_config, tools, temperature=temperature,
                    top_p=top_p)
            except Exception as e:
                last_exc = e
                if attempt >= len(delays):
                    break
                delay = delays[attempt]
                self._log(f"[LLM重试] 第{attempt+1}次调用失败({e})，{delay}s 后重试",
                          level="warning", event="llm.retry")
                await asyncio.sleep(delay)
        raise last_exc or RuntimeError("LLM 调用失败")

    def _apply_usage(self, result) -> int:
        """累加本次调用的 token 用量（只读遥测），返回本次 total_tokens。"""
        usage = result.get("usage") or {}
        total = usage.get("total_tokens")
        if not total:
            total = (usage.get("prompt_tokens") or 0) + (usage.get("completion_tokens") or 0)
        if total:
            self.spent += int(total)
        completion = usage.get("completion_tokens") or 0
        if completion:
            self.output_tokens += int(completion)
        return int(total) if total else 0

    async def _agent_turn(self, agent: Agent, user_msg: str, mode: str,
                          allow_delegate: bool = True, stream: bool = False):
        collaborators_meta = [{"name": c.name} for c in self.collaborators]
        sys_prompt = agent.system_prompt(
            collaborators_meta, self.shared_context, self._task_status_text(),
            mode, self.topic)
        messages = [{"role": "system", "content": sys_prompt},
                    *list(agent.history),
                    {"role": "user", "content": user_msg}]

        tools = agent.mcp.schemas()
        if agent.is_main and allow_delegate:
            tools = [*tools, DELEGATE_SCHEMA]
        elif (not agent.is_main) and mode == "task":
            tools = [*tools, REPORT_SCHEMA]

        content = ""
        delegate = []
        report = None
        token_cost = 0
        emitting = False  # 是否已开始向 UI 流式推送
        result = None
        # 公平性 L5：辩论发言统一采样参数（temperature / top_p），非辩论模式用默认
        samp = self.governor.sampling_params() if mode == "discussion" \
            else {"temperature": 0.7, "top_p": None}

        async def _emit_delta(delta: str):
            nonlocal emitting
            if not emitting:
                emitting = True
                await self.event_bus.publish({
                    "type": "speech_start",
                    "speaker": agent.name,
                    "round": self.round,
                    "is_main": agent.is_main,
                })
            await self.event_bus.publish({
                "type": "speech_delta",
                "speaker": agent.name,
                "delta": delta,
            })

        for _ in range(5):
            try:
                if stream:
                    try:
                        result = await self._consume_stream(
                            messages, agent.model_config, tools, _emit_delta,
                            temperature=samp["temperature"], top_p=samp["top_p"])
                    except Exception as e:
                        self._log(f"[LLM流式失败:{agent.name}] {e}，回退非流式",
                                  level="warning", event="llm.stream_fallback")
                        result = await self._run_llm_retry(
                            messages, agent.model_config, tools,
                            temperature=samp["temperature"], top_p=samp["top_p"])
                else:
                    result = await self._run_llm_retry(
                        messages, agent.model_config, tools,
                        temperature=samp["temperature"], top_p=samp["top_p"])
            except Exception as e:
                msg = f"[LLM错误:{agent.name}] {e}"
                self._log(msg, level="error", event="llm.error")
                # 把错误同步推送到前端（醒目的错误气泡），避免只在控制台可见
                await self.event_bus.publish({
                    "type": "agent_error",
                    "agent": agent.name,
                    "error": str(e),
                })
                break

            # 聚合流式性能实测（TTFT / 生成速率），供裁判模型质量评分使用
            _m = result.pop("metrics", None)
            if _m and _m.get("ttft_s") is not None:
                acc = self.agent_metrics.setdefault(
                    agent.name, {"calls": 0, "ttft_sum": 0.0, "gen_sum": 0.0, "chars": 0})
                acc["calls"] += 1
                acc["ttft_sum"] += _m["ttft_s"]
                acc["gen_sum"] += _m["gen_s"]
                acc["chars"] += _m["chars"]

            token_cost += self._apply_usage(result)  # 只读遥测：累计真实 token 用量
            content = result["content"] or content
            raw_msg = result.get("assistant_message")
            tcs = result["tool_calls"]

            if not tcs:
                break

            # 把 assistant 消息（含 tool_calls）加入上下文，维持多轮工具调用
            if raw_msg is not None:
                messages.append(raw_msg)

            special = False
            for tc in tcs:
                if agent.is_main and tc["name"] == "delegate_task":
                    delegate.append(tc["arguments"])
                    special = True
                elif (not agent.is_main) and tc["name"] == "report_result":
                    report = tc["arguments"]
                    special = True
                else:
                    result = await asyncio.to_thread(
                        agent.mcp.call, tc["name"], tc["arguments"])
                    messages.append({
                        "role": "tool",
                        "tool_call_id": tc["id"],
                        "content": result,
                    })
            if special:
                break
            if not tcs:
                break

        # 公平性 L1 兜底：discussion 模式下即使 LLM 输出超长也按公平性字符上限截断。
        # PRO/CON 用相对上限（对手近 N 轮有效输出均值 × 比例，与硬上限取 min）；
        # HOST/NEUTRAL 无对手参照，用硬性字符上限兜底（同样抑制整体消耗）。
        if mode == "discussion" and content:
            if agent.stance in ("PRO", "CON"):
                ceiling = self.governor.ceiling_for(
                    agent.stance, self.round,
                    self.governor.recent_opponent_chars(agent.stance))
            else:
                ceiling = self.governor.hard_char_ceiling
            truncated_content, was_truncated = self.governor.truncate_output(content, ceiling)
            if was_truncated:
                self._log(f"[公平性L1] {agent.name} 发言 {len(content)} 字，"
                          f"截断至 {len(truncated_content)} 字（上限 {ceiling} 字）",
                          level="warning", event="fairness.output_truncate")
            content = truncated_content
            # 记录输出统计（L1 相对参照基准 / L2 思考占比），仅统计本次最终输出
            think = result.get("think_chars", 0) if result else 0
            raw = result.get("raw_chars", len(content) + think) if result else len(content)
            self.governor.record_turn(
                agent.stance, self.round,
                visible_chars=max(0, raw - think),
                think_chars=think,
                truncated=was_truncated)

        # 流式收尾：补发 speech_end（前端用完整文本替换），并记录 transcript
        if emitting and content:
            await self.event_bus.publish({
                "type": "speech_end",
                "speaker": agent.name,
                "round": self.round,
                "text": content,
                "tokens": token_cost,          # 本次发言累计 token 用量（实时显示）
                "output_tokens": self.output_tokens,  # 全局累计输出 token（实时显示）
            })
            self.transcript.append({
                "type": "speech", "round": self.round, "speaker": agent.name,
                "is_main": agent.is_main, "text": content,
            })

        agent.append_history("user", user_msg, round_no=self.round)
        if content:
            agent.append_history("assistant", content, round_no=self.round)
        return {"content": content, "delegate": delegate, "report": report,
                "streamed": emitting, "token_cost": token_cost}

    # ---------------- 主 Agent 指令 ----------------
    def _main_directive(self, round, is_last, had_any_task, pending_exists):
        base = f"【讨论主题】{self.topic}\n【轮次】第 {round}/{self.max_rounds} 轮（辩论进行中）\n"
        if round == 1:
            base += ("这是辩论第 1 轮。请先为协作 Agent 分派调研 / 立论任务（delegate_task），"
                     "明确任务内容与期望效果，并提供接收结果的变量名(var_name)；"
                     "你也可以先陈述你的开场立场（作为主持人保持中立公正）。")
        else:
            base += (f"这是辩论第 {round} 轮。请基于各方上一轮发言与已回填的任务结果，"
                     "以主持人身份抛出疑问、要求补充论据、指出分歧或让某方进一步论证；"
                     "可通过 delegate_task 追加追问任务，或直接发言引发交锋。"
                     "请至少提出一个有针对性的质疑，推动多轮辩论。")
            if pending_exists:
                base += "\n（提示：仍存在未完成任务，请先审视其进展。）"
        return base

    # ---------------- 辩论上下文构建 ----------------
    def _record_argument(self, speaker: str, text: str, round_no: int):
        if not text:
            return
        self.round_arguments.setdefault(round_no, {})[speaker] = text
        self.latest_arguments[speaker] = {"text": text, "round": round_no}

    # ---------------- 对手定向反驳（需求 2/3） ----------------
    # PRO↔CON 立场互为对手；NEUTRAL/HOST 无固定对手
    OPPONENT_STANCE: ClassVar[dict[str, str]] = {"PRO": "CON", "CON": "PRO"}

    def _find_opponent(self, collab: Agent) -> Agent | None:
        """找到 collab 的立场对手：优先取上一轮对立场的最新发言人，空则回退全局最近。"""
        target = self.OPPONENT_STANCE.get(collab.stance)
        if target is None:
            return None
        # 先在上一轮找对立场最近发言人
        names = list((self.round_arguments.get(self.round - 1, {}) or {}).keys())
        for name in reversed(names):  # 末位 = 最近发言
            agent = self.get_agent(name)
            if agent is not None and agent.stance == target:
                return agent
        # 上一轮无对立场 → 回退全局最近发言
        for name in reversed(list((self.latest_arguments or {}).keys())):
            agent = self.get_agent(name)
            if agent is not None and agent.stance == target:
                return agent
        return None

    async def _prepare_opponent_context(self, collab: Agent) -> None:
        """为辩手准备对手上轮发言并写入 self._opponent_prep（由 _debate_prompt 注入）。

        - 第 2 轮：对手原文截断到 OPPONENT_SNIPPET_MAX_CHARS 字；
        - 第 3 轮起：对手发言 LLM 摘要到 OPPONENT_SUMMARY_MAX_CHARS 字
          （按 (对手,轮次) 缓存；usage 计入 self.spent 只读遥测）。
        """
        self._opponent_prep.pop(collab.name, None)
        if self.round < 2:
            return
        opp = self._find_opponent(collab)
        if opp is None:
            return
        round_no = self.round - 1
        text = (self.round_arguments.get(round_no, {}) or {}).get(opp.name, "")
        if not text:
            text = (self.latest_arguments.get(opp.name) or {}).get("text", "")
        if not text:
            return
        # 公平性 L3：记录本次反驳可见的对手历史轮数（PRO/CON 对称校验）
        self.governor.record_opponent_seen(collab.stance, round_no)
        if self.round == 2:
            brief = text[: settings.OPPONENT_SNIPPET_MAX_CHARS]
        else:
            key = (opp.name, round_no)
            if key not in self._opponent_summary_cache:
                summary, usage = await summarize_text(
                    opp.model_config, text,
                    max_chars=settings.OPPONENT_SUMMARY_MAX_CHARS)
                self._opponent_summary_cache[key] = summary
                total = (usage or {}).get("total_tokens") or 0
                if total:
                    self.spent += int(total)
                completion = (usage or {}).get("completion_tokens") or 0
                if completion:
                    self.output_tokens += int(completion)
                self._log(
                    f"[对手摘要] {collab.name} 引用 {opp.name} 第{round_no}轮发言"
                    f"（摘要 {len(summary)} 字）",
                    level="info", event="debate.opponent_summary")
            brief = self._opponent_summary_cache[key]
        self._opponent_prep[collab.name] = {
            "speaker": opp.name,
            "stance": STANCE_LABEL.get(opp.stance, opp.stance),
            "round": round_no,
            "text": brief,
        }

    def _evidence_for_round(self, round_no: int) -> str:
        items = self.round_evidence.get(round_no)
        if not items:
            return ""
        flat = []
        for v in items.values():
            flat.extend(v)
        return rag_service.evidence_text(flat)

    def _debate_prompt(self, collab: Agent) -> str:
        stance_label = STANCE_LABEL.get(collab.stance, collab.stance)
        persona = (f"你的角色「{collab.role}」、性格「{collab.personality}」"
                   if collab.role or collab.personality else "你的人物设定")
        parts = []
        if self.round == 1:
            # 立论阶段（需求 1：基于性格设定的单轮分析与回复）
            if collab.stance == "PRO":
                parts.append(f"这是第 1 轮立论。请作为「{stance_label}」就主题「{self.topic}」发表开场立论："
                             "包含核心观点、逻辑推导、事实依据（可调用 knowledge_search 检索）。"
                             "必须坚定维护正方立场。")
            elif collab.stance == "CON":
                parts.append(f"这是第 1 轮立论。请作为「{stance_label}」就主题「{self.topic}」发表开场立论："
                             "从质疑/反对角度提出核心观点、逻辑推导与事实依据（可调用 knowledge_search 检索）。"
                             "必须坚定维护反方立场。")
            else:
                parts.append(f"这是第 1 轮立论。请基于你的角色「{collab.role}」就主题「{self.topic}」发表开场观点。")
            parts.append(f"请以{persona}为出发点发表立论，保持人设一致。")
        else:
            # 反驳 / 辩护阶段：优先针对对手上轮发言（需求 2/3）
            opp_prep = self._opponent_prep.get(collab.name)
            if opp_prep:
                digest = ("摘要" if self.round >= 3 else "发言")
                parts.append(f"这是第 {self.round} 轮反驳/辩护阶段。你的对手「{opp_prep['speaker']}」"
                             f"（{opp_prep['stance']}）在第 {opp_prep['round']} 轮的{digest}如下：\n"
                             f"{opp_prep['text']}")
                parts.append(f"请以{persona}为出发点进行反驳：优先逐条回应对方发言中"
                             "与你立场相悖的核心观点，指出逻辑谬误（如滑坡谬误、稻草人）或提供反证"
                             "（可调用 knowledge_search 检索）。必须坚定维护你的「"
                             + stance_label + "」立场，禁止倒戈。")
            else:
                # 回退：无立场对手（NEUTRAL/多辩手场景）时沿用原「上一发言人」逻辑
                prev = None
                prev_stance = ""
                if (self.last_debate_speaker and self.last_debate_speaker != collab.name
                        and self.last_debate_speaker in self.latest_arguments):
                    prev = self.latest_arguments[self.last_debate_speaker]
                    prev_agent = self.get_agent(self.last_debate_speaker)
                    if prev_agent:
                        prev_stance = STANCE_LABEL.get(prev_agent.stance, prev_agent.stance)
                if prev:
                    brief = (f"【上一位发言人：{self.last_debate_speaker}"
                             f"（{prev_stance or '未声明立场'}）】（第{prev['round']}轮）：\n"
                             f"{prev['text'][:500]}")
                    parts.append(f"这是第 {self.round} 轮反驳/辩护阶段。上一发言人「{self.last_debate_speaker}」"
                                 f"的立场为「{prev_stance or '未声明立场'}」，与你作为「{stance_label}」的立场"
                                 "存在分歧。以下是其最新发言：\n" + brief)
                    parts.append(f"请以{persona}为出发点进行反驳：优先逐条回应对方发言中"
                                 "与你立场相悖的核心观点，指出逻辑谬误（如滑坡谬误、稻草人）或提供反证"
                                 "（可调用 knowledge_search 检索）。必须坚定维护你的「"
                                 + stance_label + "」立场，禁止倒戈。")
                else:
                    brief = f"主持人最新发言：{self.last_main_message}"
                    parts.append(f"这是第 {self.round} 轮反驳/辩护阶段。以下是最新交锋内容：\n{brief}")
                    parts.append(f"请以{persona}为出发点，针对上述观点进行反驳或辩护："
                                 "直接回应对方核心论据，指出逻辑谬误（如滑坡谬误、稻草人）或提供反证"
                                 "（可调用 knowledge_search 检索）。必须坚定维护你的「"
                                 + stance_label + "」立场，禁止倒戈。")
        # RAG 证据注入（上一轮核查结果）
        ev = self._evidence_for_round(self.round - 1)
        if ev:
            parts.append(ev)
        # 公平性 L1：相对上限（对手近 N 轮有效输出均值 × 比例，与硬上限取 min）。
        # 仅作提示约束生成，超限由 _agent_turn 事后截断兜底（不再把 max_tokens 下发 API）
        limit = self.governor.ceiling_for(
            collab.stance, self.round,
            self.governor.recent_opponent_chars(collab.stance))
        parts.append(f"【输出要求】你的发言请控制在 {limit} 字以内，输出完即止，不要复述历史。")
        parts.append("请以第一人称输出你的发言，不要输出任何 JSON。")
        return "\n\n".join(parts)

    # ---------------- 协作 Agent 回合 ----------------
    def _debate_order(self) -> list[Agent]:
        """严格交替的辩手发言顺序：正方/反方轮流，NEUTRAL 等非正反方辩手排在末尾。"""
        pros = [c for c in self.collaborators if c.stance == "PRO"]
        cons = [c for c in self.collaborators if c.stance == "CON"]
        others = [c for c in self.collaborators if c.stance not in ("PRO", "CON")]
        order, n = [], max(len(pros), len(cons))
        for i in range(n):
            if i < len(pros):
                order.append(pros[i])
            if i < len(cons):
                order.append(cons[i])
        order.extend(others)
        return order

    async def _collab_turn(self, collab: Agent):
        pending = [t for t in self.tasks
                   if not t["completed"] and t["recipient"] == collab.name]
        if pending:
            for task in pending:
                umsg = (f"你收到任务#{task['id']}：\n内容：{task['content']}\n"
                        f"期望效果：{task['expected_outcome']}\n"
                        f"请将结果回填到变量 `{task['var_name']}`。")
                # 优化 A8：任务协作模式同样启用流式输出，前端按 speaker 并行展示
                out = await self._agent_turn(collab, umsg, mode="task", stream=True)
                if out["report"]:
                    report = out["report"]
                    self._apply_report(task, report)
                    msg = report.get("message", "")
                    if msg:
                        # 流式已展示过程发言；未流式（空内容/回退路径）时才补发言气泡
                        if not out["streamed"]:
                            await self._emit_speech(collab, msg)
                        self._record_argument(collab.name, msg, self.round)
                    await self.event_bus.publish({
                        "type": "task_result",
                        "from": collab.name,
                        "var": task["var_name"],
                        "value": report.get("var_value", ""),
                        "completed": bool(report.get("completed", False)),
                        "message": msg,
                        "task_id": task["task_id"],
                        "timestamp": now_ms(),
                        "token_cost": out.get("token_cost", 0),
                    })
                else:
                    # 未回报，记录其发言（若有）
                    if out["content"]:
                        if not out["streamed"]:
                            await self._emit_speech(collab, out["content"])
                        self._record_argument(collab.name, out["content"], self.round)
                    task["rounds_waiting"] = task.get("rounds_waiting", 0) + 1
                    if task["rounds_waiting"] >= 3:
                        task["completed"] = True
                        if task["var_name"] in self.shared_context:
                            self.shared_context[task["var_name"]]["value"] = "（超时未完成）"
        # 辩论环节（立论 / 反驳）：无论是否处理过任务，辩手每轮都发言，保证严格交替
        await self._prepare_opponent_context(collab)  # 需求 2/3：注入对手上轮发言（截断/摘要）
        self.governor.record_speech(collab.stance, self.round)  # 公平性 L4：每模型每轮至多发言一次
        umsg = self._debate_prompt(collab)
        out = await self._agent_turn(collab, umsg, mode="discussion", stream=True)
        if out["content"]:
            if not out["streamed"]:
                await self._emit_speech(collab, out["content"])
            self._record_memory(collab, out["content"])
            self._record_argument(collab.name, out["content"], self.round)
            self.last_debate_speaker = collab.name

    # ---------------- RAG 事实核查（架构文档 §2.5 / §4.2） ----------------
    async def _rag_check_round(self):
        texts = list(self.round_arguments.get(self.round, {}).values())
        if not texts:
            return
        combined = "\n".join(texts)
        try:
            evidence = await asyncio.to_thread(rag_service.check, combined, 3)
        except Exception as e:
            self._log(f"[RAG核查失败] {e}", level="warning", event="rag.fail")
            return
        if evidence:
            self.round_evidence[self.round] = {"default": evidence}
            self._log(f"[RAG] 第{self.round}轮事实核查返回 {len(evidence)} 条证据",
                      level="info", event="rag.check")

    # ---------------- 收敛检测（架构文档 §4.4） ----------------
    async def _check_convergence(self) -> bool:
        if self.round < 2:
            return False
        cur = " ".join(self.round_arguments.get(self.round, {}).values())
        prev = " ".join(self.round_arguments.get(self.round - 1, {}).values())
        if not cur or not prev:
            return False
        try:
            import numpy as np

            from app.embeddings import embed_query
            v_cur = np.array(embed_query(cur))
            v_prev = np.array(embed_query(prev))
            sim = float(np.dot(v_cur, v_prev) /
                        (np.linalg.norm(v_cur) * np.linalg.norm(v_prev) + 1e-9))
        except Exception:
            return False
        # 新证据检测：本轮 RAG 若返回了新证据，则不视为无效循环
        has_new_evidence = bool(self.round_evidence.get(self.round))
        if sim >= settings.CONVERGENCE_SIMILARITY and not has_new_evidence:
            self.convergence_streak += 1
            self._log(f"[收敛检测] 第{self.round}轮与上一轮相似度={sim:.3f} "
                      f"（连续{self.convergence_streak}轮）",
                      level="info", event="convergence.check")
        else:
            self.convergence_streak = 0
        return self.convergence_streak >= settings.CONVERGENCE_ROUNDS

    # ---------------- 公平性治理报告（替代旧的 budget planner） ----------------
    def _log_fairness_report(self, prefix: str = "") -> dict:
        """发布公平性治理报告（各层状态 / 违规 / 警告），用于日志与状态同步。"""
        rep = self.governor.report()
        lines = [f"{prefix}公平性治理报告："]
        for layer in rep["layers"]:
            lines.append(f"  {layer['layer']} {layer['name']}: {layer['status']} — {layer['detail']}")
        self._log("\n".join(lines), level="info", event="fairness.report")
        return rep

    # ---------------- 讨论主循环 ----------------
    async def run_discussion(self, topic: str = "", max_rounds: int | None = None):
        if self.status == "running":
            return
        if max_rounds is not None:
            self.max_rounds = max(1, int(max_rounds))
        if topic:
            self.topic = topic
        self.trace_id = new_trace_id()
        self.spent = 0                  # 累计 token 用量（只读遥测）
        self.output_tokens = 0          # 累计输出 token（实时显示，只读遥测）
        from app.logger import set_trace_id
        set_trace_id(self.trace_id)
        self.phase = "preparing"
        self._started_at = time.time()
        self.round = 0
        self.convergence_streak = 0
        self.round_arguments = {}
        self.latest_arguments = {}
        self.round_evidence = {}
        self.judgment = None
        self.last_debate_speaker = None
        # 对手定向反驳（需求 2/3）：collab.name -> {speaker, stance, round, text}；
        # 摘要按 (对手,轮次) 缓存，避免同轮多个辩手引用同一对手重复调用 LLM
        self._opponent_prep = {}
        self._opponent_summary_cache = {}
        self.run_stats = {"compressions": 0, "started_at": time.time()}
        self.agent_metrics = {}
        # 公平性治理：每场全新实例（替代旧的预算规划器状态；token 只读遥测见 __init__）
        self.governor = FairnessGovernor()
        self.status = "running"
        self._pause_event.set()
        await self.event_bus.publish({
            "type": "status", "status": "running", "phase": self.phase,
            "topic": self.topic, "max_rounds": self.max_rounds,
            "trace_id": self.trace_id,
            "agents": [{"name": a.name, "role": a.role, "is_main": a.is_main,
                        "stance": a.stance} for a in self.agents],
        })
        await self.event_bus.publish(self._status_sync())

        had_any_task = False
        self.last_main_message = self.topic

        try:
            while self.round < self.max_rounds:
                # 暂停 / 结束 控制
                await self._pause_event.wait()
                if self.status == "stopped":
                    break

                self.round += 1
                self.phase = "debating"
                self._opponent_prep.clear()  # 每轮重新准备对手上轮发言上下文
                is_last = (self.round >= self.max_rounds)
                # 公平性治理：每轮发布各层状态（替代旧的预算规划）
                self._log_fairness_report(f"[第{self.round}轮] ")
                await self.event_bus.publish({
                    "type": "round", "round": self.round, "max": self.max_rounds,
                })
                await self.event_bus.publish(self._status_sync())

                pending_exists = any(not t["completed"] for t in self.tasks)
                had_any_task = had_any_task or len(self.tasks) > 0

                # 1) 主持人回合（第1轮分派任务，之后轮次抛疑追问）
                directive = self._main_directive(self.round, is_last, had_any_task, pending_exists)
                main_out = await self._agent_turn(self.main, directive, mode="discussion", stream=True)
                if main_out["content"]:
                    if not main_out["streamed"]:
                        await self._emit_speech(self.main, main_out["content"])
                    self.last_main_message = main_out["content"]
                for d in main_out["delegate"]:
                    task = self._create_task(d)
                    if task is None:
                        continue
                    await self.event_bus.publish({
                        "type": "task_assigned",
                        "from": self.main.name,
                        "to": task["recipient"],
                        "content": task["content"],
                        "expected": task["expected_outcome"],
                        "var": task["var_name"],
                        "task_id": task["task_id"],
                        "timestamp": task["timestamp"],
                    })
                self._record_memory(self.main, main_out["content"])
                self._record_argument(self.main.name, main_out["content"], self.round)

                # 2) 协作 Agent 回合（严格交替：正方/反方轮流发言，其余立场排后）
                if self.round == 1:
                    # 第 1 轮并行立论（架构文档 §4.2 / §6.2）
                    await asyncio.gather(
                        *[self._collab_turn(c) for c in self.collaborators],
                        return_exceptions=True,
                    )
                    # 确定第 2 轮首位发言人的"上一发言人"（交替顺序的末位）
                    order = self._debate_order()
                    if order:
                        self.last_debate_speaker = order[-1].name
                else:
                    # 第 2 轮起严格交替串行发言：正/反方轮流，其余立场最后
                    for c in self._debate_order():
                        await self._collab_turn(c)

                # 3) RAG 事实核查（注入下一轮上下文）
                await self._rag_check_round()

                # 4) 记忆转化与遗忘
                await self._memory_housekeeping()

                # 5) 收敛检测（架构文档 §4.3/4.4）
                if await self._check_convergence():
                    self._log(f"收敛检测触发：连续{self.convergence_streak}轮观点高度相似"
                              "且无新证据，提前进入裁决",
                              level="info", event="convergence.trigger")
                    break

                # 6) 达到最大轮次则结束（公平性治理下无预算熔断；轮次对称性由 L4 保证）
                if is_last or self.round >= self.max_rounds:
                    break

                await self.event_bus.publish(self._status_sync())
                await asyncio.sleep(0)

            # ---- Judging：终止后单次调用裁判（架构文档 §4.1/§6.2） ----
            if self.status == "stopped":
                # 手动结束（end()）：跳过裁判裁决与主持人汇总，直接收尾，
                # 避免旧 run 任务在重新配置/新讨论启动后继续污染编排器状态
                self.phase = "idle"
                await self.event_bus.publish({"type": "status", "status": "stopped", "phase": self.phase})
                await self.event_bus.publish(self._status_sync())
            else:
                self.phase = "judging"
                await self.event_bus.publish({"type": "status", "status": "running", "phase": self.phase})
                judgment = await self._judge_round()
                self.judgment = judgment
                await self.event_bus.publish({
                    "type": "judgment",
                    "judge": self.judge.name if self.judge else self.main.name,
                    "scores": judgment.scores,
                    "agent_scores": judgment.agent_scores,
                    "model_performance": judgment.model_performance,
                    "composite_scores": judgment.composite_scores,
                    "winner": judgment.winner,
                    "critique": judgment.critique,
                    "convergence_flag": judgment.convergence_flag,
                    "round": self.round,
                })
                await self.event_bus.publish(self._status_sync())

                # ---- Finished：主持人汇总最终报告（一律用 LLM 完成；公平性治理下无预算熔断） ----
                self.phase = "finished"
                summary = await self._final_summary(judgment)
                await self._emit_summary(summary)
                self.status = "done"
                await self.event_bus.publish({"type": "status", "status": "done", "phase": self.phase})
                await self.event_bus.publish(self._status_sync())
        except Exception as e:
            self._log(f"[编排器错误] {e}", level="error", event="orchestrator.error")
            self.status = "stopped"
            self.phase = "idle"  # 异常熔断回到 Idle（架构文档 §4.1）
            await self.event_bus.publish({"type": "status", "status": "stopped", "phase": self.phase})

    # ---------------- Judge 裁决（架构文档 §2.4） ----------------
    def _build_debate_history_text(self) -> str:
        lines = []
        for e in self.transcript:
            if e["type"] == "speech":
                lines.append(f"[第{e['round']}轮] {e['speaker']}：{e['text']}")
        return "\n\n".join(lines) if lines else "（无有效发言记录）"

    def _build_judge_debate_text(self) -> str:
        """构建供独立裁判阅读的辩论记录：仅含辩手（协作 Agent）发言。

        主持人的发言（含任务分派 / 追问等）一律剔除，裁判只依据辩手立论、
        辩护与交锋内容进行辩论质量评分，不受主持人任务管理内容影响。
        """
        lines = []
        for e in self.transcript:
            if e["type"] == "speech" and not e.get("is_main"):
                lines.append(f"[第{e['round']}轮] {e['speaker']}：{e['text']}")
        return "\n\n".join(lines) if lines else "（无辩手发言记录）"

    @staticmethod
    def _extract_json(text: str) -> dict:
        """从 LLM 输出中提取 JSON 对象（容忍代码块与前后缀）。"""
        if not text:
            raise ValueError("裁判返回空内容")
        t = text.strip()
        if t.startswith("```"):
            t = re.sub(r"^```[a-zA-Z]*\s*", "", t)
            t = re.sub(r"\s*```$", "", t)
        start, end = t.find("{"), t.rfind("}")
        if start == -1 or end == -1 or end <= start:
            raise ValueError("裁判输出中未找到 JSON 对象")
        return json.loads(t[start:end + 1])

    # ---------------- 模型质量评分（系统确定性，非 LLM） ----------------
    @staticmethod
    def _score_ttft(ttft_s: float) -> float:
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

    @staticmethod
    def _score_rate(rate: float) -> float:
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

    def _model_performance(self) -> dict:
        """聚合各辩手流式实测指标，产出 {辩手: {avg_ttft_ms, avg_gen_rate, performance_score}}。"""
        perf = {}
        for name, m in self.agent_metrics.items():
            if m["calls"] <= 0:
                continue
            avg_ttft = m["ttft_sum"] / m["calls"]
            avg_rate = m["chars"] / m["gen_sum"] if m["gen_sum"] > 0 else 0.0
            perf[name] = {
                "avg_ttft_ms": round(avg_ttft * 1000, 1),
                "avg_gen_rate": round(avg_rate, 1),
                "performance_score": round(
                    (self._score_ttft(avg_ttft) + self._score_rate(avg_rate)) / 2, 1),
            }
        return perf

    def _performance_text(self) -> str:
        """供裁判提示词/评语参考的模型性能指标文本。"""
        perf = self._model_performance()
        if not perf:
            return ""
        lines = [f"- {n}：平均TTFT={v['avg_ttft_ms']}ms，平均生成速率={v['avg_gen_rate']}字/s"
                 for n, v in perf.items() if n != self.main.name]
        return "\n".join(lines) if lines else ""

    def _compute_composite(self, judgment: Judgment) -> Judgment:
        """计算全局综合得分、模型质量分与最终综合分，并依据内容分推导胜方。

        - 全局综合得分 scores：后端按各辩手四维分数取均值（确定性计算，不再依赖 LLM 输出），
          避免"综合得分 == 某辩手得分"的照抄问题。
        - composite = 内容均分(agent_scores 四维均值) × 内容权重 + 模型质量分 × 性能权重（仅展示）。
        - 胜方仅由内容均分决定（纯内容判定）；模型质量分不参与胜负。
        - agent_scores 缺失（如裁判调用失败回退）时 composite 退化为模型质量分，胜方为 DRAW。
        """
        names = [a.name for a in self.collaborators]
        perf = self._model_performance()
        judgment.model_performance = {
            n: perf.get(n, {"avg_ttft_ms": None, "avg_gen_rate": None,
                            "performance_score": 5.0})
            for n in names
        }
        # 全局综合得分 = 各辩手该维度分数均值
        if judgment.agent_scores:
            global_scores = {}
            for dim in ("logic", "evidence", "rebuttal", "clarity"):
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
            top_stances = {self.get_agent(n).stance for n in top_names}
            if len(top_stances) == 1 and top_stances <= {"PRO", "CON"}:
                judgment.winner = top_stances.pop()
            else:
                judgment.winner = "DRAW"
        else:
            judgment.winner = "DRAW"
        return judgment

    async def _judge_round(self) -> Judgment:
        if not self.collaborators:
            # 无辩手可评（仅主持人）：直接回退确定性裁决
            return Judgment(winner="DRAW", critique="无可评分的辩手（仅有主持人）",
                            convergence_flag=False)
        history = self._build_judge_debate_text()
        stance_lines = "\n".join(
            f"- {a.name}（{a.role}）立场：{STANCE_LABEL.get(a.stance, a.stance)}"
            for a in self.collaborators)
        names = [a.name for a in self.collaborators]
        # 示例使用不同的数值，避免 LLM 照抄同一组分数（few-shot 锚定）；仅作格式展示
        _example_pool = [
            {"logic": 8, "evidence": 7, "rebuttal": 9, "clarity": 8},
            {"logic": 7, "evidence": 6, "rebuttal": 8, "clarity": 7},
            {"logic": 9, "evidence": 8, "rebuttal": 7, "clarity": 8},
        ]
        agent_scores_example = "{" + ", ".join(
            f'"{n}": {json.dumps(_example_pool[i % len(_example_pool)])}'
            for i, n in enumerate(names)) + "}"
        judge_sys = ("你是独立辩论裁判，不参与辩论，也不接收主持人的任务分派信息。"
                     "你保持绝对中立，仅基于各位辩手的辩论发言进行多维度评分。"
                     "评分维度（1-10）：逻辑自洽性(logic)、证据效力(evidence)、"
                     "反驳有效性(rebuttal)、表达清晰度(clarity)。"
                     "请对【每位辩手】分别独立打分，再给出全场胜方。\n"
                     + self.governor.judge_guidance())
        prompt = (f"辩论主题：{self.topic}\n\n辩手与立场：\n{stance_lines}\n\n"
                  f"辩论记录（仅辩手发言，不含主持人的任务分派）：\n{history}\n\n")
        perf_text = self._performance_text()
        if perf_text:
            prompt += ("模型性能实测（系统测量，仅供你评语时参考，不参与你的内容评分）：\n"
                       f"{perf_text}\n\n")
        prompt += (
                  "请输出 JSON（不要输出其他文字或代码块）：\n"
                  f'{{"agent_scores": {agent_scores_example}, '
                  '"winner": "PRO" | "CON" | "DRAW", "critique": "详细评语", '
                  '"convergence_flag": false}\n'
                  "agent_scores 的键必须与上方辩手名称完全一致，每位辩手都需给出 4 个维度分数；"
                  "注意：示例中的数字仅为格式展示，禁止照抄，请根据每位辩手的实际发言独立评分，"
                  "不同辩手的分数可以（也应当）不同；"
                  "winner 取内容得分更优的一方；若势均力敌取 DRAW。"
                  "convergence_flag 表示辩论是否因观点收敛而提前终止。")
        judge_name = self.judge.name if self.judge else self.main.name
        judge_model = self.judge.model_config if self.judge else self.main.model_config
        jp = self.governor.judge_params()  # L5：辅助 LLM（裁判）统一采样参数
        try:
            result = await self._run_llm_retry(
                [{"role": "system", "content": judge_sys},
                 {"role": "user", "content": prompt}],
                judge_model, tools=None, temperature=jp["temperature"], top_p=jp["top_p"])
            self._apply_usage(result)  # 只读遥测：累计 token 用量，不做任何约束
            data = self._extract_json(result["content"])
            judgment = Judgment(**data)
            # 系统确定性模型质量分 + 综合评分，并据此推导胜方
            judgment = self._compute_composite(judgment)
        except Exception as e:
            self._log(f"[裁判调用失败] {e}", level="error", event="judge.fail")
            judgment = self._compute_composite(Judgment(
                winner="DRAW", critique=f"裁判调用失败：{e}",
                convergence_flag=False))
        self._log(f"[裁判:{judge_name}] winner={judgment.winner} "
                  f"agent_scores={json.dumps(judgment.agent_scores, ensure_ascii=False)} "
                  f"composite={json.dumps(judgment.composite_scores, ensure_ascii=False)}",
                  level="info", event="judge.result")
        return judgment

    # ---------------- 任务与变量 ----------------
    def _create_task(self, d: dict):
        recipient = d.get("recipient", "")
        # P15：校验接收方存在，避免 _apply_report 中 get_agent 返回 None
        if recipient and self.get_agent(recipient) is None:
            self._log(f"[任务分派忽略] 接收方 {recipient} 不存在于当前席位",
                      level="warning", event="task.ignore")
            return None
        var_name = d.get("var_name") or f"var_{self.task_counter+1}"
        self.task_counter += 1
        tid = self.task_counter
        task = {
            "id": tid,
            "task_id": new_uuid(),          # 协议字段：UUID 全局唯一
            "recipient": recipient,
            "content": d.get("content", ""),
            "expected_outcome": d.get("expected_outcome", ""),
            "var_name": var_name,
            "completed": False,
            "result_value": "",
            "result_message": "",
            "rounds_waiting": 0,
            "timestamp": now_ms(),
            "token_cost": 0,
        }
        self.tasks.append(task)
        self.shared_context[var_name] = {
            "by": self.main.name, "value": "", "task_id": tid,
        }
        return task

    def _apply_report(self, task, report: dict):
        var = report.get("var_name") or task["var_name"]
        val = report.get("var_value", "")
        task["completed"] = bool(report.get("completed", False))
        task["result_value"] = val
        task["result_message"] = report.get("message", "")
        task["token_cost"] = int(report.get("token_cost", 0) or 0)
        if var in self.shared_context:
            self.shared_context[var]["value"] = val
        else:
            self.shared_context[var] = {"by": self.main.name, "value": val, "task_id": task["id"]}
        # 协作 Agent 把结果沉淀进长期记忆
        agent = self.get_agent(task["recipient"])
        if agent is not None:
            agent.memory.add_long(
                f"完成任务#{task['id']}({var}): {val}", importance=0.85)
        else:
            self._log(f"[回报落库] 接收方 {task['recipient']} 已不存在，跳过记忆沉淀",
                      level="warning", event="report.skip")

    # ---------------- 记忆 ----------------
    def _record_memory(self, agent: Agent, text: str):
        if text:
            agent.memory.add_short(f"[{self.topic}] {text}", importance=0.5)

    async def _memory_housekeeping(self):
        for a in self.agents:
            try:
                a.memory.promote_short_to_long(threshold=0.7)
                if self.round % 2 == 0:
                    await a.memory.summarize_short_to_context(
                        self._memory_summarizer(a))
                a.memory.forget()
                # 长对话管理（优化 A9）：轮次边界触发历史摘要压缩，并计入预算
                usage = await a.compress_history()
                if usage:
                    total = usage.get("total_tokens") or 0
                    if total:
                        self.spent += int(total)
                        self.run_stats["compressions"] = \
                            self.run_stats.get("compressions", 0) + 1
                    completion = usage.get("completion_tokens") or 0
                    if completion:
                        self.output_tokens += int(completion)
            except Exception:
                pass

    def _memory_summarizer(self, agent: Agent):
        """构造「短期 → 情境」记忆的 LLM 摘要器（async 闭包），usage 计入预算。"""
        async def _summarize(texts: list[str]) -> str:
            summary, usage = await summarize_text(
                agent.model_config, "\n".join(texts),
                max_chars=settings.MEMORY_SUMMARY_MAX_CHARS)
            total = (usage or {}).get("total_tokens") or 0
            if total:
                self.spent += int(total)
                self.run_stats["memory_summaries"] = \
                    self.run_stats.get("memory_summaries", 0) + 1
            completion = (usage or {}).get("completion_tokens") or 0
            if completion:
                self.output_tokens += int(completion)
            return summary or ""
        return _summarize

    # ---------------- 控制状态事件（暂停/继续/结束 后推送给前端） ----------------
    def _status_event(self) -> dict:
        ev = self._status_sync()
        ev.update({
            "type": "status",
            "status": self.status,
            "topic": self.topic,
            "round": self.round,
            "max_rounds": self.max_rounds,
            "agents": [{"name": a.name, "role": a.role, "is_main": a.is_main,
                        "stance": a.stance} for a in self.agents],
        })
        return ev

    # ---------------- 状态同步（架构文档 §3.5） ----------------
    def _status_sync(self) -> dict:
        fair = self.governor.report()
        return {
            "type": "status_sync",
            "trace_id": self.trace_id,
            "phase": self.phase,
            "current_round": self.round,
            "agent_states": {a.name: "IDLE" for a in self.agents},
            # 只读遥测：token 用量不再做任何约束，仅保留观测（实时显示）
            "spent_tokens": self.spent,            # 累计 token 用量（输入+输出）
            "output_tokens": self.output_tokens,   # 累计输出 token（实时）
            # 公平性治理报告（替代旧的 budget_plan 视图）
            "fairness": fair,
            "elapsed_time_ms": int((time.time() - self._started_at) * 1000)
            if self._started_at else 0,
        }

    # ---------------- 输出 ----------------
    async def _emit_speech(self, agent: Agent, text: str):
        if not text:
            return
        self.transcript.append({
            "type": "speech", "round": self.round, "speaker": agent.name,
            "is_main": agent.is_main, "text": text,
        })
        await self.event_bus.publish({
            "type": "speech", "round": self.round, "speaker": agent.name,
            "is_main": agent.is_main, "text": text,
        })

    async def _emit_summary(self, text: str):
        self.transcript.append({
            "type": "summary", "round": self.round, "speaker": self.main.name,
            "is_main": True, "text": text,
        })
        await self.event_bus.publish({
            "type": "summary", "round": self.round, "speaker": self.main.name, "text": text,
        })

    async def _final_summary(self, judgment=None):
        judge_section = ""
        if judgment:
            judge_section = (
                "\n\n【裁判裁决】\n"
                f"- 综合得分：{json.dumps(judgment.scores, ensure_ascii=False)}\n"
                f"- 辩手评分：{json.dumps(judgment.agent_scores, ensure_ascii=False)}\n")
            if judgment.composite_scores:
                judge_section += (
                    f"- 综合评分（内容×{settings.JUDGE_CONTENT_WEIGHT} + "
                    f"模型质量×{settings.JUDGE_PERFORMANCE_WEIGHT}）："
                    f"{json.dumps(judgment.composite_scores, ensure_ascii=False)}\n")
            judge_section += (
                f"- 胜方：{judgment.winner}\n"
                f"- 评语：{judgment.critique}")
        directive = ("所有 Agent 的任务已结束或已达最大轮次。请作为主持人，"
                     "综合各方发言、任务结果与裁判裁决，输出一份结构化的最终讨论报告"
                     "（使用 markdown，含：核心结论、各方立场与论点摘要、裁判裁决、待办/分歧）。"
                     "请直接输出总结文本，不要再次分派任务。") + judge_section
        out = await self._agent_turn(self.main, directive, mode="discussion", allow_delegate=False)
        return out["content"] or "（主 Agent 未生成总结）"

    def _fallback_summary(self, judgment=None) -> str:
        """确定性汇总：不调用 LLM，直接基于发言记录与裁决拼装报告（作为系统兜底）。"""
        lines = []
        lines.append("## 最终讨论报告（系统确定性汇总）\n")
        lines.append(f"- 主题：{self.topic}")
        lines.append(f"- 实际轮次：{self.round}/{self.max_rounds}")
        lines.append(f"- Token 用量（只读遥测）：累计 {self.spent} / 输出 {self.output_tokens}")
        lines.append("")
        lines.append("### 核心观点摘要\n")
        for e in self.transcript:
            if e["type"] == "speech":
                snippet = e["text"][:200] + ("…" if len(e["text"]) > 200 else "")
                lines.append(f"**{e['speaker']}**（第{e['round']}轮）：{snippet}\n")
        if judgment:
            lines.append("### 裁判裁决\n")
            lines.append(f"- 胜方：{judgment.winner}")
            lines.append(f"- 得分：{json.dumps(judgment.scores, ensure_ascii=False)}")
            if judgment.agent_scores:
                lines.append(f"- 辩手评分：{json.dumps(judgment.agent_scores, ensure_ascii=False)}")
            if judgment.composite_scores:
                lines.append(f"- 综合评分（内容×{settings.JUDGE_CONTENT_WEIGHT} + "
                             f"模型质量×{settings.JUDGE_PERFORMANCE_WEIGHT}）："
                             f"{json.dumps(judgment.composite_scores, ensure_ascii=False)}")
            if judgment.critique:
                lines.append(f"- 评语：{judgment.critique}")
            lines.append("")
        lines.append("> 注：本报告由系统按发言记录确定性生成（未调用主持人 LLM 汇总），"
                     "作为公平性治理下的兜底路径保留。")
        return "\n".join(lines)

    # ---------------- 导出 Markdown ----------------
    def export_markdown(self) -> str:
        lines = []
        lines.append("# 多 Agent 辩论记录\n")
        lines.append(f"- 主题：{self.topic}")
        lines.append(f"- 轮次：{self.round}/{self.max_rounds}")
        lines.append(f"- 状态：{self.status} / 阶段：{self.phase}")
        lines.append(f"- Trace ID：{self.trace_id}")
        lines.append(f"- Token 用量（只读遥测）：累计 {self.spent} / 输出 {self.output_tokens}")
        lines.append(f"- 时间：{time.strftime('%Y-%m-%d %H:%M:%S')}")
        lines.append("- 参与 Agent：")
        for a in self.agents:
            tag = "（主）" if a.is_main else ""
            lines.append(f"  - {a.name}{tag} | 角色：{a.role} | 立场："
                         f"{STANCE_LABEL.get(a.stance, a.stance)} | "
                         f"模型：{a.model_config['model_id']} @ {a.model_config['base_url']}")
        if self.judge:
            lines.append(f"- 独立裁判：{self.judge.name} | "
                         f"模型：{self.judge.model_config['model_id']} @ "
                         f"{self.judge.model_config['base_url']}")
        lines.append("")
        lines.append("## 运行摘要\n")
        elapsed = (time.time() - self.run_stats.get("started_at", time.time())) \
            if self.run_stats.get("started_at") else 0.0
        conv_flag = self.convergence_streak >= settings.CONVERGENCE_ROUNDS
        done_tasks = sum(1 for t in self.tasks if t["completed"])
        conv_text = f"是（连续{self.convergence_streak}轮相似）" if conv_flag else "否"
        lines.append(f"- 持续时间：{elapsed:.1f} 秒")
        lines.append(f"- 实际轮次：{self.round}/{self.max_rounds}")
        lines.append(f"- 收敛触发：{conv_text}")
        lines.append(f"- RAG 证据轮数：{len(self.round_evidence)}")
        lines.append(f"- 任务：{len(self.tasks)}（完成 {done_tasks}）")
        lines.append(f"- 历史压缩次数：{self.run_stats.get('compressions', 0)}")
        lines.append(f"- Token 用量（只读遥测）：累计 {self.spent} / 输出 {self.output_tokens}"
                     f"（不再作为约束）")
        # 公平性治理报告（替代旧的预算规划摘要）
        fair = self.governor.report()
        lines.append(f"- 公平性治理：{'全部通过' if fair['ok'] else '存在违约'}"
                     f"（{len(fair['warnings'])} 警告 / {len(fair['violations'])} 违约）")
        for layer in fair["layers"]:
            lines.append(f"  - {layer['layer']} {layer['name']}：{layer['status']} — {layer['detail']}")
        lines.append("")
        lines.append("## 发言记录\n")
        for e in self.transcript:
            if e["type"] == "speech":
                lines.append(f"**{e['speaker']}**（第{e['round']}轮）：\n\n{e['text']}\n")
            elif e["type"] == "summary":
                lines.append(f"## 最终讨论结果（{e['speaker']} 整理）\n\n{e['text']}\n")
        if self.judgment:
            lines.append("## 裁判裁决\n")
            j = self.judgment
            lines.append(f"- 得分：{json.dumps(j.scores, ensure_ascii=False)}")
            if j.agent_scores:
                lines.append("- 辩手评分：")
                for name, sc in j.agent_scores.items():
                    lines.append(f"  - {name}：{json.dumps(sc, ensure_ascii=False)}")
            lines.append(f"- 胜方：{j.winner}")
            lines.append(f"- 收敛终止：{'是' if j.convergence_flag else '否'}")
            lines.append(f"- 评语：{j.critique}")
            lines.append("")
        if self.judgment and self.judgment.model_performance:
            lines.append("## 模型性能与综合评分\n")
            lines.append("| 辩手 | 平均TTFT | 平均生成速率 | 模型质量分 | 综合评分 |")
            lines.append("| --- | --- | --- | --- | --- |")
            for name, p in self.judgment.model_performance.items():
                ttft = f"{p['avg_ttft_ms']}ms" if p.get("avg_ttft_ms") is not None else "N/A"
                rate = f"{p['avg_gen_rate']}字/s" if p.get("avg_gen_rate") is not None else "N/A"
                comp = self.judgment.composite_scores.get(name, "N/A")
                lines.append(f"| {name} | {ttft} | {rate} | {p['performance_score']} | {comp} |")
            lines.append("")
        lines.append("## 任务结果\n")
        if self.tasks:
            lines.append("| 任务# | 接收方 | 变量 | 完成 | 结果 |")
            lines.append("| --- | --- | --- | --- | --- |")
            for t in self.tasks:
                lines.append(f"| {t['id']} | {t['recipient']} | {t['var_name']} | "
                             f"{'是' if t['completed'] else '否'} | "
                             f"{t['result_value'] or t['result_message']} |")
        else:
            lines.append("（本次讨论未产生任务分派）")
        lines.append("")
        lines.append("## 各 Agent 记忆快照\n")
        for a in self.agents:
            snap = a.memory.snapshot()
            lines.append(f"- {a.name}：短期 {snap['short_term']} / 长期 {snap['long_term']} "
                         f"/ 情境 {snap['contextual']}")
        return "\n".join(lines)


# 全局编排器实例
orchestrator = Orchestrator()
