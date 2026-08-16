"""
orchestrator.py — 多 Agent 辩论系统编排器（Supervisor Agent 实现）

对齐《多Agent辩论系统架构设计文档》：
  - Supervisor 职责：任务编排、状态机流转、收敛触发、结果聚合；不亲自生成论点
  - 状态机：Idle → Preparing → Debating → Judging → Finished
  - 并行立论：第 1 轮所有辩手并发立论（asyncio.gather）
  - 串行反驳：后续轮次辩手基于最新交锋内容逐一反驳/辩护
  - 终止条件：max_rounds / Token 预算熔断 / 语义收敛检测 / 手动结束
  - RAG 核查：每轮结束后异步事实核查，证据注入下一轮上下文
  - Judge 裁决：终止后单次调用裁判，输出 Judgment（scores/winner/critique/convergence_flag）
  - 消息协议：任务分派/回报事件带 task_id(UUID)、timestamp、token_cost
  - 可观测性：trace_id 贯穿事件与导出文件；统一日志
"""
import asyncio
import json
import re
import threading
import time

from app.agent import Agent
from app.config import settings
from app.llm import chat, chat_stream
from app.protocol import new_trace_id, new_uuid, now_ms, Judgment
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


class Orchestrator:
    def __init__(self):
        self.agents = []
        self.main = None
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

        # ---- 预算（架构文档 §6.3） ----
        self.budget = settings.TOKEN_BUDGET
        self.spent = 0

        # ---- 辩论状态 ----
        self._started_at = None
        self.convergence_streak = 0
        self.round_arguments = {}     # round -> {speaker: text}
        self.latest_arguments = {}    # speaker -> {"text","round"}
        self.round_evidence = {}      # round -> {speaker: [EvidenceItem]}
        self.judgment = None
        self.last_main_message = ""
        self.run_stats = {"compressions": 0}  # 运行摘要统计（优化 A4）

    # ---------------- 配置 ----------------
    def configure(self, agent_configs: list, topic: str = "", max_rounds: int = 8):
        if self.status == "running":
            raise ValueError("讨论进行中，请先结束或等待后再重新配置")
        self.agents = []
        self.main = None
        self.collaborators = []
        self.shared_context = {}
        self.tasks = []
        self.task_counter = 0
        self.round = 0
        self.last_main_message = topic
        self.max_rounds = max(1, int(max_rounds))
        self.topic = topic
        self.transcript = []
        self.trace_id = new_trace_id()
        self.budget = settings.TOKEN_BUDGET
        self.spent = 0
        self.phase = "idle"
        self.convergence_streak = 0
        self.round_arguments = {}
        self.latest_arguments = {}
        self.round_evidence = {}
        self.judgment = None
        self.run_stats = {"compressions": 0}

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
        self.status = "idle"
        self._log(f"配置完成，席位 {len(self.agents)} 个",
                  event="configure", level="info")

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

    def resume(self):
        if self.status == "paused":
            self.status = "running"
            self._pause_event.set()

    def end(self):
        self.status = "stopped"
        self._pause_event.set()

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
        asyncio.create_task(self.event_bus.publish({
            "type": "log", "text": f"[{self.trace_id}] {text}",
            "level": level, "event": event or "orchestrator.event",
        }))

    # ---------------- 单次 Agent 回合（含工具调用循环） ----------------
    async def _consume_stream(self, messages, model_config, tools, on_delta=None):
        """
        在后台线程运行流式生成，把增量事件桥接回事件循环。

        返回完整结果 dict（与 chat() 一致：content / tool_calls / assistant_message / usage）。
        on_delta(delta: str) 为可选回调，每次收到文本增量时调用。
        整体超时受 settings.STREAM_TIMEOUT 约束（架构文档 §4.5 单步超时）。
        """
        q = asyncio.Queue()
        loop = asyncio.get_running_loop()

        def _run():
            try:
                for ev in chat_stream(messages, model_config, tools):
                    loop.call_soon_threadsafe(q.put_nowait, ev)
            except Exception as e:  # noqa
                loop.call_soon_threadsafe(q.put_nowait, {"type": "error", "error": str(e)})

        threading.Thread(target=_run, daemon=True).start()

        async def _drain():
            while True:
                ev = await q.get()
                t = ev.get("type")
                if t == "final":
                    return ev["result"]
                if t == "error":
                    raise RuntimeError(ev["error"])
                if t == "content" and on_delta is not None:
                    await on_delta(ev["delta"])

        try:
            return await asyncio.wait_for(_drain(), timeout=settings.STREAM_TIMEOUT)
        except asyncio.TimeoutError:
            raise RuntimeError(f"流式响应超时（>{settings.STREAM_TIMEOUT}s）")

    async def _run_llm_retry(self, messages, model_config, tools=None, temperature=0.7):
        """带指数退避重试的非流式 LLM 调用（架构文档 §4.5：重试 2 次，间隔 1s/3s）。"""
        delays = list(settings.LLM_RETRY_DELAYS)
        last_exc = None
        for attempt in range(max(1, settings.LLM_RETRY_ATTEMPTS)):
            try:
                return await asyncio.to_thread(
                    chat, messages, model_config, tools, temperature=temperature)
            except Exception as e:  # noqa
                last_exc = e
                if attempt >= len(delays):
                    break
                delay = delays[attempt]
                self._log(f"[LLM重试] 第{attempt+1}次调用失败({e})，{delay}s 后重试",
                          level="warning", event="llm.retry")
                await asyncio.sleep(delay)
        raise last_exc or RuntimeError("LLM 调用失败")

    def _apply_usage(self, result) -> int:
        """累加本次调用的 token 用量，返回本次 total_tokens。"""
        usage = result.get("usage") or {}
        total = usage.get("total_tokens")
        if not total:
            total = (usage.get("prompt_tokens") or 0) + (usage.get("completion_tokens") or 0)
        if total:
            self.spent += int(total)
        return int(total) if total else 0

    async def _agent_turn(self, agent: Agent, user_msg: str, mode: str,
                          allow_delegate: bool = True, stream: bool = False):
        collaborators_meta = [{"name": c.name} for c in self.collaborators]
        sys_prompt = agent.system_prompt(
            collaborators_meta, self.shared_context, self._task_status_text(),
            mode, self.topic)
        messages = [{"role": "system", "content": sys_prompt}] + list(agent.history) + \
                   [{"role": "user", "content": user_msg}]

        tools = agent.mcp.schemas()
        if agent.is_main and allow_delegate:
            tools = tools + [DELEGATE_SCHEMA]
        elif (not agent.is_main) and mode == "task":
            tools = tools + [REPORT_SCHEMA]

        content = ""
        delegate = []
        report = None
        token_cost = 0
        emitting = False  # 是否已开始向 UI 流式推送

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
                            messages, agent.model_config, tools, _emit_delta)
                    except Exception as e:  # noqa
                        self._log(f"[LLM流式失败:{agent.name}] {e}，回退非流式",
                                  level="warning", event="llm.stream_fallback")
                        result = await self._run_llm_retry(
                            messages, agent.model_config, tools)
                else:
                    result = await self._run_llm_retry(
                        messages, agent.model_config, tools)
            except Exception as e:  # noqa
                msg = f"[LLM错误:{agent.name}] {e}"
                self._log(msg, level="error", event="llm.error")
                # 把错误同步推送到前端（醒目的错误气泡），避免只在控制台可见
                await self.event_bus.publish({
                    "type": "agent_error",
                    "agent": agent.name,
                    "error": str(e),
                })
                break

            token_cost += self._apply_usage(result)
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

        # 流式收尾：补发 speech_end（前端用完整文本替换），并记录 transcript
        if emitting and content:
            await self.event_bus.publish({
                "type": "speech_end",
                "speaker": agent.name,
                "round": self.round,
                "text": content,
            })
            self.transcript.append({
                "type": "speech", "round": self.round, "speaker": agent.name,
                "is_main": agent.is_main, "text": content,
            })

        agent.append_history("user", user_msg)
        if content:
            agent.append_history("assistant", content)
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
            base += ("这是辩论第 %d 轮。请基于各方上一轮发言与已回填的任务结果，"
                     "以主持人身份抛出疑问、要求补充论据、指出分歧或让某方进一步论证；"
                     "可通过 delegate_task 追加追问任务，或直接发言引发交锋。"
                     "请至少提出一个有针对性的质疑，推动多轮辩论。" % round)
            if pending_exists:
                base += "\n（提示：仍存在未完成任务，请先审视其进展。）"
        return base

    # ---------------- 辩论上下文构建 ----------------
    def _record_argument(self, speaker: str, text: str, round_no: int):
        if not text:
            return
        self.round_arguments.setdefault(round_no, {})[speaker] = text
        self.latest_arguments[speaker] = {"text": text, "round": round_no}

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
        parts = []
        if self.round == 1:
            # 立论阶段
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
        else:
            # 反驳 / 辩护阶段
            others = {k: v for k, v in self.latest_arguments.items() if k != collab.name}
            if others:
                brief = "\n".join(
                    f"【{k}】（第{v['round']}轮）：{v['text'][:500]}" for k, v in others.items())
            else:
                brief = f"主持人最新发言：{self.last_main_message}"
            parts.append(f"这是第 {self.round} 轮反驳/辩护阶段。以下是最新交锋内容：\n{brief}")
            parts.append(f"请作为「{stance_label}」针对上述观点进行反驳或辩护："
                         "直接回应对方核心论据，指出逻辑谬误（如滑坡谬误、稻草人）或提供反证"
                         "（可调用 knowledge_search 检索）。必须坚定维护你的立场，禁止倒戈。")
        # RAG 证据注入（上一轮核查结果）
        ev = self._evidence_for_round(self.round - 1)
        if ev:
            parts.append(ev)
        parts.append("请以第一人称输出你的发言，不要输出任何 JSON。")
        return "\n\n".join(parts)

    # ---------------- 协作 Agent 回合 ----------------
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
        else:
            # 辩论环节：立论 / 反驳
            umsg = self._debate_prompt(collab)
            out = await self._agent_turn(collab, umsg, mode="discussion", stream=True)
            if out["content"]:
                if not out["streamed"]:
                    await self._emit_speech(collab, out["content"])
                self._record_memory(collab, out["content"])
                self._record_argument(collab.name, out["content"], self.round)

    # ---------------- RAG 事实核查（架构文档 §2.5 / §4.2） ----------------
    async def _rag_check_round(self):
        texts = list(self.round_arguments.get(self.round, {}).values())
        if not texts:
            return
        combined = "\n".join(texts)
        try:
            evidence = await asyncio.to_thread(rag_service.check, combined, 3)
        except Exception as e:  # noqa
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
        except Exception:  # noqa
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

    # ---------------- 讨论主循环 ----------------
    async def run_discussion(self, topic: str = "", max_rounds: int = None, budget: int = None):
        if self.status == "running":
            return
        if max_rounds is not None:
            self.max_rounds = max(1, int(max_rounds))
        if topic:
            self.topic = topic
        self.trace_id = new_trace_id()
        self.budget = int(budget or settings.TOKEN_BUDGET)
        self.spent = 0
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
        self.run_stats = {"compressions": 0, "started_at": time.time()}
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
                is_last = (self.round >= self.max_rounds)
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

                # 2) 协作 Agent 回合
                if self.round == 1:
                    # 并行立论（架构文档 §4.2 / §6.2）
                    await asyncio.gather(
                        *[self._collab_turn(c) for c in self.collaborators],
                        return_exceptions=True,
                    )
                else:
                    # 串行反驳：依赖对方最新论点，按序发言
                    for c in self.collaborators:
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

                # 6) 预算熔断（架构文档 §6.3）
                remaining = self.budget - self.spent
                if remaining < settings.MIN_TOKEN_THRESHOLD:
                    self._log(f"预算熔断：剩余Token({remaining})低于阈值"
                              f"({settings.MIN_TOKEN_THRESHOLD})，强制进入裁决",
                              level="warning", event="budget.circuit_break")
                    break

                # 7) 达到最大轮次则结束
                if is_last or self.round >= self.max_rounds:
                    break

                await self.event_bus.publish(self._status_sync())
                await asyncio.sleep(0)

            # ---- Judging：终止后单次调用裁判（架构文档 §4.1/§6.2） ----
            self.phase = "judging"
            await self.event_bus.publish({"type": "status", "status": "running", "phase": self.phase})
            remaining = self.budget - self.spent
            if remaining >= settings.MIN_TOKEN_THRESHOLD:
                judgment = await self._judge_round()
            else:
                # 预算硬熔断（架构文档 §6.3）：剩余不足以支撑裁判 LLM 调用，回退确定性裁决
                self._log(f"预算硬熔断：剩余Token({remaining})低于阈值"
                          f"({settings.MIN_TOKEN_THRESHOLD})，裁判使用回退裁决",
                          level="warning", event="budget.hard_break")
                judgment = Judgment(winner="DRAW", critique="预算耗尽，未执行完整 LLM 裁决",
                                    convergence_flag=False)
            self.judgment = judgment
            await self.event_bus.publish({
                "type": "judgment",
                "scores": judgment.scores,
                "winner": judgment.winner,
                "critique": judgment.critique,
                "convergence_flag": judgment.convergence_flag,
                "round": self.round,
            })
            await self.event_bus.publish(self._status_sync())

            # ---- Finished：主持人汇总最终报告（同样受预算硬熔断约束） ----
            self.phase = "finished"
            remaining = self.budget - self.spent
            if remaining >= settings.MIN_TOKEN_THRESHOLD:
                summary = await self._final_summary(judgment)
            else:
                self._log(f"预算硬熔断：剩余Token({remaining})低于阈值，主持人汇总使用确定性回退",
                          level="warning", event="budget.hard_break")
                summary = self._fallback_summary(judgment)
            await self._emit_summary(summary)
            self.status = "done"
            await self.event_bus.publish({"type": "status", "status": "done", "phase": self.phase})
            await self.event_bus.publish(self._status_sync())
        except Exception as e:  # noqa
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

    async def _judge_round(self) -> Judgment:
        history = self._build_debate_history_text()
        stance_lines = "\n".join(
            f"- {a.name}（{a.role}）立场：{STANCE_LABEL.get(a.stance, a.stance)}"
            for a in self.agents)
        judge_sys = ("你是辩论裁判。你保持绝对中立，不参与辩论，仅基于辩论记录进行多维度评分。"
                     "评分维度（1-10）：逻辑自洽性(logic)、证据效力(evidence)、"
                     "反驳有效性(rebuttal)、表达清晰度(clarity)。")
        prompt = (f"辩论主题：{self.topic}\n\n辩手与立场：\n{stance_lines}\n\n"
                  f"完整辩论记录：\n{history}\n\n"
                  "请输出 JSON（不要输出其他文字或代码块）：\n"
                  '{"scores": {"logic": 8, "evidence": 7, "rebuttal": 9, "clarity": 8}, '
                  '"winner": "PRO" | "CON" | "DRAW", "critique": "详细评语", '
                  '"convergence_flag": false}\n'
                  "winner 取全场综合表现更优的一方；若势均力敌取 DRAW。"
                  "convergence_flag 表示辩论是否因观点收敛而提前终止。")
        try:
            result = await self._run_llm_retry(
                [{"role": "system", "content": judge_sys},
                 {"role": "user", "content": prompt}],
                self.main.model_config, tools=None, temperature=0.3)
            self._apply_usage(result)
            data = self._extract_json(result["content"])
            judgment = Judgment(**data)
        except Exception as e:  # noqa
            self._log(f"[裁判调用失败] {e}", level="error", event="judge.fail")
            judgment = Judgment(winner="DRAW", critique=f"裁判调用失败：{e}",
                                convergence_flag=False)
        self._log(f"[裁判] winner={judgment.winner} scores={judgment.scores}",
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
                    a.memory.summarize_short_to_context()
                a.memory.forget()
                # 长对话管理（优化 A9）：轮次边界触发历史摘要压缩，并计入预算
                usage = await a.compress_history()
                if usage:
                    total = usage.get("total_tokens") or 0
                    if total:
                        self.spent += int(total)
                        self.run_stats["compressions"] = \
                            self.run_stats.get("compressions", 0) + 1
            except Exception:  # noqa
                pass

    # ---------------- 状态同步（架构文档 §3.5） ----------------
    def _status_sync(self) -> dict:
        return {
            "type": "status_sync",
            "trace_id": self.trace_id,
            "phase": self.phase,
            "current_round": self.round,
            "agent_states": {a.name: "IDLE" for a in self.agents},
            "remaining_budget": max(0, self.budget - self.spent),
            "spent_budget": self.spent,
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
                f"- 得分：{json.dumps(judgment.scores, ensure_ascii=False)}\n"
                f"- 胜方：{judgment.winner}\n"
                f"- 评语：{judgment.critique}")
        directive = ("所有 Agent 的任务已结束或已达最大轮次。请作为主持人，"
                     "综合各方发言、任务结果与裁判裁决，输出一份结构化的最终讨论报告"
                     "（使用 markdown，含：核心结论、各方立场与论点摘要、裁判裁决、待办/分歧）。"
                     "请直接输出总结文本，不要再次分派任务。") + judge_section
        out = await self._agent_turn(self.main, directive, mode="discussion", allow_delegate=False)
        return out["content"] or "（主 Agent 未生成总结）"

    def _fallback_summary(self, judgment=None) -> str:
        """预算硬熔断时的确定性汇总：不调用 LLM，直接基于发言记录与裁决拼装报告。"""
        lines = []
        lines.append(f"## 最终讨论报告（预算耗尽自动汇总）\n")
        lines.append(f"- 主题：{self.topic}")
        lines.append(f"- 实际轮次：{self.round}/{self.max_rounds}")
        lines.append(f"- Token 用量：{self.spent} / 预算：{self.budget}")
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
            if judgment.critique:
                lines.append(f"- 评语：{judgment.critique}")
            lines.append("")
        lines.append("> 注：因 Token 预算耗尽（架构文档 §6.3 硬熔断），本报告由系统按发言记录"
                     "确定性生成，未调用主持人 LLM 汇总。")
        return "\n".join(lines)

    # ---------------- 导出 Markdown ----------------
    def export_markdown(self) -> str:
        lines = []
        lines.append(f"# 多 Agent 辩论记录\n")
        lines.append(f"- 主题：{self.topic}")
        lines.append(f"- 轮次：{self.round}/{self.max_rounds}")
        lines.append(f"- 状态：{self.status} / 阶段：{self.phase}")
        lines.append(f"- Trace ID：{self.trace_id}")
        lines.append(f"- Token 用量：{self.spent} / 预算：{self.budget}")
        lines.append(f"- 时间：{time.strftime('%Y-%m-%d %H:%M:%S')}")
        lines.append(f"- 参与 Agent：")
        for a in self.agents:
            tag = "（主）" if a.is_main else ""
            lines.append(f"  - {a.name}{tag} | 角色：{a.role} | 立场："
                         f"{STANCE_LABEL.get(a.stance, a.stance)} | "
                         f"模型：{a.model_config['model_id']} @ {a.model_config['base_url']}")
        lines.append("")
        lines.append("## 运行摘要\n")
        elapsed = (time.time() - self.run_stats.get("started_at", time.time())) \
            if self.run_stats.get("started_at") else 0.0
        conv_flag = self.convergence_streak >= settings.CONVERGENCE_ROUNDS
        done_tasks = sum(1 for t in self.tasks if t["completed"])
        lines.append(f"- 持续时间：{elapsed:.1f} 秒")
        lines.append(f"- 实际轮次：{self.round}/{self.max_rounds}")
        lines.append(f"- 收敛触发：{'是（连续%d轮相似）' % self.convergence_streak if conv_flag else '否'}")
        lines.append(f"- RAG 证据轮数：{len(self.round_evidence)}")
        lines.append(f"- 任务：{len(self.tasks)}（完成 {done_tasks}）")
        lines.append(f"- 历史压缩次数：{self.run_stats.get('compressions', 0)}")
        lines.append(f"- 预算：已用 {self.spent} / 总量 {self.budget}"
                     f"（剩余 {max(0, self.budget - self.spent)}）")
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
            lines.append(f"- 胜方：{j.winner}")
            lines.append(f"- 收敛终止：{'是' if j.convergence_flag else '否'}")
            lines.append(f"- 评语：{j.critique}")
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
