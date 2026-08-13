"""
orchestrator.py — 多 Agent 圆桌协作编排器

职责：
  - 维护 Agent 列表（首个为主 Agent，其余为协作 Agent）
  - 驱动"圆桌讨论"：主 Agent 分派任务，协作 Agent 回报结果
  - 任务分派：主 Agent 只说明任务内容与期望效果，并提供"任务接收方变量"
  - 任务回报：协作 Agent 仅返回最终结果 + 是否完成，并回填变量（不含中间过程）
  - 停止条件：所有 Agent 回报完成 → 停止等待用户；或达到最大轮次 → 主 Agent 总结
  - 支持暂停 / 继续 / 结束
  - 通过事件总线把发言/任务/状态实时推送给 UI
"""
import asyncio
import threading
import time
from app.agent import Agent
from app.llm import chat, chat_stream, resolve_model_config
from app.config import settings

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
        self.status = "idle"          # idle/running/paused/done/stopped
        self.event_bus = EventBus()
        self.transcript = []          # 发言记录（用于导出 markdown）
        self._pause_event = asyncio.Event()
        self._pause_event.set()
        self._run_task = None

    # ---------------- 配置 ----------------
    def configure(self, agent_configs: list, topic: str = "", max_rounds: int = 8):
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
        for i, cfg in enumerate(agent_configs):
            model_config = resolve_model_config(
                cfg.get("provider", "default"),
                cfg.get("base_url", ""),
                cfg.get("api_key", ""),
                cfg.get("model_id", ""),
            )
            agent = Agent(
                agent_id=cfg.get("agent_id", f"agent_{i+1}"),
                name=cfg.get("name", f"Agent_{i+1}"),
                role=cfg.get("role", ""),
                personality=cfg.get("personality", ""),
                model_config=model_config,
                is_main=(i == 0),
                forget_config=cfg.get("forget_config"),
            )
            for a in self.agents:
                a.reset_history()
            self.agents.append(agent)
            if i == 0:
                self.main = agent
            else:
                self.collaborators.append(agent)
        # 通知主 Agent 可指派的协作 Agent 名称
        self.status = "idle"

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

    def _log(self, text):
        asyncio.create_task(self.event_bus.publish({"type": "log", "text": text}))

    # ---------------- 单次 Agent 回合（含工具调用循环） ----------------
    async def _consume_stream(self, messages, model_config, tools, on_delta=None):
        """
        在后台线程运行流式生成，把增量事件桥接回事件循环。

        返回完整结果 dict（与 chat() 一致：content / tool_calls / assistant_message）。
        on_delta(delta: str) 为可选回调，每次收到文本增量时调用。
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
        while True:
            ev = await q.get()
            t = ev.get("type")
            if t == "final":
                return ev["result"]
            if t == "error":
                raise RuntimeError(ev["error"])
            if t == "content" and on_delta is not None:
                await on_delta(ev["delta"])

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
                        self._log(f"[LLM流式失败:{agent.name}] {e}，回退非流式")
                        result = await asyncio.to_thread(
                            chat, messages, agent.model_config, tools)
                else:
                    result = await asyncio.to_thread(
                        chat, messages, agent.model_config, tools)
            except Exception as e:  # noqa
                msg = f"[LLM错误:{agent.name}] {e}"
                self._log(msg)
                # 把错误同步推送到前端（醒目的错误气泡），避免只在控制台可见
                await self.event_bus.publish({
                    "type": "agent_error",
                    "agent": agent.name,
                    "error": str(e),
                })
                break

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
                "streamed": emitting}

    # ---------------- 主 Agent 指令 ----------------
    def _main_directive(self, round, is_last, had_any_task, pending_exists):
        base = f"【讨论主题】{self.topic}\n【轮次】第 {round}/{self.max_rounds} 轮（圆桌辩论进行中）\n"
        if round == 1:
            base += ("这是辩论第 1 轮。请先为协作 Agent 分派调研 / 立论任务（delegate_task），"
                     "明确任务内容与期望效果，并提供接收结果的变量名(var_name)；"
                     "你也可以先陈述你的开场立场。")
        else:
            base += ("这是辩论第 %d 轮。请基于各方上一轮发言与已回填的任务结果，"
                     "以主持人身份抛出疑问、要求补充论据、指出分歧或让某方进一步论证；"
                     "可通过 delegate_task 追加追问任务，或直接发言引发交锋。"
                     "请至少提出一个有针对性的质疑，推动多轮讨论。" % round)
            if pending_exists:
                base += "\n（提示：仍存在未完成任务，请先审视其进展。）"
        return base

    # ---------------- 讨论主循环 ----------------
    async def run_discussion(self, topic: str = "", max_rounds: int = None):
        if self.status == "running":
            return
        if max_rounds is not None:
            self.max_rounds = max(1, int(max_rounds))
        if topic:
            self.topic = topic
        self.round = 0
        self.status = "running"
        self._pause_event.set()
        await self.event_bus.publish({
            "type": "status", "status": "running",
            "topic": self.topic, "max_rounds": self.max_rounds,
            "agents": [{"name": a.name, "role": a.role, "is_main": a.is_main} for a in self.agents],
        })

        had_any_task = False
        self.last_main_message = self.topic

        try:
            while self.round < self.max_rounds:
                # 暂停 / 结束 控制
                await self._pause_event.wait()
                if self.status == "stopped":
                    break

                self.round += 1
                is_last = (self.round >= self.max_rounds)
                await self.event_bus.publish({
                    "type": "round", "round": self.round, "max": self.max_rounds,
                })

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
                    self._create_task(d)
                    await self.event_bus.publish({
                        "type": "task_assigned",
                        "from": self.main.name,
                        "to": d.get("recipient", ""),
                        "content": d.get("content", ""),
                        "expected": d.get("expected_outcome", ""),
                        "var": d.get("var_name", ""),
                    })
                self._record_memory(self.main, main_out["content"])

                # 2) 协作 Agent 回合
                for collab in self.collaborators:
                    pending = [t for t in self.tasks
                               if not t["completed"] and t["recipient"] == collab.name]
                    if pending:
                        for task in pending:
                            umsg = (f"你收到任务#{task['id']}：\n内容：{task['content']}\n"
                                    f"期望效果：{task['expected_outcome']}\n"
                                    f"请将结果回填到变量 `{task['var_name']}`。")
                            out = await self._agent_turn(collab, umsg, mode="task")
                            if out["report"]:
                                self._apply_report(task, out["report"])
                                await self._emit_speech(collab, out["report"].get("message", ""))
                                await self.event_bus.publish({
                                    "type": "task_result",
                                    "from": collab.name,
                                    "var": task["var_name"],
                                    "value": out["report"].get("var_value", ""),
                                    "completed": bool(out["report"].get("completed", False)),
                                    "message": out["report"].get("message", ""),
                                    "task_id": task["id"],
                                })
                            else:
                                # 未回报，记录其发言（若有）
                                if out["content"]:
                                    await self._emit_speech(collab, out["content"])
                                task["rounds_waiting"] = task.get("rounds_waiting", 0) + 1
                                if task["rounds_waiting"] >= 3:
                                    task["completed"] = True
                                    self.shared_context[task["var_name"]]["value"] = "（超时未完成）"
                    else:
                        # 辩论环节：针对主持人最新发言进行回应 / 辩护 / 反驳
                        umsg = (f"主持人刚才说：\n「{self.last_main_message}」\n\n"
                                f"请基于你的角色「{collab.role}」与性格，就此进行回应、辩护或反驳，"
                                f"推进这场圆桌辩论；可调用 knowledge_search 检索论据支撑论点。")
                        out = await self._agent_turn(collab, umsg, mode="discussion", stream=True)
                        if out["content"]:
                            if not out["streamed"]:
                                await self._emit_speech(collab, out["content"])
                            self._record_memory(collab, out["content"])

                # 3) 记忆转化与遗忘
                await self._memory_housekeeping()

                # 4) 达到最大轮次则结束（由最终总结收尾，不再提前停止）
                if is_last or self.round >= self.max_rounds:
                    break

                await asyncio.sleep(0)

            # 全部轮次结束 → 主持人综合各方观点进行总结
            summary = await self._final_summary()
            await self._emit_summary(summary)
            self.status = "done"
            await self.event_bus.publish({"type": "status", "status": "done"})
        except Exception as e:  # noqa
            self._log(f"[编排器错误] {e}")
            self.status = "stopped"
            await self.event_bus.publish({"type": "status", "status": "stopped"})

    # ---------------- 任务与变量 ----------------
    def _create_task(self, d: dict):
        recipient = d.get("recipient", "")
        var_name = d.get("var_name") or f"var_{self.task_counter+1}"
        self.task_counter += 1
        tid = self.task_counter
        task = {
            "id": tid,
            "recipient": recipient,
            "content": d.get("content", ""),
            "expected_outcome": d.get("expected_outcome", ""),
            "var_name": var_name,
            "completed": False,
            "result_value": "",
            "result_message": "",
            "rounds_waiting": 0,
        }
        self.tasks.append(task)
        self.shared_context[var_name] = {
            "by": self.main.name, "value": "", "task_id": tid,
        }

    def _apply_report(self, task, report: dict):
        var = report.get("var_name") or task["var_name"]
        val = report.get("var_value", "")
        task["completed"] = bool(report.get("completed", False))
        task["result_value"] = val
        task["result_message"] = report.get("message", "")
        if var in self.shared_context:
            self.shared_context[var]["value"] = val
        else:
            self.shared_context[var] = {"by": self.main.name, "value": val, "task_id": task["id"]}
        # 协作 Agent 把结果沉淀进长期记忆
        self.get_agent(task["recipient"]).memory.add_long(
            f"完成任务#{task['id']}({var}): {val}", importance=0.85)

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
            except Exception:
                pass

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

    async def _final_summary(self):
        directive = ("所有 Agent 的任务已结束或已达最大轮次。请作为主 Agent，"
                     "综合各方发言与任务结果，输出一份结构化的最终讨论结果"
                     "（使用 markdown，含：核心结论、各任务结果、待办/分歧）。"
                     "请直接输出总结文本，不要再次分派任务。")
        out = await self._agent_turn(self.main, directive, mode="discussion", allow_delegate=False)
        return out["content"] or "（主 Agent 未生成总结）"

    # ---------------- 导出 Markdown ----------------
    def export_markdown(self) -> str:
        lines = []
        lines.append(f"# 圆桌讨论记录\n")
        lines.append(f"- 主题：{self.topic}")
        lines.append(f"- 轮次：{self.round}/{self.max_rounds}")
        lines.append(f"- 时间：{time.strftime('%Y-%m-%d %H:%M:%S')}")
        lines.append(f"- 参与 Agent：")
        for a in self.agents:
            tag = "（主）" if a.is_main else ""
            lines.append(f"  - {a.name}{tag} | 角色：{a.role} | 模型：{a.model_config['model_id']} @ {a.model_config['base_url']}")
        lines.append("")
        lines.append("## 发言记录\n")
        for e in self.transcript:
            if e["type"] == "speech":
                lines.append(f"**{e['speaker']}**（第{e['round']}轮）：\n\n{e['text']}\n")
            elif e["type"] == "summary":
                lines.append(f"## 最终讨论结果（{e['speaker']} 整理）\n\n{e['text']}\n")
        lines.append("## 任务结果\n")
        if self.tasks:
            lines.append("| 任务# | 接收方 | 变量 | 完成 | 结果 |")
            lines.append("| --- | --- | --- | --- | --- |")
            for t in self.tasks:
                lines.append(f"| {t['id']} | {t['recipient']} | {t['var_name']} | "
                             f"{'是' if t['completed'] else '否'} | {t['result_value'] or t['result_message']} |")
        else:
            lines.append("（本次讨论未产生任务分派）")
        lines.append("")
        lines.append("## 各 Agent 记忆快照\n")
        for a in self.agents:
            snap = a.memory.snapshot()
            lines.append(f"- {a.name}：短期 {snap['short_term']} / 长期 {snap['long_term']} / 情境 {snap['contextual']}")
        return "\n".join(lines)


# 全局编排器实例
orchestrator = Orchestrator()
