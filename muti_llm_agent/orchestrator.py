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
    L5 采样参数对齐 / L6 长度中性评审（详见 muti_llm_agent.fairness.py）
  - 消息协议：任务分派/回报事件带 task_id(UUID)、timestamp、token_cost
  - 可观测性：trace_id 贯穿事件与导出文件；统一日志

模块分层（本文件只保留编排状态机与回合驱动，其余已拆分为独立模块）：
  - `muti_llm_agent.events`    事件总线（有界队列 + 背压）
  - `muti_llm_agent.prompts`   工具 schema / 结论模板 / 反思 prompt / 指令构建
  - `muti_llm_agent.textutil`  文本与费用工具（相似度、论据句抽取、费用估算）
  - `muti_llm_agent.scoring`   裁判评分（TTFT/速率 → 质量分、综合分、胜方推导）
  - `muti_llm_agent.conclusion` 结构化结论的四段式规整与兜底
  - `muti_llm_agent.exporter`  Markdown 导出渲染

下列私有名保留为「向后兼容别名」（既有测试与外部脚本按原名导入）：
`_shorten` / `_estimate_cost_rmb` / `_text_similarity` / `_extract_argument_sentences`。
"""
import asyncio
import json
import re
import threading
import time

from muti_llm_agent.agent import Agent
from muti_llm_agent.bgtasks import spawn_safe as _spawn_safe
from muti_llm_agent.bgtasks import submit_stream
from muti_llm_agent.conclusion import fallback_summary as _fallback_summary_text
from muti_llm_agent.conclusion import normalize_conclusion as _normalize_conclusion_text
from muti_llm_agent.conclusion import split_sections as _split_sections
from muti_llm_agent.config import OPPONENT_STANCE, settings
from muti_llm_agent.events import EventBus
from muti_llm_agent.exporter import render_markdown
from muti_llm_agent.fairness import FairnessGovernor
from muti_llm_agent.followup import (
    build_carryover,
    build_seed_block,
    normalize_materials,
    snapshot_iteration,
    validate_followup,
)
from muti_llm_agent.llm import chat, chat_stream, summarize_text
from muti_llm_agent.prompts import (
    CLAIM_SCHEMA,
    DELEGATE_SCHEMA,
    REPORT_SCHEMA,
    STANCE_LABEL,
    collab_summary_directive,
    final_summary_directive,
)
from muti_llm_agent.prompts import collab_main_directive as _collab_main_directive_fn
from muti_llm_agent.prompts import collab_work_prompt as _collab_work_prompt_fn
from muti_llm_agent.prompts import main_directive as _main_directive_fn
from muti_llm_agent.prompts import parse_reflection as _parse_reflection
from muti_llm_agent.prompts import reflection_prompt as _reflection_prompt
from muti_llm_agent.protocol import Judgment, new_trace_id, new_uuid, now_ms
from muti_llm_agent.rag import rag_service
from muti_llm_agent.runlog import write_event as _runlog_write
from muti_llm_agent.scoring import (
    compute_composite,
    judge_ruling_text,
    model_performance,
    performance_text,
    score_rate,
    score_ttft,
)
from muti_llm_agent.steering import SteeringQueue
from muti_llm_agent.textutil import estimate_cost_rmb as _estimate_cost_rmb
from muti_llm_agent.textutil import extract_argument_sentences as _extract_argument_sentences
from muti_llm_agent.textutil import shorten as _shorten
from muti_llm_agent.textutil import text_similarity as _text_similarity


class Orchestrator:
    def __init__(self, session_id: str | None = None):
        self.session_id = session_id or "default"
        self.agents: list[Agent] = []
        self.main: Agent | None = None
        self.judge: Agent | None = None
        self.collaborators: list[Agent] = []
        self.shared_context: dict[str, dict] = {}  # var -> {"by","value","task_id"}
        self.tasks: list[dict] = []
        self.task_counter = 0
        self.round = 0
        self.max_rounds = 8
        self.topic = ""
        self.status = "idle"          # idle/running/paused/done/stopped（UI 状态）
        self.phase = "idle"           # idle/preparing/debating/judging/finished（架构状态机）
        self.mode = "debate"          # debate（辩论）/ collaboration（合作）
        self.trace_id = ""
        self.event_bus = EventBus()
        self.transcript: list[dict] = []   # 发言记录（用于导出 markdown）
        self._pause_event = asyncio.Event()
        self._pause_event.set()
        self._run_task: asyncio.Task | None = None

        # ---- 遥测（只读）：token 用量不再作为硬约束，仅保留观测 ----
        self.spent = 0                         # 累计 token 用量（含输入/输出，只读遥测）
        self.output_tokens = 0                 # 累计输出 token（实时显示，只读遥测）
        # ---- 多层公平性治理（FairnessGovernor，替代旧的 token 预算总控） ----
        self.governor = FairnessGovernor()

        # ---- 辩论状态 ----
        self._started_at: float | None = None
        self.convergence_streak = 0
        # 收敛检测模式：embedding（向量余弦） / text-similarity（嵌入不可用时的降级，P0-3）
        self.convergence_mode = "embedding"
        self.round_arguments: dict[int, dict[str, str]] = {}  # 轮次 -> {speaker: text}
        self.latest_arguments: dict[str, dict] = {}  # speaker -> {"text","round"}
        self.round_evidence: dict[int, dict[str, list]] = {}  # 轮次 -> {立场: [证据]}
        self.judgment: Judgment | None = None
        self.conclusion: dict | None = None  # P0-1 结构化结论
        self.last_main_message = ""
        self.last_debate_speaker = None  # 上一轮/上一位实际发言的辩手（用于针对性反驳）
        self.run_stats: dict = {"compressions": 0}  # 运行摘要统计（优化 A4）
        self.agent_metrics: dict[str, dict] = {}  # {name: {calls, ttft_sum, gen_sum, chars}}
        # {name: 成功返回的 LLM 调用次数}（含纯工具调用回合，与 agent_metrics 区分）
        self.llm_call_counts: dict[str, int] = {}

        # ---- 中途注入纠偏（steering.py）：会话级指令队列 ----
        self.steering = SteeringQueue()
        # ---- 迭代链（followup.py）：第 1 轮为原始运行，之后每次「补充材料续轮」+1 ----
        self.iteration_no = 1
        self.iterations: list[dict] = []
        # 本轮迭代的承接包（上一轮结论 + 新材料 + 焦点），只在第 1 轮注入各席位
        self.followup_seed_active = ""
        self.followup_focus = ""
        self.followup_materials_chars = 0
        # ---- 运行期异常收集（写进运行摘要，复盘时不必翻诊断日志） ----
        self.errors: list[dict] = []
        self.ended_at: float | None = None

    # ---------------- 运行期必备引用 ----------------
    @property
    def main_agent(self) -> Agent:
        """当前主持人（Supervisor）；未 configure 时明确报错。

        属性 `main` 在 configure 前为 None，直接 `self.main_agent.name` 会抛
        `AttributeError: 'NoneType'`，定位困难；这里给出可读的错误信息，
        同时让类型检查器在调用点拿到非 Optional 的 Agent。
        """
        if self.main is None:
            raise RuntimeError("编排器尚未 configure：没有主持人 Agent")
        return self.main

    # ---------------- 配置 ----------------
    def configure(self, agent_configs: list, topic: str = "", max_rounds: int = 8,
                  judge_config: dict | None = None, mode: str = "debate"):
        if self.is_active:
            raise ValueError("讨论进行中，请先结束或等待后再重新配置")
        # 双模式：显式 mode 优先；未指定（含 None）回退 .env 的 DISCUSSION_MODE（默认 debate）
        mode = mode or settings.DISCUSSION_MODE
        self.mode = mode if mode in ("debate", "collaboration") else "debate"
        self.agents = []
        self.main = None
        self.judge = None
        self.collaborators = []
        self.max_rounds = max(1, int(max_rounds))
        self.topic = topic
        # 重新配置 = 开启一条全新的运行链：迭代链与中途指令一并清零，
        # 避免上一场的纠偏指令 / 承接材料被误用到新主题上。
        self.iteration_no = 1
        self.iterations = []
        self.steering = SteeringQueue()
        # 单场讨论的运行态统一清零（与 run_discussion 入口共用同一份清理逻辑）
        self._reset_run_state()

        for i, cfg in enumerate(agent_configs):
            from muti_llm_agent.llm import resolve_model_config
            model_config = resolve_model_config(
                cfg.get("provider", "default"),
                cfg.get("base_url", ""),
                cfg.get("api_key", ""),
                cfg.get("model_id", ""),
            )
            # 立场：显式指定优先；否则主位=主持人，其余按 PRO/CON 交替（辩论模式）
            # 合作模式弱化立场：主位 HOST、其余一律 NEUTRAL，不参与对抗逻辑
            stance = (cfg.get("stance", "") or "").upper()
            if not stance:
                if self.mode == "collaboration":
                    stance = "HOST" if i == 0 else "NEUTRAL"
                else:
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
                # 记忆按会话隔离：同名席位在不同 session 下不共享记忆文件
                session_id=self.session_id,
            )
            self.agents.append(agent)
            if i == 0:
                self.main = agent
            else:
                self.collaborators.append(agent)
        # 独立裁判 Agent（不占席位、不参与辩论，仅裁决阶段调用）；
        # 合作模式无辩论对抗，不构建裁判
        self.judge = None if self.mode == "collaboration" else self._build_judge(judge_config or {})
        self.status = "idle"
        self._persist_meta()
        self._log(f"配置完成，席位 {len(self.agents)} 个，模式：{self.mode}"
                  + (f"，独立裁判：{self.judge.name}" if self.judge else "，裁判回退使用主 Agent 模型"),
                  event="configure", level="info")

    # ---------------- 单场讨论的运行态 ----------------
    def _reset_run_state(self, keep_followup: bool = False):
        """清零「单场讨论」的运行态，保留席位 / 模式 / 裁判等配置。

        configure() 与 run_discussion() 入口共用同一份清理逻辑：后者在「不重新
        配置就直接再跑一场」时（status=done → 再次 start）必须先把上一场的
        transcript / tasks / shared_context / 各 Agent 历史清空，否则导出的
        Markdown 会混入上一场的发言与任务表，Agent 也会带着旧上下文继续发言。

        Args:
            keep_followup: 为 True 时保留本轮迭代的承接包（`start_followup`
                先调用本方法清场、再写入承接包，随后 run_discussion 又会清一次，
                因此需要显式要求保留）。
        """
        self.round = 0
        self.phase = "idle"
        self.transcript = []
        self.tasks = []
        self.task_counter = 0
        self.shared_context = {}
        self.trace_id = new_trace_id()
        self.spent = 0                    # 累计 token 用量（只读遥测）
        self.output_tokens = 0            # 累计输出 token（实时显示，只读遥测）
        self.governor = FairnessGovernor()  # 公平性治理器（每场全新）
        self.convergence_streak = 0
        self.convergence_mode = "embedding"
        self.round_arguments = {}
        self.latest_arguments = {}
        self.round_evidence = {}
        self.judgment = None
        self.conclusion = None            # P0-1 结构化结论
        self.last_debate_speaker = None
        self.last_main_message = self.topic
        self._opponent_prep: dict = {}             # 对手定向反驳上下文（需求 2/3）
        self._opponent_summary_cache: dict = {}    # (对手,轮次) -> 摘要，防同轮重复调用
        self.run_stats = {"compressions": 0}
        self.agent_metrics = {}
        self.llm_call_counts = {}
        self._started_at = None
        self.ended_at = None
        self.errors = []
        if not keep_followup:
            self.followup_seed_active = ""
            self.followup_focus = ""
            self.followup_materials_chars = 0
        for a in self.agents:
            a.reset_history()

    @property
    def is_active(self) -> bool:
        """是否已有未结束的讨论（run 任务仍在跑 / running / paused）。

        `/api/start` 与 `/api/configure` 用该属性做并发闸门：只判 `status ==
        "running"` 会漏掉 paused —— 暂停后再点「开始」会在同一个编排器上并发
        跑两个主循环，两边同时推进 round、写 transcript/遥测，导出记录错乱且
        无法用 end() 一次收尾。
        """
        if self.status in ("running", "paused"):
            return True
        task = self._run_task
        return task is not None and not task.done()

    # ---------------- 独立裁判（Judge Agent） ----------------
    def _build_judge(self, judge_config: dict):
        """根据 judge_config / .env 的 JUDGE_* 构建独立裁判 Agent。

        - judge_config 非空（前端已选择裁判模型）或 .env 配置了 JUDGE_* 时创建独立裁判；
        - 完全未配置时返回 None，_judge_round 回退使用主 Agent 的模型配置（保持向后兼容）。
        裁判不进入 self.agents / self.collaborators，因此不占席位、不参与辩论、不接收任务。

        provider 字段的显式值/环境默认值合并统一走 config.resolve_provider（§1.4），
        供应商预设解析仍由 llm.resolve_model_config 负责。
        """
        from muti_llm_agent.config import resolve_provider
        from muti_llm_agent.llm import resolve_model_config
        judge_cfg = {k: v for k, v in (judge_config or {}).items() if v}
        spec = resolve_provider(
            "JUDGE",
            provider=judge_cfg.get("provider", ""),
            base_url=judge_cfg.get("base_url", ""),
            api_key=judge_cfg.get("api_key", ""),
            model_id=judge_cfg.get("model_id", ""),
        )
        if not (judge_cfg or spec):
            return None
        model_config = resolve_model_config(
            spec.get("provider", "default"),
            spec.get("base_url", ""),
            spec.get("api_key", ""),
            spec.get("model_id", ""),
        )
        name = judge_cfg.get("name") or settings.JUDGE_NAME
        return Agent(
            agent_id="judge",
            name=name,
            role="独立辩论裁判",
            personality="客观、中立、严谨，不参与辩论",
            model_config=model_config,
            is_main=False,
            stance="NEUTRAL",
            session_id=self.session_id,
        )

    def get_agent(self, name):
        for a in self.agents:
            if a.name == name:
                return a
        return None

    # ---------------- 会话元数据持久化 ----------------
    def _persist_meta(self):
        """把当前会话关键状态写入 data/sessions/{session_id}.json（轻量快照）。

        用于多会话场景下：UI 中断横幅展示、进程重启后的中断标记。
        惰性导入 muti_llm_agent.manager，避免循环依赖。
        """
        try:
            from muti_llm_agent.manager import manager
            manager.save_meta(self)
        except Exception:
            pass  # 元数据持久化失败不影响辩论主流程

    def _notify_done(self):
        """通知全局 DebateManager「本会话讨论完成」（触发自动落盘等回调）。"""
        try:
            from muti_llm_agent.manager import manager
            manager.notify_done(self)
        except Exception:
            pass

    # ---------------- 控制 ----------------
    def pause(self):
        if self.status == "running":
            self.status = "paused"
            self._pause_event.clear()
            self._persist_meta()
            _spawn_safe(self.event_bus.publish(self._status_event()))

    def resume(self):
        if self.status == "paused":
            self.status = "running"
            self._pause_event.set()
            self._persist_meta()
            _spawn_safe(self.event_bus.publish(self._status_event()))

    def end(self):
        if self.status in ("running", "paused"):
            self.status = "stopped"
            self._pause_event.set()
            self._persist_meta()
            _spawn_safe(self.event_bus.publish(self._status_event()))

    # ---------------- 任务状态文本 ----------------
    def _task_status_text(self):
        if not self.tasks:
            return "（暂无任务）"
        lines = []
        for t in self.tasks:
            if t.get("status") == "pending":
                state = "🔵待认领"
                recipient = "任务池"
            elif t["completed"]:
                state = "✅已完成"
                recipient = t["recipient"] or "任务池"
            else:
                state = "⏳进行中"
                recipient = t["recipient"] or "任务池"
            lines.append(f"- [{state}] 任务#{t['id']} → {recipient} | "
                         f"变量:{t['var_name']}\n  内容：{t['content']}\n  期望：{t['expected_outcome']}")
        return "\n".join(lines)

    def _log(self, text, level="info", event=""):
        """结构化日志（优化 A4）：同时写入控制台 JSON 日志并推送到前端。"""
        from muti_llm_agent.logger import log, set_log_context
        set_log_context(trace_id=self.trace_id, session_id=self.session_id)
        log(level, event or "orchestrator.event", text, trace_id=self.trace_id,
            session_id=self.session_id)
        _spawn_safe(self.event_bus.publish({
            "type": "log", "text": f"[{self.trace_id}] {text}",
            "level": level, "event": event or "orchestrator.event",
        }))

    # ---------------- 运行事件日志（复盘时间线） ----------------
    def _runlog(self, etype: str, **fields):
        """写一条运行事件（runlog.py）；失败静默，绝不影响主流程。"""
        payload = {"type": etype, "round": self.round, "iteration": self.iteration_no}
        payload.update({k: v for k, v in fields.items() if v is not None})
        try:
            return _runlog_write(self.trace_id, payload)
        except Exception:
            return None

    def _record_error(self, where: str, error) -> None:
        """收集运行期异常（进运行摘要；同时落一条运行事件）。"""
        self.errors.append({"where": where, "error": str(error)[:800],
                            "ts": now_ms(), "round": self.round})
        self._runlog("error", where=where, text=str(error)[:500])

    # ---------------- 中途注入纠偏 ----------------
    def add_directive(self, text: str, kind: str = "correct", scope: str = "all",
                      target: str = "", priority: str = "normal",
                      source: str = "user", at_round: int = 0) -> dict:
        """由 API / 飞书 / DSH 调用：把一条指令加入本会话的纠偏队列。

        Raises:
            ValueError: 功能关闭 / 文本为空 / 队列已满
        """
        info = self.steering.add(text=text, kind=kind, scope=scope, target=target,
                                 priority=priority, source=source, at_round=at_round)
        self._runlog("directive_added", kind=info.get("kind"),
                     kind_label=info.get("kind_label"), scope=scope,
                     target=target, priority=priority, source=source,
                     text=info.get("text"))
        _spawn_safe(self.event_bus.publish({
            "type": "directive", "action": "added", "round": self.round,
            "directive": info, "steering": self.steering.stats(),
        }))
        return info

    async def _emit_directive_applied(self, agent: Agent, directives: list[dict]):
        """指令已在某席位回合生效：写 transcript + 运行事件 + 前端事件。"""
        text = "\n".join(
            f"[{d.get('kind_label') or d.get('kind')}] {d.get('text', '')}"
            for d in directives)
        self.transcript.append({
            "type": "directive", "round": self.round, "speaker": agent.name,
            "is_main": bool(agent.is_main), "text": text,
        })
        for d in directives:
            self._runlog("directive_applied", kind=d.get("kind"),
                         kind_label=d.get("kind_label"), agent=agent.name,
                         is_main=bool(agent.is_main), text=d.get("text"))
        await self.event_bus.publish({
            "type": "directive", "action": "applied", "round": self.round,
            "agent": agent.name, "is_main": bool(agent.is_main),
            "directives": directives, "text": text,
            "steering": self.steering.stats(),
        })

    # ---------------- 运行摘要（summary.py） ----------------
    def summary(self) -> dict:
        """构建当前运行的摘要（只读；未开始时也能安全调用）。"""
        from muti_llm_agent.summary import build_summary
        return build_summary(self)

    # ---------------- 单次 Agent 回合（含工具调用循环） ----------------
    async def _consume_stream(self, messages, model_config, tools, on_delta=None,
                              on_tool_start=None, temperature=0.7, top_p=None):
        """
        在**流式线程池**中运行流式生成，把增量事件桥接回事件循环。

        返回完整结果 dict（与 chat() 一致：content / tool_calls / assistant_message / usage），
        额外附带 "tool_starts"：流式中已发出 tool_call_start 的工具索引列表（供调用方补齐展示）。
        on_delta(delta: str) 为可选回调，每次收到文本增量时调用。
        on_tool_start(name: str, index: int) 为可选回调，模型首次产出工具名时调用（P1-4 工具可视化）。
        整体超时受 settings.STREAM_TIMEOUT 约束（架构文档 §4.5 单步超时）。

        线程治理：早期实现是每次调用新建一个 daemon 线程，超时后线程阻塞在 SDK 里
        无法回收，长期运行会累积。现在统一提交到 `bgtasks.stream_pool()`（容量
        `LLM_STREAM_WORKERS`，默认 8），并用 `cancelled` 事件让超时后的生产线程
        在下一个增量处尽早退出。
        """
        q = asyncio.Queue()
        loop = asyncio.get_running_loop()
        t_start = time.time()
        metrics: dict[str, float | None] = {"ttft_s": None, "gen_s": 0.0, "chars": 0}
        started = set()  # 已发出 tool_call_start 的工具 index
        cancelled = threading.Event()

        def _safe_put(ev: dict) -> None:
            try:
                q.put_nowait(ev)
            except asyncio.QueueFull:
                pass

        def _emit(ev: dict) -> None:
            """从 worker 线程安全投递事件；超时/关闭后静默丢弃。"""
            if cancelled.is_set():
                return
            try:
                loop.call_soon_threadsafe(_safe_put, ev)
            except RuntimeError:
                # 事件循环已关闭（超时后收尾阶段），丢弃即可
                pass

        def _run():
            try:
                for ev in chat_stream(messages, model_config, tools,
                                      temperature=temperature, top_p=top_p):
                    if cancelled.is_set():
                        break  # 超时已触发：尽快结束生成，把 worker 还给线程池
                    _emit(ev)
            except Exception as e:
                _emit({"type": "error", "error": str(e)})

        submit_stream(_run)

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
                    ev["result"]["tool_starts"] = sorted(started)
                    return ev["result"]
                if t == "error":
                    raise RuntimeError(ev["error"])
                if t == "tool_call_start":
                    started.add(ev.get("index", 0))
                    if on_tool_start is not None:
                        await on_tool_start(ev["name"], ev.get("index", 0))
                    continue
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
        finally:
            # 无论正常结束还是超时：通知 worker 线程在下个增量处停止，释放线程池槽位
            cancelled.set()

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
        # ---- 中途注入纠偏（steering.py）----
        # 在构造 messages 之前取本回合命中的指令并渲染进 user 消息：
        # 这样指令既进入本次调用，也随 append_history 留在该席位的历史里，
        # 后续轮次不会「忘掉」用户中途纠正过的方向。
        if mode in ("discussion", "collaboration", "collab", "task"):
            injected = self.steering.take(agent.name, bool(agent.is_main), self.round)
            if injected:
                user_msg = user_msg + SteeringQueue.render(injected)
                await self._emit_directive_applied(agent, injected)
        # ---- 补充材料续轮：承接包 + 新材料只在第 1 轮注入，保证每个席位都看到 ----
        if self.followup_seed_active and self.round <= 1:
            user_msg = user_msg + "\n\n" + self.followup_seed_active

        collaborators_meta = [{"name": c.name} for c in self.collaborators]
        # system_prompt 内部会做记忆检索（memory.context_text），卸载到线程避免阻塞事件循环（§2.4）
        sys_prompt = await asyncio.to_thread(
            agent.system_prompt, collaborators_meta, self.shared_context,
            self._task_status_text(), mode, self.topic)
        messages = [{"role": "system", "content": sys_prompt},
                    *list(agent.history),
                    {"role": "user", "content": user_msg}]

        tools = agent.mcp.schemas()
        if agent.is_main and allow_delegate:
            tools = [*tools, DELEGATE_SCHEMA]
        elif (not agent.is_main) and mode in ("task", "collab"):
            # 双模式：task（辩论附属任务）/ collab（合作模式子任务）都注入回报工具；
            # 合作模式额外注入认领工具（主动从任务池领取任务）
            tools = [*tools, REPORT_SCHEMA]
            if mode == "collab":
                tools = [*tools, CLAIM_SCHEMA]

        content = ""
        delegate = []
        report = None
        claim = []
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

        # P1-4 工具调用可视化：流式中首次产出工具名 / 工具执行结束时推送到前端。
        # tool_call_start 同样先补发 speech_start，保证气泡在纯工具回合（无文本）时也存在。
        async def _emit_tool_start(name: str, index: int = 0):
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
                "type": "tool_call_start",
                "speaker": agent.name,
                "round": self.round,
                "is_main": agent.is_main,
                "name": name,
                "index": index,
            })

        async def _emit_tool_end(name: str, summary: str = ""):
            await self.event_bus.publish({
                "type": "tool_call_end",
                "speaker": agent.name,
                "round": self.round,
                "name": name,
                "summary": _shorten(summary, 120),
            })

        max_loops = max(1, int(settings.TOOL_LOOP_MAX))
        for _loop_idx in range(max_loops):
            try:
                if stream:
                    try:
                        result = await self._consume_stream(
                            messages, agent.model_config, tools, _emit_delta,
                            on_tool_start=_emit_tool_start,
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
                self._record_error(f"llm:{agent.name}", e)
                # 模型级健康度：把失败按 model_id 记账，花名册 / UI 能直接看到
                # 「这个席位上次调用挂了、为什么」，不必翻日志。
                try:
                    from muti_llm_agent.models_registry import record_failure
                    record_failure(
                        agent.model_config.get("model_id", ""),
                        agent.model_config.get("provider", ""), str(e))
                except Exception:
                    pass
                # 把错误同步推送到前端（醒目的错误气泡），避免只在控制台可见
                await self.event_bus.publish({
                    "type": "agent_error",
                    "agent": agent.name,
                    "model_id": agent.model_config.get("model_id", ""),
                    "error": str(e),
                })
                break

            # LLM 调用次数统计：与 agent_metrics 分开记 —— 后者只统计「有文本产出、
            # 拿到 TTFT」的调用（用于裁判的模型质量评分），纯工具调用回合没有 TTFT，
            # 但仍是一次真实调用，复盘与费用分析需要看到它。
            self.llm_call_counts[agent.name] = \
                self.llm_call_counts.get(agent.name, 0) + 1

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

            # P1-4：流式中已展示过的工具索引（非流式/流式回退时为空 → 全部在执行前补发 start）
            streamed = set(result.get("tool_starts") or [])
            special = False
            for i, tc in enumerate(tcs):
                if i not in streamed:
                    await _emit_tool_start(tc["name"], i)
                if agent.is_main and tc["name"] == "delegate_task":
                    delegate.append(tc["arguments"])
                    special = True
                    await _emit_tool_end(
                        tc["name"],
                        f"分派任务 → {tc['arguments'].get('recipient', '?')}")
                elif (not agent.is_main) and tc["name"] == "report_result":
                    report = tc["arguments"]
                    special = True
                    await _emit_tool_end(tc["name"], "回报任务结果")
                elif (not agent.is_main) and tc["name"] == "claim_task":
                    claim.append(tc["arguments"])
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
            if not tcs:
                break
        else:
            # for 循环跑满仍未 break：说明模型在持续调用普通工具（未产出终态）
            self._log(f"[工具循环] {agent.name} 达到工具调用上限 {max_loops} 轮，"
                      "按已有内容收尾", level="warning", event="llm.tool_loop_limit")
            await self.event_bus.publish({
                "type": "agent_error",
                "agent": agent.name,
                "error": f"工具调用超过 {max_loops} 轮上限，已按当前结果收尾",
            })

        # 公平性 L1 兜底：discussion 模式下即使 LLM 输出超长也按公平性字符上限截断。
        # PRO/CON 用相对上限（对手近 N 轮有效输出均值 × 比例，与硬上限取 min）；
        # HOST/NEUTRAL 无对手参照，用硬性字符上限兜底（同样抑制整体消耗）。
        # 双模式：合作模式（collaboration/collab）无 PRO/CON 对称性，一律用硬上限。
        if content and (mode == "discussion" or mode in ("collaboration", "collab")):
            if mode == "discussion" and agent.stance in ("PRO", "CON"):
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

        # 流式收尾：补发 speech_end（前端用完整文本替换），并记录 transcript。
        # P1-4：纯工具回合（仅调用工具未产出文本）也以 emitting=True 触发 speech_end，
        # 避免气泡悬空（工具调用摘要已通过 tool_call_start/end 展示）。
        if emitting:
            await self.event_bus.publish({
                "type": "speech_end",
                "speaker": agent.name,
                "round": self.round,
                "text": content,
                "tokens": token_cost,          # 本次发言累计 token 用量（实时显示）
                "output_tokens": self.output_tokens,  # 全局累计输出 token（实时显示）
                "cost_rmb": _estimate_cost_rmb(self.spent, self.output_tokens),  # P1-6 预估费用（¥）
            })
            if content:
                self.transcript.append({
                    "type": "speech", "round": self.round, "speaker": agent.name,
                    "is_main": agent.is_main, "text": content,
                })

        agent.append_history("user", user_msg, round_no=self.round)
        if content:
            agent.append_history("assistant", content, round_no=self.round)

        # ---- 纯动作回合的复盘可见性（多模型协同常见：模型只发工具调用、不写正文）----
        # 若本回合既没有正文也尚未记录任何发言，但确实做了「分派 / 认领 / 回报」这类
        # 有语义的动作，就把动作摘要写入 transcript 并推一条 speech 事件。
        # 否则复盘里这场运行会显示「0 次发言」，看上去像什么都没发生。
        if not content and not emitting:
            actions = []
            for d in delegate:
                actions.append(
                    f"分派任务 → {d.get('recipient') or '任务池'}："
                    f"{_shorten(str(d.get('content') or ''), 80)}")
            for c in claim:
                actions.append(f"认领任务：{c.get('var_name') or c.get('task_id') or '?'}")
            if report:
                actions.append(
                    f"回报结果：{report.get('var_name') or '?'} = "
                    f"{_shorten(str(report.get('var_value') or ''), 80)}")
            if actions:
                action_text = "（本回合仅执行动作，无正文）\n" + "\n".join(actions)
                self.transcript.append({
                    "type": "action", "round": self.round, "speaker": agent.name,
                    "is_main": agent.is_main, "text": action_text,
                })
                self._runlog("action", speaker=agent.name,
                             is_main=bool(agent.is_main),
                             text=action_text[:300])
                await self.event_bus.publish({
                    "type": "speech", "round": self.round, "speaker": agent.name,
                    "is_main": agent.is_main, "text": action_text,
                })

        return {"content": content, "delegate": delegate, "report": report,
                "claim": claim, "streamed": emitting, "token_cost": token_cost}

    # ---------------- 主 Agent 指令 ----------------
    def _main_directive(self, round, is_last, had_any_task, pending_exists):
        """辩论模式主持人指令（文案与用词见 muti_llm_agent.prompts.main_directive）。"""
        return _main_directive_fn(self.topic, self.max_rounds, round,
                                  pending_exists=bool(pending_exists))

    # ---------------- 辩论上下文构建 ----------------
    def _record_argument(self, speaker: str, text: str, round_no: int):
        if not text:
            return
        self.round_arguments.setdefault(round_no, {})[speaker] = text
        self.latest_arguments[speaker] = {"text": text, "round": round_no}

    # ---------------- 对手定向反驳（需求 2/3） ----------------
    # PRO↔CON 立场互为对手；NEUTRAL/HOST 无固定对手（定义收敛于 muti_llm_agent.config.OPPONENT_STANCE，§3.4）

    def _find_opponent(self, collab: Agent) -> Agent | None:
        """找到 collab 的立场对手：优先取上一轮对立场的最新发言人，空则回退全局最近。"""
        target = OPPONENT_STANCE.get(collab.stance)
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
        第 3 轮起额外抽取对手关键论据句（OPPONENT_ARGUMENT_SNIPPET_MAX_CHARS 字，§5.4），
        弥补 LLM 摘要丢失具体论点的不足。
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
            # 摘要轮补充关键论据句（§5.4）：原文中带序号/论据标记的句子，
            # 防止 LLM 摘要丢失具体论点导致反驳泛泛而谈；第 2 轮原文片段已足够。
            "arguments": _extract_argument_sentences(
                text, settings.OPPONENT_ARGUMENT_SNIPPET_MAX_CHARS) if self.round >= 3 else "",
        }

    def _evidence_for_round(self, round_no: int, stance: str = "") -> str:
        """取第 round_no 轮的 RAG 证据文本；按调用者立场取对应证据桶（§5.3）。

        无立场或该立场无证据时回退到默认桶（旧数据 / 未分桶场景）。
        """
        items = self.round_evidence.get(round_no)
        if not items:
            return ""
        bucket = items.get(stance) if stance else None
        if not bucket:
            bucket = items.get("default")
        if not bucket:
            return ""
        return rag_service.evidence_text(bucket)

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
                # 摘要轮补充关键论据句（§5.4）：明确列出对方具体论点，要求逐条回应，
                # 避免只攻击摘要里的一句话而遗漏其余论点。
                if opp_prep.get("arguments"):
                    parts.append(f"对方发言中的关键论据句（请逐条回应，不要遗漏）：\n"
                                 f"{opp_prep['arguments']}")
                parts.append(f"请以{persona}为出发点进行反驳：优先逐条回应对方发言中"
                             "与你立场相悖的核心观点，指出逻辑谬误（如滑坡谬误、稻草人）或提供反证"
                             "（可调用 knowledge_search 检索）。必须坚定维护你的「"
                             + stance_label + "」立场，禁止倒戈。")
            else:
                # 回退：无立场对手（NEUTRAL/多辩手场景）时沿用原「上一发言人」逻辑
                prev: dict | None = None
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
        # RAG 证据注入（上一轮核查结果；只注入与本人立场对应的证据桶，§5.3）
        ev = self._evidence_for_round(self.round - 1, collab.stance)
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

    async def _task_turn(self, collab: Agent):
        """任务回合（§5.8）：只处理该辩手的待办任务，不发言辩论。

        任务输出仅回填 shared_context 并发布 task_result 事件；
        不再调用 _record_argument / _record_memory——任务结果不进入
        round_arguments（避免污染收敛检测、RAG 核查与对手上下文），
        也不进入辩论记忆（避免任务细节暴露到辩论发言中）。
        """
        pending = [t for t in self.tasks
                   if not t["completed"] and t["recipient"] == collab.name]
        if not pending:
            return
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
                # 未回报：不计入辩论发言记录（§5.8），仅流式展示其过程输出
                if out["content"] and not out["streamed"]:
                    await self._emit_speech(collab, out["content"])
                task["rounds_waiting"] = task.get("rounds_waiting", 0) + 1
                if task["rounds_waiting"] >= 3:
                    task["completed"] = True
                    if task["var_name"] in self.shared_context:
                        self.shared_context[task["var_name"]]["value"] = "（超时未完成）"

    async def _debate_turn(self, collab: Agent):
        """辩论回合（§5.8）：立论 / 反驳发言，与任务回合完全分离。

        无论本轮回合是否处理过任务，辩手每轮都发言，保证严格交替。
        """
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
        args = self.round_arguments.get(self.round, {}) or {}
        if not args:
            return
        # 按立场分桶（§5.3）：只核查该立场辩手本轮发言，证据归入对应立场桶，
        # 下一轮注入时辩手只拿到与自己立场相关的核查结果，避免被对手证据干扰。
        by_stance: dict[str, list[str]] = {}
        for name, text in args.items():
            agent = self.get_agent(name)
            stance = agent.stance if agent is not None else "default"
            by_stance.setdefault(stance, []).append(text)
        buckets: dict[str, list] = {}
        total = 0
        for stance, texts in by_stance.items():
            combined = "\n".join(texts)
            try:
                evidence = await asyncio.to_thread(rag_service.check, combined, 3)
            except Exception as e:
                self._log(f"[RAG核查失败] 立场{stance}: {e}",
                          level="warning", event="rag.fail")
                continue
            if evidence:
                buckets[stance] = evidence
                total += len(evidence)
        if buckets:
            self.round_evidence[self.round] = buckets
            self._log(f"[RAG] 第{self.round}轮事实核查返回 {total} 条证据"
                      f"（{ {k: len(v) for k, v in buckets.items()} }）",
                      level="info", event="rag.check")

    # ---------------- 收敛检测（架构文档 §4.4；P0-3 支持降级模式） ----------------
    async def _check_convergence(self) -> bool:
        if self.round < 2:
            return False
        cur_args = self.round_arguments.get(self.round, {}) or {}
        prev_args = self.round_arguments.get(self.round - 1, {}) or {}
        if not cur_args or not prev_args:
            return False
        try:
            from muti_llm_agent.embeddings import embed_query, is_fallback
            mode = "text-similarity" if is_fallback() else "embedding"
            if mode != self.convergence_mode:
                self.convergence_mode = mode
                if mode == "text-similarity":
                    self._log("嵌入模型不可用，收敛检测降级为文本相似度 "
                              f"（difflib 字符级，阈值 {settings.CONVERGENCE_TEXT_SIMILARITY}）",
                              level="warning", event="convergence.degraded")

            async def _sim(a: str, b: str) -> float:
                if mode == "text-similarity":
                    return _text_similarity(a, b)
                import numpy as np
                # 向量化在 worker 线程执行，避免阻塞事件循环（§2.4）
                v_a = np.array(await asyncio.to_thread(embed_query, a))
                v_b = np.array(await asyncio.to_thread(embed_query, b))
                return float(np.dot(v_a, v_b) /
                             (np.linalg.norm(v_a) * np.linalg.norm(v_b) + 1e-9))

            # 按发言者逐人计算（§5.7）：连续两轮都有发言的每个辩手单独算相似度再取平均，
            # 避免拼接全文时单一长文本主导而误判收敛；无共同发言者时回退整轮聚合对比。
            common = [n for n in cur_args if n in prev_args]
            if settings.CONVERGENCE_PER_SPEAKER and common:
                sims = [await _sim(cur_args[n], prev_args[n]) for n in common]
                sim = sum(sims) / len(sims)
            else:
                sim = await _sim(" ".join(cur_args.values()), " ".join(prev_args.values()))
        except Exception:
            return False
        # 新证据检测：本轮 RAG 若返回了新证据，则不视为无效循环
        has_new_evidence = bool(self.round_evidence.get(self.round))
        threshold = (settings.CONVERGENCE_TEXT_SIMILARITY
                     if self.convergence_mode == "text-similarity"
                     else settings.CONVERGENCE_SIMILARITY)
        if sim >= threshold and not has_new_evidence:
            self.convergence_streak += 1
            self._log(f"[收敛检测] 第{self.round}轮与上一轮相似度={sim:.3f} "
                      f"（连续{self.convergence_streak}轮，模式={self.convergence_mode}）",
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
        # 并发闸门：已有未结束的讨论（running / paused / run 任务未退出）时不再启动
        # 第二个主循环，否则两个循环会同时推进 round、写同一份 transcript 与遥测。
        if self.is_active:
            self._log("[启动忽略] 本会话已有进行中的讨论（running/paused），"
                      "请先结束再开始新一轮", level="warning", event="run.reject")
            return
        if max_rounds is not None:
            self.max_rounds = max(1, int(max_rounds))
        if topic:
            self.topic = topic
        # 单场运行态清零（与 configure 共用）：不重新配置直接再跑一场时，
        # 上一场的 transcript / tasks / 各 Agent 历史都不再带入新一场。
        # keep_followup：续轮场景下承接包在 start_followup 里已写好，不能被清掉。
        self._reset_run_state(keep_followup=bool(self.followup_seed_active))
        from muti_llm_agent.logger import set_log_context
        set_log_context(trace_id=self.trace_id, session_id=self.session_id)
        self.phase = "preparing"
        self._started_at = time.time()
        self.run_stats = {"compressions": 0, "started_at": time.time()}
        self.status = "running"
        # 运行事件日志头部：复盘时间线的锚点（无索引也能自描述）
        try:
            from muti_llm_agent.runlog import write_header
            write_header(
                self.trace_id, session_id=self.session_id, topic=self.topic,
                mode=self.mode, iteration=self.iteration_no,
                agents=[{"name": a.name, "role": a.role, "stance": a.stance,
                         "model_id": a.model_config.get("model_id", "")}
                        for a in self.agents])
        except Exception:
            pass
        self._runlog("run_start", text=f"开始运行（第 {self.iteration_no} 轮迭代）",
                     topic=self.topic, mode=self.mode, max_rounds=self.max_rounds,
                     steering_pending=len(self.steering.pending()))
        self._run_task = asyncio.current_task()  # 供 is_active 判定「run 是否仍在跑」
        self._pause_event.set()
        self._persist_meta()
        await self.event_bus.publish({
            "type": "status", "status": "running", "phase": self.phase,
            "topic": self.topic, "max_rounds": self.max_rounds,
            "trace_id": self.trace_id,
            "agents": [{"name": a.name, "role": a.role, "is_main": a.is_main,
                        "stance": a.stance} for a in self.agents],
        })
        await self.event_bus.publish(self._status_sync())

        self.last_main_message = self.topic

        try:
            # 双模式：debate（辩论主循环）/ collaboration（合作主循环）
            if self.mode == "collaboration":
                await self._run_collab_loop()
            else:
                await self._run_debate_loop()

            # ---- Judging：终止后单次调用裁判（架构文档 §4.1/§6.2）。
            #      stopped 分支两模式共享；合作模式无裁判，走 _collab_final_summary 直接收尾 ----
            if self.status == "stopped":
                # 手动结束（end()）：跳过裁判裁决与主持人汇总，直接收尾，
                # 避免旧 run 任务在重新配置/新讨论启动后继续污染编排器状态
                self.phase = "idle"
                await self.event_bus.publish({"type": "status", "status": "stopped", "phase": self.phase})
                await self.event_bus.publish(self._status_sync())
            elif self.mode == "collaboration":
                # ---- Finished（合作模式）：主 Agent 三段式汇总，无裁判裁决 ----
                self.phase = "finished"
                summary = await self._collab_final_summary()
                await self._emit_summary(summary)
                # 先解除 run 占用再改终态：避免状态已是 done、run 任务却还没退出时，
                # 紧接着到来的 /api/configure 被 is_active 误判为「讨论进行中」。
                self._run_task = None
                self.status = "done"
                await self.event_bus.publish({"type": "status", "status": "done", "phase": self.phase})
                await self.event_bus.publish(self._status_sync())
                self._persist_meta()
                self._notify_done()
            else:
                self.phase = "judging"
                await self.event_bus.publish({"type": "status", "status": "running", "phase": self.phase})
                judgment = await self._judge_round()
                self.judgment = judgment
                self._runlog("judge", winner=judgment.winner,
                             composite=getattr(judgment, "composite_scores", {}),
                             text=(judgment.critique or "")[:300])
                await self.event_bus.publish({
                    "type": "judgment",
                    "judge": self.judge.name if self.judge else self.main_agent.name,
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
                self._run_task = None  # 解除 run 占用（理由同上）
                self.status = "done"
                await self.event_bus.publish({"type": "status", "status": "done", "phase": self.phase})
                await self.event_bus.publish(self._status_sync())
                self._persist_meta()
                self._notify_done()
        except Exception as e:
            self._log(f"[编排器错误] {e}", level="error", event="orchestrator.error")
            self._record_error("run_discussion", e)
            self.status = "stopped"
            self.phase = "idle"  # 异常熔断回到 Idle（架构文档 §4.1）
            self._persist_meta()
            await self.event_bus.publish({"type": "status", "status": "stopped", "phase": self.phase})
        finally:
            # 任何退出路径都必须释放 run 占用，否则该会话会被永久判为「讨论进行中」
            self._run_task = None
            self.ended_at = time.time()
            await self._finalize_run()

    async def _finalize_run(self):
        """运行收尾：轮次级指令收尾 + 摘要落索引 + 广播 run_summary。

        所有退出路径（正常完成 / 手动结束 / 异常熔断）共用这一处，
        保证「每场运行都以一条 run_summary 事件与一条索引记录收尾」。
        """
        try:
            self.steering.end_round(self.round)
            self.followup_seed_active = ""
        except Exception:
            pass
        summary: dict = {}
        try:
            summary = self.summary()
        except Exception as e:
            self._record_error("summary", e)
        if summary:
            try:
                from muti_llm_agent.runlog import register_run
                register_run(summary)
            except Exception:
                pass
            try:
                self._runlog(
                    "run_end", text=f"运行结束：{summary.get('status')}",
                    rounds=summary.get("rounds_executed"),
                    output_tokens=(summary.get("tokens") or {}).get("output", 0),
                    cost_rmb=summary.get("cost_rmb"),
                    errors=summary.get("error_count"))
            except Exception:
                pass
            try:
                await self.event_bus.publish({"type": "run_summary", "summary": summary})
            except Exception:
                pass
        try:
            self._persist_meta()
        except Exception:
            pass

    # ---------------- 会话结束后的「补充材料续轮」（followup.py） ----------------
    def start_followup(self, materials=None, focus: str = "",
                       max_rounds: int | None = None) -> dict:
        """归档当前迭代并准备下一轮迭代的承接包（不启动主循环，由调用方 start）。

        Returns:
            {"iteration", "trace_id", "focus", "materials_chars", "max_rounds",
             "previous_trace_id", "notes"}
        Raises:
            ValueError: 不满足续轮条件（运行中 / 无内容 / 超过最大迭代次数）
        """
        validate_followup(self)
        text, chars = normalize_materials(materials)
        focus = (focus or "").strip()
        if not text and not focus:
            raise ValueError("补充材料与聚焦点不能同时为空")

        previous_trace = self.trace_id
        # 1) 归档当前迭代（必须在清场之前，快照依赖 transcript / conclusion）
        snap = snapshot_iteration(self)
        self.iterations.append(snap)
        self._runlog("iteration", action="archive", archive_iteration=snap["iteration"],
                     text=f"第 {snap['iteration']} 轮迭代归档（{snap['rounds']} 轮）",
                     rounds=snap["rounds"], output_tokens=snap["output_tokens"])

        # 2) 压缩承接包：上一轮结论 / 分歧 / 末轮要点 / 交付结果
        carryover = build_carryover(self)
        seed = build_seed_block(text, focus, carryover)

        # 3) 清场（保留席位 / 模型 / 模式 / 迭代链），写入承接包
        self._reset_run_state()
        self.followup_seed_active = seed
        self.followup_focus = focus
        self.followup_materials_chars = chars
        self.iteration_no = snap["iteration"] + 1
        if max_rounds is not None:
            self.max_rounds = max(1, int(max_rounds))
        self.status = "idle"
        self.phase = "idle"
        self._persist_meta()

        info = {
            "session_id": self.session_id,
            "iteration": self.iteration_no,
            "previous_iteration": snap["iteration"],
            "previous_trace_id": previous_trace,
            "trace_id": self.trace_id,
            "focus": focus,
            "materials_chars": chars,
            "carryover_chars": len(carryover),
            "max_rounds": self.max_rounds,
            "notes": [
                f"已归档第 {snap['iteration']} 轮迭代（{snap['rounds']} 轮 / "
                f"输出 {snap['output_tokens']} tokens）",
                f"承接包 {len(carryover)} 字，补充材料 {chars} 字",
            ],
        }
        self._log(f"补充材料续轮：第 {info['previous_iteration']} 轮迭代已归档，"
                  f"进入第 {info['iteration']} 轮（材料 {chars} 字）",
                  level="info", event="followup.prepared")
        _spawn_safe(self.event_bus.publish({
            "type": "iteration", "action": "prepared",
            "info": info, "iterations": list(self.iterations),
        }))
        return info

    async def _run_debate_loop(self):
        """辩论模式主循环（原 run_discussion while 循环体，行为逐字节不变）。"""
        had_any_task = False
        while self.round < self.max_rounds:
            # 暂停 / 结束 控制
            await self._pause_event.wait()
            if self.status == "stopped":
                break

            # 上一轮收尾：high 优先级指令在整轮生效后于此处消费；
            # 续轮承接包只在第 1 轮注入，之后立即失效（避免每轮重复灌入）。
            if self.round:
                self.steering.end_round(self.round)
            if self.round >= 1:
                self.followup_seed_active = ""

            self.round += 1
            self.phase = "debating"
            self._runlog("round_start", round=self.round, max=self.max_rounds)
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
            main_out = await self._agent_turn(self.main_agent, directive, mode="discussion", stream=True)
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
                    "from": self.main_agent.name,
                    "to": task["recipient"],
                    "content": task["content"],
                    "expected": task["expected_outcome"],
                    "var": task["var_name"],
                    "task_id": task["task_id"],
                    "timestamp": task["timestamp"],
                })
            self._record_memory(self.main, main_out["content"])
            self._record_argument(self.main_agent.name, main_out["content"], self.round)

            # 2) 协作 Agent 回合（§5.8 拆分：任务回合 → 辩论回合，两者完全分离）
            # 2a) 任务回合：处理各辩手待办任务；任务输出不回填 round_arguments，
            #     不污染收敛检测 / RAG 核查 / 对手上下文
            if any(not t["completed"] for t in self.tasks):
                for c in self.collaborators:
                    await self._task_turn(c)
            # 2b) 辩论回合（严格交替：正方/反方轮流发言，其余立场排后）
            if self.round == 1:
                # 第 1 轮并行立论（架构文档 §4.2 / §6.2）
                await asyncio.gather(
                    *[self._debate_turn(c) for c in self.collaborators],
                    return_exceptions=True,
                )
                # 确定第 2 轮首位发言人的"上一发言人"（交替顺序的末位）
                order = self._debate_order()
                if order:
                    self.last_debate_speaker = order[-1].name
            else:
                # 第 2 轮起严格交替串行发言：正/反方轮流，其余立场最后
                for c in self._debate_order():
                    await self._debate_turn(c)

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

    async def _run_collab_loop(self):
        """合作模式主循环：主 Agent 拆解任务入池 → 子 Agent 认领并完成。

        结束判定：所有任务 completed 即收尾（防空任务误判：self.tasks and all(...)），
        max_rounds 兜底。无裁判、无收敛检测、无立场对抗。
        """
        while self.round < self.max_rounds:
            # 暂停 / 结束 控制
            await self._pause_event.wait()
            if self.status == "stopped":
                break

            # 上一轮收尾：high 优先级指令在整轮生效后于此处消费；
            # 续轮承接包只在第 1 轮注入，之后立即失效（避免每轮重复灌入）。
            if self.round:
                self.steering.end_round(self.round)
            if self.round >= 1:
                self.followup_seed_active = ""

            self.round += 1
            self.phase = "collaborating"
            self._runlog("round_start", round=self.round, max=self.max_rounds)
            is_last = (self.round >= self.max_rounds)
            await self.event_bus.publish({
                "type": "round", "round": self.round, "max": self.max_rounds,
            })
            await self.event_bus.publish(self._status_sync())

            # 1) 主 Agent 回合：拆解/补充子任务（delegate_task recipient 留空 → 任务池），或统筹协调
            directive = self._collab_main_directive(self.round, is_last)
            main_out = await self._agent_turn(self.main_agent, directive, mode="collaboration",
                                              stream=True)
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
                    "from": self.main_agent.name,
                    "to": task["recipient"],  # 空 = 任务池（待认领）
                    "content": task["content"],
                    "expected": task["expected_outcome"],
                    "var": task["var_name"],
                    "task_id": task["task_id"],
                    "timestamp": task["timestamp"],
                })
            self._record_memory(self.main, main_out["content"])

            # 2) 子 Agent 回合：认领（claim_task）+ 完成回报（report_result）
            for c in self.collaborators:
                await self._collab_work_turn(c)

            # 3) 结束判定：所有任务 completed 即收尾（防空任务误判）；max_rounds 兜底
            if self.tasks and all(t["completed"] for t in self.tasks):
                self._log("合作模式：所有任务已完成，提前收尾",
                          level="info", event="collab.all_done")
                break
            if is_last or self.round >= self.max_rounds:
                break

            await self.event_bus.publish(self._status_sync())
            await asyncio.sleep(0)

    # ---------------- 合作模式：主 Agent 指令 / 子 Agent 回合 ----------------
    def _collab_main_directive(self, round, is_last) -> str:
        """合作模式主 Agent 指令（文案见 muti_llm_agent.prompts.collab_main_directive）。"""
        return _collab_main_directive_fn(self.topic, self.max_rounds, round)

    def _collab_work_prompt(self, collab: Agent, claimed: list, claimable: list) -> str:
        """合作模式子 Agent 回合 prompt（文案见 muti_llm_agent.prompts.collab_work_prompt）。"""
        return _collab_work_prompt_fn(self.round, self.max_rounds, claimed, claimable)

    async def _collab_work_turn(self, collab: Agent):
        """合作模式子 Agent 回合：一轮单次 LLM 调用。

        先解析 claim（认领），再解析 report（回报），支持同轮认领后立即回报；
        未回报的任务累积 rounds_waiting，>=3 轮强制完成（超时兜底）。
        """
        claimed = [t for t in self.tasks
                   if t.get("status") == "claimed" and t["recipient"] == collab.name
                   and not t["completed"]]
        claimable = [t for t in self.tasks if t.get("status") == "pending"]
        umsg = self._collab_work_prompt(collab, claimed, claimable)
        out = await self._agent_turn(collab, umsg, mode="collab", stream=True)

        # 1) 认领（可多个）
        for args in out["claim"]:
            self._apply_claim(collab, args)

        # 2) 回报：认领后可能同轮立即回报，故此处重新刷新 claimed
        reported = None
        reflected = None  # 反思元信息（None=未反思/失败；dict=本次执行了反思）
        if out["report"]:
            task = self._find_task_by_var(out["report"].get("var_name") or "")
            if task is not None and not task["completed"]:
                if task.get("status") == "claimed" and task["recipient"] == collab.name:
                    rep, reflected = await self._reflect_report(collab, task, out["report"])
                    self._apply_report(task, rep)
                    reported = task
                elif task.get("status") == "pending":
                    # 同轮「认领 + 回报」：先补认领再反思修订、落库
                    self._apply_claim(collab, {"task_id": task["id"]})
                    rep, reflected = await self._reflect_report(collab, task, out["report"])
                    self._apply_report(task, rep)
                    reported = task
        if reported is not None:
            msg = reported.get("result_message", "")
            if msg and not out["streamed"]:
                await self._emit_speech(collab, msg)
            await self.event_bus.publish({
                "type": "task_result",
                "from": collab.name,
                "var": reported["var_name"],
                "value": reported.get("result_value", ""),
                "completed": bool(reported["completed"]),
                "message": msg,
                "task_id": reported["task_id"],
                "timestamp": now_ms(),
                "token_cost": out.get("token_cost", 0),
            })
            # 反思修订事件（§12.18）：仅在实际执行了反思时发布
            if reflected is not None:
                await self.event_bus.publish({
                    "type": "task_revised",
                    "from": collab.name,
                    "var": reported["var_name"],
                    "task_id": reported["task_id"],
                    "value": reported.get("result_value", ""),
                    "reflections": reflected["reflections"],
                    "revised": reflected["revised"],
                    "timestamp": now_ms(),
                })
        else:
            # 未回报：仅流式展示过程输出；认领任务超时强制完成
            if out["content"] and not out["streamed"]:
                await self._emit_speech(collab, out["content"])
            for t in self.tasks:
                if t.get("status") == "claimed" and t["recipient"] == collab.name \
                        and not t["completed"]:
                    t["rounds_waiting"] = t.get("rounds_waiting", 0) + 1
                    if t["rounds_waiting"] >= 3:
                        t["completed"] = True
                        t["status"] = "completed"
                        if t["var_name"] in self.shared_context:
                            self.shared_context[t["var_name"]]["value"] = "（超时未完成）"
                        self._log(f"[任务超时] 任务#{t['id']} 由 {collab.name} 认领但多轮未回报，"
                                  "强制标记完成", level="warning", event="task.timeout")

    def _apply_claim(self, collab: Agent, args: dict):
        """把任务池中的 pending 任务标记为 claimed（认领者独占负责）。"""
        tid = args.get("task_id")
        task = next((t for t in self.tasks
                     if t.get("status") == "pending"
                     and (t["id"] == tid or t["task_id"] == str(tid))), None)
        if task is None:
            self._log(f"[任务认领忽略] {collab.name} 认领的任务 #{tid} 不存在或已被认领",
                      level="warning", event="task.claim_ignore")
            return
        task["recipient"] = collab.name
        task["status"] = "claimed"
        task["rounds_waiting"] = 0
        self._log(f"[任务认领] {collab.name} 认领了任务#{task['id']}（{task['var_name']}）",
                  level="info", event="task.claim")
        _spawn_safe(self.event_bus.publish({
            "type": "task_claimed",
            "from": collab.name,
            "task_id": task["task_id"],
            "var": task["var_name"],
            "timestamp": now_ms(),
        }))

    def _find_task_by_var(self, var_name: str) -> dict | None:
        for t in self.tasks:
            if t["var_name"] == var_name:
                return t
        return None

    # ---------------- 合作模式收尾 ----------------
    async def _collab_final_summary(self) -> str:
        """合作模式收尾：主 Agent 汇总三段式报告（无裁判裁决）。"""
        directive = collab_summary_directive(self.topic, self.tasks)
        out = await self._agent_turn(self.main_agent, directive, mode="collaboration",
                                     allow_delegate=False)
        content = out["content"] or ""
        if not content.strip():
            return await self._fallback_collab_summary()
        return await self._normalize_collab_conclusion(content)

    async def _fallback_collab_summary(self) -> str:
        """合作模式确定性兜底汇总：不调用 LLM，直接按任务记录拼装三段式报告。"""
        report = await self._normalize_collab_conclusion("")
        return ("> 注：本报告由系统按任务记录确定性生成（未调用主 Agent LLM 汇总）。\n\n"
                + report)

    async def _normalize_collab_conclusion(self, text: str) -> str:
        """把合作总结文本规范为三段式（缺段补兜底），并发布 conclusion 事件。"""
        sections = self._split_sections(text)
        core = sections.get("核心结论", "整体任务已完成，各子任务结果由协作 Agent 回报。")
        tasks_section = sections.get("子任务与成果", "（主 Agent 未给出子任务明细）")
        pending = sections.get("遗留问题与后续建议", "暂无遗留问题。")
        self.conclusion = {
            "core": core,
            "positions": tasks_section,      # 复用字段：合作模式下为子任务与成果
            "judge_ruling": "",
            "pending": pending,
        }
        await self.event_bus.publish({
            "type": "conclusion",
            "core": core,
            "positions": tasks_section,
            "judge_ruling": "",
            "winner": None,
            "round": self.round,
        })
        return (f"## 核心结论\n\n{core}\n\n"
                f"## 子任务与成果\n\n{tasks_section}\n\n"
                f"## 遗留问题与后续建议\n\n{pending}\n")

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
    @staticmethod
    def _score_ttft(ttft_s: float) -> float:
        """TTFT（秒）→ 1-10 分（实现见 muti_llm_agent.scoring.score_ttft）。"""
        return score_ttft(ttft_s)

    @staticmethod
    @staticmethod
    def _score_rate(rate: float) -> float:
        """生成速率（字符/秒）→ 1-10 分（实现见 muti_llm_agent.scoring.score_rate）。"""
        return score_rate(rate)

    def _model_performance(self) -> dict:
        """聚合各辩手流式实测指标（实现见 muti_llm_agent.scoring.model_performance）。"""
        return model_performance(self.agent_metrics)

    def _performance_text(self) -> str:
        """供裁判参考的模型性能文本（实现见 muti_llm_agent.scoring.performance_text）。"""
        return performance_text(self._model_performance(), self.main_agent.name)

    def _compute_composite(self, judgment: Judgment) -> Judgment:
        """综合评分与胜方推导（实现见 muti_llm_agent.scoring.compute_composite）。"""
        names = [a.name for a in self.collaborators]
        return compute_composite(
            judgment, names, self._model_performance(),
            lambda n: getattr(self.get_agent(n), "stance", ""))

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
        judge_name = self.judge.name if self.judge else self.main_agent.name
        judge_model = self.judge.model_config if self.judge else self.main_agent.model_config
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
        # P15：校验接收方存在，避免 _apply_report 中 get_agent 返回 None。
        # 双模式：recipient 留空 = 放入共享任务池（合作模式），跳过校验，由协作 Agent 主动认领。
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
            # 双模式状态机：recipient 非空 = 已指定分派（claimed）；为空 = 待认领（pending）
            "status": "claimed" if recipient else "pending",
            "content": d.get("content", ""),
            "expected_outcome": d.get("expected_outcome", ""),
            "var_name": var_name,
            "completed": False,
            "result_value": "",
            "result_message": "",
            "rounds_waiting": 0,
            "reflections": 0,          # 已执行的自我反思轮数（合作模式 §12.18）
            "reflection_log": [],      # [{"pass","summary","revision","revised","timestamp"}]
            "timestamp": now_ms(),
            "token_cost": 0,
        }
        self.tasks.append(task)
        self.shared_context[var_name] = {
            "by": self.main_agent.name, "value": "", "task_id": tid,
        }
        self._runlog("task_assigned", **{"from": self.main_agent.name},
                     to=recipient or "任务池", task_id=task["task_id"],
                     var=var_name, content=_shorten(task["content"], 160))
        return task

    async def _reflect_report(self, collab: Agent, task: dict, report: dict):
        """合作模式反思与自我修正（§12.18）：对照期望效果对草稿做至多 N 轮自评修订。

        每轮把上一轮修订版作为新草稿继续自评，直到达到 REFLECTION_MAX_PASSES 上限
        或收敛（无修订 / 修订等于当前草稿）提前终止。

        返回 (report, meta)：
          - meta=None  未反思（被禁用 / 已达上限 / 首轮调用即异常），report 保持原草稿；
          - meta={"revised": bool, "reflections": int, "token_cost": int}
            已执行 ≥1 次反思（reflections 为本轮实际完成的轮数）。
        任一反思调用异常或输出无法解析时，保留当前最佳草稿照常落库，不阻塞任务完成。
        """
        if not (settings.REFLECTION_ENABLED
                and task["reflections"] < settings.REFLECTION_MAX_PASSES):
            return report, None
        original = report.get("var_value", "") or report.get("message", "")
        current = original
        last_explanation = ""
        passes_done = 0
        token_cost_total = 0
        try:
            # 非主 Agent + mode="collaboration" → 不注入任何工具（§12.4），非流式不产生气泡；
            # _apply_usage 自动把本次调用计入 spent/output_tokens 遥测，_run_llm_retry 内置重试。
            while (settings.REFLECTION_ENABLED
                   and task["reflections"] < settings.REFLECTION_MAX_PASSES):
                out = await self._agent_turn(
                    collab, _reflection_prompt(task, current),
                    mode="collaboration", allow_delegate=False, stream=False)
                parsed = _parse_reflection(out["content"])
                revision = (parsed.get("revision") or "").strip()
                if revision:
                    # 公平性 L1：修订版同样按硬性字符上限截断
                    revision, _ = self.governor.truncate_output(
                        revision, self.governor.hard_char_ceiling)
                task["reflections"] += 1  # 每次反思调用计一轮
                passes_done = task["reflections"]
                token_cost_total += int(out.get("token_cost", 0) or 0)
                changed = bool(revision) and revision != current
                if changed:
                    current = revision
                last_explanation = parsed.get("explanation") or last_explanation
                task["reflection_log"].append({
                    "pass": task["reflections"],
                    "summary": parsed.get("reflection", "")[:200],
                    "revision": (revision or current)[:200],
                    "revised": changed,
                    "timestamp": now_ms(),
                })
                if not changed:
                    break  # 收敛：无修订 / 修订无变化 → 提前终止
        except Exception as e:
            self._log(f"[反思失败] 任务#{task['id']}: {e}，保留当前草稿落库",
                      level="warning", event="reflection.fail")
        if passes_done == 0:
            return report, None  # 首轮即异常 → 不发布 task_revised
        revised = current != original
        if revised:
            report = {**report, "var_value": current,
                      "message": last_explanation or report.get("message", "")}
            self._record_memory(collab, f"任务#{task['id']}({task['var_name']}) 反思修订："
                                        f"{last_explanation[:100]}")
            self._log(f"[反思修订] 任务#{task['id']} 反思后修订结果",
                      level="info", event="reflection.revised")
        else:
            self._log(f"[反思通过] 任务#{task['id']} 反思后维持草稿",
                      level="info", event="reflection.passed")
        return report, {"revised": revised, "reflections": passes_done,
                        "token_cost": token_cost_total}

    def _apply_report(self, task, report: dict):
        var = report.get("var_name") or task["var_name"]
        val = report.get("var_value", "")
        task["completed"] = bool(report.get("completed", False))
        task["status"] = "completed" if task["completed"] else task.get("status", "claimed")
        task["result_value"] = val
        task["result_message"] = report.get("message", "")
        task["token_cost"] = int(report.get("token_cost", 0) or 0)
        if var in self.shared_context:
            self.shared_context[var]["value"] = val
        else:
            self.shared_context[var] = {"by": self.main_agent.name, "value": val, "task_id": task["id"]}
        # 协作 Agent 把结果沉淀进长期记忆
        agent = self.get_agent(task["recipient"])
        if agent is not None:
            agent.memory.add_long(
                f"完成任务#{task['id']}({var}): {val}", importance=0.85)
        else:
            self._log(f"[回报落库] 接收方 {task['recipient']} 已不存在，跳过记忆沉淀",
                      level="warning", event="report.skip")
        self._runlog("task_result", **{"from": task["recipient"] or "任务池"},
                     task_id=task["task_id"], var=var,
                     completed=bool(task["completed"]),
                     value=_shorten(str(val), 200))

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
            "session_id": self.session_id,
            "mode": self.mode,
            "phase": self.phase,
            "current_round": self.round,
            "iteration": self.iteration_no,
            "agent_states": {a.name: "IDLE" for a in self.agents},
            # 只读遥测：token 用量不再做任何约束，仅保留观测（实时显示）
            "spent_tokens": self.spent,            # 累计 token 用量（输入+输出）
            "output_tokens": self.output_tokens,   # 累计输出 token（实时）
            "cost_rmb": _estimate_cost_rmb(self.spent, self.output_tokens),  # P1-6 预估费用（¥）
            # 收敛检测（P0-3）：模式与连续相似轮数
            "convergence_mode": self.convergence_mode,
            "convergence_streak": self.convergence_streak,
            # 公平性治理报告（替代旧的 budget_plan 视图）
            "fairness": fair,
            # 中途注入纠偏的实时统计（UI 用它显示「待生效 / 已生效」徽标）
            "steering": self.steering.stats(),
            "iteration_no": self.iteration_no,
            "error_count": len(self.errors),
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
        self._runlog("speech", speaker=agent.name, is_main=bool(agent.is_main),
                     chars=len(text), text=text[:200])
        await self.event_bus.publish({
            "type": "speech", "round": self.round, "speaker": agent.name,
            "is_main": agent.is_main, "text": text,
        })

    async def _emit_summary(self, text: str):
        self.transcript.append({
            "type": "summary", "round": self.round, "speaker": self.main_agent.name,
            "is_main": True, "text": text,
        })
        self._runlog("summary", speaker=self.main_agent.name, chars=len(text or ""),
                     text=(text or "")[:200])
        await self.event_bus.publish({
            "type": "summary", "round": self.round, "speaker": self.main_agent.name, "text": text,
        })

    # ---------------- P0-1 结构化结论：四段式最终报告 ----------------
    @staticmethod
    @staticmethod
    def _split_sections(text: str) -> dict:
        """按一/二级 Markdown 标题切段（实现见 muti_llm_agent.conclusion.split_sections）。"""
        return _split_sections(text)

    @staticmethod
    @staticmethod
    def _judge_ruling_text(judgment) -> str:
        """权威裁决文本（实现见 muti_llm_agent.scoring.judge_ruling_text）。"""
        return judge_ruling_text(judgment)

    def _normalize_conclusion(self, text: str, judgment) -> str:
        """四段式最终报告规整（实现见 muti_llm_agent.conclusion.normalize_conclusion）。"""
        report, conclusion = _normalize_conclusion_text(
            text, judgment, self.transcript)
        self.conclusion = conclusion
        return report

    async def _final_summary(self, judgment=None):
        judge_section = ""
        if judgment:
            judge_section = (
                "\n\n裁判给出的裁决数据（权威，供你在「裁判裁决」段引用）：\n"
                + self._judge_ruling_text(judgment))
        directive = final_summary_directive(judge_section)
        out = await self._agent_turn(self.main_agent, directive, mode="discussion", allow_delegate=False)
        content = out["content"] or ""
        if not content.strip():
            # 模型未生成任何内容：走确定性兜底汇总
            report = self._fallback_summary(judgment)
        else:
            report = self._normalize_conclusion(content, judgment)
        # 发布结构化结论事件，供前端「核心结论」面板展示
        await self.event_bus.publish({
            "type": "conclusion",
            "core": self.conclusion["core"],
            "positions": self.conclusion["positions"],
            "judge_ruling": self.conclusion["judge_ruling"],
            "pending": self.conclusion["pending"],
            "winner": judgment.winner if judgment else None,
            "round": self.round,
        })
        return report

    def _fallback_summary(self, judgment=None) -> str:
        """确定性兜底汇总（实现见 muti_llm_agent.conclusion.fallback_summary）。"""
        report, conclusion = _fallback_summary_text(judgment, self.transcript)
        self.conclusion = conclusion
        return report

    # ---------------- 导出 Markdown ----------------
    def export_markdown(self) -> str:
        """导出 Markdown 记录（渲染实现在 muti_llm_agent.exporter.render_markdown）。"""
        return render_markdown(self)

