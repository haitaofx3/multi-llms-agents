"""
test_features_v2.py — P0/P1 新特性确定性测试（无需真实 LLM）

覆盖本次迭代新增的行为：
  - P1-6 成本估算：_estimate_cost_rmb 纯函数计价；
    /_status_sync 与 speech_end 事件携带 cost_rmb（只读遥测，无预算约束）；
  - P0-3 收敛降级：_text_similarity 字符相似度；嵌入不可用（is_fallback）时
    自动切换 text-similarity 模式并累计连续相似轮，新证据重置计数；
  - P1-4 工具调用可视化：流式已发 tool_call_start 不重复（tool_starts 去重）；
    流式失败回退非流式时补发 tool_call_start；纯工具回合也产出 speech_start/end；
  - P0-1 结构化结论：_normalize_conclusion 四段式回填、裁判裁决权威重写、
    缺失段落确定性兜底；_final_summary 发布 conclusion 事件。

运行方式（无需服务器、无需真实 LLM）：
    python -m unittest tests.test_features_v2 -v
"""
import asyncio
import re
import unittest
from unittest.mock import patch

from app.config import settings
from app.manager import manager
from app.orchestrator import _estimate_cost_rmb, _extract_argument_sentences, _text_similarity
from app.protocol import Judgment

# §1.3 解耦：会话实例统一由 DebateManager 注册表持有，不再导入模块级单例。
orchestrator = manager.get_or_create("default")

HOST_CFG = {"name": "主持", "role": "主持", "personality": "",
            "provider": "default", "stance": "HOST"}
PRO_CFG = {"name": "正方", "role": "正方", "personality": "",
           "provider": "default", "stance": "PRO"}
CON_CFG = {"name": "反方", "role": "反方", "personality": "",
           "provider": "default", "stance": "CON"}


def _fake_llm_result(content="发言"):
    return {"content": content, "tool_calls": [], "assistant_message": None,
            "usage": {"total_tokens": 1}}


class CostEstimateTest(unittest.TestCase):
    """P1-6 成本估算纯函数：输入/输出分别按每百万单价计价（只读展示）。"""
    def test_zero_cost(self):
        self.assertEqual(_estimate_cost_rmb(0, 0), 0.0)
        self.assertEqual(_estimate_cost_rmb(None, None), 0.0)

    def test_mixed_usage_pricing(self):
        total, output = 1_000_000, 250_000
        expected = (750_000 / 1e6 * settings.COST_INPUT_PER_1M
                    + 250_000 / 1e6 * settings.COST_OUTPUT_PER_1M)
        self.assertAlmostEqual(_estimate_cost_rmb(total, output), expected)

    def test_output_only(self):
        self.assertAlmostEqual(
            _estimate_cost_rmb(250_000, 250_000),
            250_000 / 1e6 * settings.COST_OUTPUT_PER_1M)

    def test_negative_clamped_to_zero(self):
        self.assertEqual(_estimate_cost_rmb(-100, -50), 0.0)


class StatusAndSpeechCostTest(unittest.IsolatedAsyncioTestCase):
    """cost_rmb 出现在 _status_sync 与 speech_end 事件中。"""

    async def _configure(self):
        orchestrator.configure([HOST_CFG, PRO_CFG], topic="成本遥测", max_rounds=3)
        q = orchestrator.event_bus.subscribe()
        await self._drain(q)
        return q

    @staticmethod
    async def _drain(q):
        while True:
            try:
                await asyncio.wait_for(q.get(), timeout=0.1)
            except asyncio.TimeoutError:
                break

    async def test_status_sync_carries_cost_rmb(self):
        await self._configure()
        orchestrator.spent = 800
        orchestrator.output_tokens = 200
        s = orchestrator._status_sync()
        self.assertIn("cost_rmb", s)
        self.assertAlmostEqual(s["cost_rmb"], _estimate_cost_rmb(800, 200))
        # 只读遥测：不因成本估算产生任何预算/约束字段
        self.assertNotIn("budget", s)

    async def test_speech_end_carries_cost_rmb(self):
        q = await self._configure()
        orch = orchestrator
        orch.round = 1

        async def fake_consume(messages, model_config, tools=None, on_delta=None,
                               on_tool_start=None, **kwargs):
            if on_delta:
                await on_delta("发言文本")
            return {"content": "发言文本", "tool_calls": [], "assistant_message": None,
                    "usage": {"total_tokens": 100, "completion_tokens": 40},
                    "tool_starts": []}

        orch._consume_stream = fake_consume
        orch._run_llm_retry = lambda *a, **k: _fake_llm_result()
        out = await orch._agent_turn(orch.collaborators[0], "请发言",
                                     mode="discussion", stream=True)
        self.assertEqual(out["content"], "发言文本")

        while True:
            ev = await asyncio.wait_for(q.get(), timeout=2)
            if ev["type"] == "speech_end":
                break
        self.assertIn("cost_rmb", ev)
        self.assertIn("tokens", ev)
        self.assertIn("output_tokens", ev)
        self.assertEqual(orch.spent, 100)
        self.assertEqual(orch.output_tokens, 40)
        self.assertAlmostEqual(ev["cost_rmb"],
                               _estimate_cost_rmb(orch.spent, orch.output_tokens))
        orchestrator.event_bus.unsubscribe(q)


class ConvergenceFallbackTest(unittest.IsolatedAsyncioTestCase):
    """P0-3 收敛检测：嵌入不可用时降级为字符级文本相似度。"""

    async def _configure(self):
        orchestrator.configure([HOST_CFG, PRO_CFG, CON_CFG],
                               topic="收敛测试", max_rounds=8)
        q = orchestrator.event_bus.subscribe()
        await self._drain(q)
        return q

    @staticmethod
    async def _drain(q):
        while True:
            try:
                await asyncio.wait_for(q.get(), timeout=0.1)
            except asyncio.TimeoutError:
                break

    def test_text_similarity_pure(self):
        self.assertEqual(_text_similarity("完全相同的句子", "完全相同的句子"), 1.0)
        # 空白差异不影响相似度
        self.assertAlmostEqual(_text_similarity("  甲  乙  ", "甲乙"), 1.0)
        self.assertLess(_text_similarity("支持机器人的权利", "反对机器人的权利"), 0.85)
        self.assertEqual(_text_similarity("", "非空"), 0.0)
        self.assertEqual(_text_similarity("", ""), 0.0)

    async def test_fallback_mode_switches_to_text_similarity(self):
        await self._configure()
        orch = orchestrator
        orch.round = 2
        orch.round_arguments[1] = {"正方": "应该赋予机器人权利"}
        orch.round_arguments[2] = {"反方": "应该赋予机器人权利"}
        orch.round_evidence = {}
        with patch("app.embeddings.is_fallback", return_value=True):
            triggered = await orch._check_convergence()
        self.assertEqual(orch.convergence_mode, "text-similarity")
        self.assertEqual(orch.convergence_streak, 1)
        self.assertFalse(triggered)  # 需连续 CONVERGENCE_ROUNDS 轮才触发

    async def test_convergence_triggers_after_consecutive_rounds(self):
        await self._configure()
        orch = orchestrator
        orch.convergence_mode = "text-similarity"
        orch.convergence_streak = 1
        orch.round = 3
        orch.round_arguments[2] = {"正方": "应该赋予机器人权利"}
        orch.round_arguments[3] = {"反方": "应该赋予机器人权利"}
        orch.round_evidence = {}
        with patch("app.embeddings.is_fallback", return_value=True):
            triggered = await orch._check_convergence()
        self.assertTrue(triggered)
        self.assertEqual(orch.convergence_streak, settings.CONVERGENCE_ROUNDS)

    async def test_new_evidence_resets_streak(self):
        await self._configure()
        orch = orchestrator
        orch.convergence_mode = "text-similarity"
        orch.convergence_streak = 1
        orch.round = 2
        orch.round_arguments[1] = {"正方": "应该赋予机器人权利"}
        orch.round_arguments[2] = {"反方": "应该赋予机器人权利"}
        # 本轮 RAG 返回了新证据 → 不视为无效循环
        orch.round_evidence = {2: {"default": ["新证据"]}}
        with patch("app.embeddings.is_fallback", return_value=True):
            triggered = await orch._check_convergence()
        self.assertFalse(triggered)
        self.assertEqual(orch.convergence_streak, 0)


class ToolCallEventTest(unittest.IsolatedAsyncioTestCase):
    """P1-4 工具调用可视化：start 去重 / 回退补发 / 纯工具回合气泡。"""

    async def _configure(self):
        orchestrator.configure([HOST_CFG, PRO_CFG], topic="工具可视化", max_rounds=3)
        q = orchestrator.event_bus.subscribe()
        await self._drain(q)
        return q

    @staticmethod
    async def _drain(q):
        while True:
            try:
                await asyncio.wait_for(q.get(), timeout=0.1)
            except asyncio.TimeoutError:
                break

    @staticmethod
    async def _collect(q, stop_types):
        """收集事件直到出现 stop_types 中的类型；返回 (events, matched)。"""
        events = []
        matched = None
        while True:
            try:
                ev = await asyncio.wait_for(q.get(), timeout=0.5)
            except asyncio.TimeoutError:
                break
            events.append(ev)
            if ev.get("type") in stop_types:
                matched = ev
                break
        return events, matched

    @staticmethod
    def _delegate_result(content="", tool_starts=None):
        return {"content": content, "tool_calls": [
            {"id": "call_1", "name": "delegate_task",
             "arguments": {"recipient": "正方", "task": "调研"}}],
            "assistant_message": None, "usage": {"total_tokens": 10},
            "tool_starts": tool_starts or []}

    async def test_streamed_tool_start_not_duplicated(self):
        """流式中已发 tool_call_start（tool_starts 含索引）→ 不重复补发；
        纯工具回合（无文本）同样产出 speech_start 与 speech_end。"""
        q = await self._configure()
        orch = orchestrator
        orch.round = 1

        async def fake_consume(messages, model_config, tools=None, on_delta=None,
                               on_tool_start=None, **kwargs):
            # 模拟流式过程已通过 on_tool_start 发出 start 事件
            if on_tool_start:
                await on_tool_start("delegate_task", 0)
            return self._delegate_result(tool_starts=[0])

        async def _unused_retry(*a, **k):
            return _fake_llm_result()

        orch._consume_stream = fake_consume
        orch._run_llm_retry = _unused_retry
        out = await orch._agent_turn(orch.main, "请发言", mode="discussion", stream=True)
        events, _ = await self._collect(q, {"speech_end"})

        starts = [e for e in events if e["type"] == "tool_call_start"]
        ends = [e for e in events if e["type"] == "tool_call_end"]
        self.assertEqual(len(starts), 1)  # 仅流式一次，未重复
        self.assertEqual(starts[0]["name"], "delegate_task")
        self.assertEqual(starts[0]["index"], 0)
        self.assertEqual(len(ends), 1)
        self.assertEqual(ends[0]["name"], "delegate_task")
        self.assertIn("分派任务", ends[0]["summary"])
        # 纯工具回合气泡存在：speech_start / speech_end 均已发布
        self.assertIn("speech_start", [e["type"] for e in events])
        self.assertIn("speech_end", [e["type"] for e in events])
        self.assertEqual(out["content"], "")
        orchestrator.event_bus.unsubscribe(q)

    async def test_stream_fallback_emits_missing_tool_start(self):
        """流式失败回退非流式（结果无 tool_starts）→ 补发 tool_call_start。"""
        q = await self._configure()
        orch = orchestrator
        orch.round = 1

        async def boom(*args, **kwargs):
            raise RuntimeError("流式失败")

        async def fake_retry(*args, **kwargs):
            return self._delegate_result()

        orch._consume_stream = boom
        orch._run_llm_retry = fake_retry
        await orch._agent_turn(orch.main, "请发言", mode="discussion", stream=True)
        events, _ = await self._collect(q, {"speech_end"})

        starts = [e for e in events if e["type"] == "tool_call_start"]
        ends = [e for e in events if e["type"] == "tool_call_end"]
        self.assertEqual(len(starts), 1)
        self.assertEqual(starts[0]["name"], "delegate_task")
        self.assertEqual(len(ends), 1)
        orchestrator.event_bus.unsubscribe(q)


class StructuredConclusionTest(unittest.IsolatedAsyncioTestCase):
    """P0-1 结构化结论：四段式最终报告 + conclusion 事件。"""

    async def _configure(self):
        orchestrator.configure([HOST_CFG, PRO_CFG, CON_CFG],
                               topic="结论测试", max_rounds=2)
        q = orchestrator.event_bus.subscribe()
        await self._drain(q)
        return q

    @staticmethod
    async def _drain(q):
        while True:
            try:
                await asyncio.wait_for(q.get(), timeout=0.1)
            except asyncio.TimeoutError:
                break

    @staticmethod
    def _judgment():
        return Judgment(
            winner="PRO", critique="正方论证更完整", convergence_flag=False,
            scores={"logic": 8.0, "evidence": 7.5, "rebuttal": 6.0, "clarity": 9.0})

    def test_normalize_conclusion_four_sections_with_fallback(self):
        """空文本 → 四段式结构完整，裁判裁决与核心结论由确定性数据回填。"""
        report = orchestrator._normalize_conclusion("", self._judgment())
        self.assertEqual(len(re.findall(r"^## ", report, re.M)), 4)
        for title in ("核心结论", "各方立场与论点摘要", "裁判裁决", "分歧与待办"):
            self.assertIn(f"## {title}", report)
        # 结构化数据已写入 self.conclusion
        c = orchestrator.conclusion
        self.assertIsNotNone(c)
        self.assertIn("正方论证更完整", c["core"])
        self.assertIn("胜方：PRO", c["judge_ruling"])
        self.assertEqual(c["pending"], "（无明确列出的分歧或待办事项）")

    def test_normalize_conclusion_keeps_model_text_overwrites_ruling(self):
        """模型产出四段 → 保留正文；裁判裁决段一律用权威数据重写。"""
        text = ("## 核心结论\n\n模型总结\n\n"
                "## 各方立场与论点摘要\n\n模型立场\n\n"
                "## 裁判裁决\n\n（模型转述——应被权威数据覆盖）\n\n"
                "## 分歧与待办\n\n待解决：成本")
        report = orchestrator._normalize_conclusion(text, self._judgment())
        self.assertIn("模型总结", report)
        self.assertIn("模型立场", report)
        self.assertIn("待解决：成本", report)
        # 裁判裁决段被权威数据重写，模型转述不残留
        self.assertNotIn("模型转述", report)
        self.assertIn("胜方：PRO", report)
        self.assertIn("正方论证更完整", report)
        c = orchestrator.conclusion
        self.assertEqual(c["core"], "模型总结")
        self.assertIn("胜方：PRO", c["judge_ruling"])

    def test_normalize_conclusion_missing_sections_filled(self):
        """模型遗漏段落 → 用确定性数据回填，保证结构完整。"""
        text = "## 核心结论\n\n只有结论"
        report = orchestrator._normalize_conclusion(text, self._judgment())
        self.assertIn("只有结论", report)
        self.assertIn("## 各方立场与论点摘要", report)
        self.assertIn("## 裁判裁决", report)
        self.assertIn("## 分歧与待办", report)

    async def test_final_summary_publishes_conclusion_event(self):
        q = await self._configure()
        orch = orchestrator

        async def fake_agent_turn(agent, user_msg, mode, allow_delegate=True, stream=False):
            return {"content": "## 核心结论\n\n事件结论\n\n"
                               "## 各方立场与论点摘要\n\n事件立场\n\n"
                               "## 裁判裁决\n\nx\n\n"
                               "## 分歧与待办\n\n无",
                    "delegate": [], "report": None,
                    "streamed": False, "token_cost": 0}

        orig_agent_turn = orch._agent_turn
        orch._agent_turn = fake_agent_turn
        try:
            report = await orch._final_summary(self._judgment())
            self.assertIn("## 核心结论", report)

            while True:
                ev = await asyncio.wait_for(q.get(), timeout=2)
                if ev["type"] == "conclusion":
                    break
            for key in ("core", "positions", "judge_ruling", "pending", "winner", "round"):
                self.assertIn(key, ev)
            self.assertEqual(ev["winner"], "PRO")
            self.assertEqual(ev["core"], "事件结论")
            self.assertIn("胜方：PRO", ev["judge_ruling"])
        finally:
            orch._agent_turn = orig_agent_turn
        orchestrator.event_bus.unsubscribe(q)


class ArgumentSnippetTest(unittest.TestCase):
    """§5.4 对手关键论据句抽取纯函数：序号/论据标记句优先，截断到 max_chars。"""

    def test_picks_marker_sentences(self):
        text = ("首先，历史数据表明ATM机未减少银行职员。"
                "其次，AI在创造性工作上仍有局限。"
                "天气不错。")
        out = _extract_argument_sentences(text, 200)
        self.assertIn("首先", out)
        self.assertIn("其次", out)
        # 无标记句不入选（短句“天气不错”分数最低）
        self.assertNotIn("天气不错", out)

    def test_max_chars_truncated(self):
        text = "第一，" + "长" * 500 + "。第二，另有论点。"
        out = _extract_argument_sentences(text, 50)
        self.assertLessEqual(len(out), 50)

    def test_empty_input(self):
        self.assertEqual(_extract_argument_sentences("", 100), "")
        self.assertEqual(_extract_argument_sentences("   \n", 100), "")
        self.assertEqual(_extract_argument_sentences("有效文本", 0), "")

    def test_fallback_longest_when_no_markers(self):
        text = "短句。这是一句没有任何标记但相对较长的论据性内容。"
        out = _extract_argument_sentences(text, 200)
        # 无序号标记时仍返回非空内容，不会整体丢失
        self.assertTrue(out)


class TaskDebateSeparationTest(unittest.IsolatedAsyncioTestCase):
    """§5.8 任务回合与辩论回合分离：
    - _task_turn 处理任务但不污染 round_arguments；
    - _debate_turn 才记录辩论发言；
    - _prepare_opponent_context 第 3 轮起附带关键论据句。
    """

    async def _configure(self):
        orchestrator.configure([HOST_CFG, PRO_CFG, CON_CFG],
                               topic="任务辩论分离", max_rounds=3)
        q = orchestrator.event_bus.subscribe()
        while True:
            try:
                await asyncio.wait_for(q.get(), timeout=0.1)
            except asyncio.TimeoutError:
                break
        return q

    @staticmethod
    def _collab(stance):
        for c in orchestrator.collaborators:
            if c.stance == stance:
                return c
        raise AssertionError(f"无 {stance} 立场协作辩手")

    async def test_task_turn_does_not_pollute_round_arguments(self):
        await self._configure()
        pro = self._collab("PRO")
        orch = orchestrator
        orch.round = 2
        orch.tasks.clear()
        orch.round_arguments.clear()
        orch._create_task({
            "recipient": pro.name, "content": "调研就业数据",
            "expected_outcome": "给出关键数字", "var_name": "research"})
        orig = orch._agent_turn

        async def fake_agent_turn(agent, user_msg, mode, allow_delegate=True, stream=False):
            self.assertEqual(mode, "task")  # 任务回合必须走 task 模式
            return {"content": "过程输出", "delegate": [],
                    "report": {"var_name": "research", "var_value": "8.0%",
                               "completed": True, "message": "已查得数据"},
                    "streamed": True, "token_cost": 0}

        orch._agent_turn = fake_agent_turn
        try:
            await orch._task_turn(pro)
        finally:
            orch._agent_turn = orig
        # 结果回填 shared_context 且任务标记完成
        self.assertEqual(orch.shared_context["research"]["value"], "8.0%")
        self.assertTrue(orch.tasks[0]["completed"])
        # 关键：round_arguments 无任务痕迹（§5.8）
        self.assertEqual(orch.round_arguments, {})

    async def test_debate_turn_records_speech(self):
        await self._configure()
        pro = self._collab("PRO")
        orch = orchestrator
        orch.round = 2
        orch.tasks.clear()
        orch.round_arguments.clear()
        orig = orch._agent_turn

        async def fake_agent_turn(agent, user_msg, mode, allow_delegate=True, stream=False):
            self.assertEqual(mode, "discussion")
            return {"content": "我的反驳发言", "delegate": [],
                    "report": None, "streamed": True, "token_cost": 0}

        orch._agent_turn = fake_agent_turn
        try:
            await orch._debate_turn(pro)
        finally:
            orch._agent_turn = orig
        self.assertEqual(orch.round_arguments[2][pro.name], "我的反驳发言")
        self.assertEqual(orch.last_debate_speaker, pro.name)

    async def test_round3_opponent_prep_includes_argument_snippets(self):
        q = await self._configure()
        import app.orchestrator as orch_mod
        pro = self._collab("PRO")
        con = self._collab("CON")
        orch = orchestrator
        orch.round = 3
        orch.round_arguments[2] = {con.name: (
            "第一，历史数据表明技术变革未减少总就业。"
            "第二，人类在伦理决策上有不可替代优势。"
            "其余填充内容。")}

        async def fake_summarize_text(model_config, text, max_chars=150):
            return "浓缩要点", {"total_tokens": 5}

        orig = orch_mod.summarize_text
        orch_mod.summarize_text = fake_summarize_text
        try:
            await orch._prepare_opponent_context(pro)
        finally:
            orch_mod.summarize_text = orig
        prep = orch._opponent_prep[pro.name]
        self.assertEqual(prep["text"], "浓缩要点")
        # §5.4：摘要轮额外保留关键论据句
        self.assertIn("第一", prep.get("arguments", ""))
        self.assertIn("第二", prep.get("arguments", ""))
        orchestrator.event_bus.unsubscribe(q)


class CollaborationModeTest(unittest.IsolatedAsyncioTestCase):
    """双模式之合作模式（collaboration）：
    - configure 模式透传、席位立场弱化（NEUTRAL）、无裁判；
    - 任务池（recipient 留空 → pending）+ 主动认领（claim_task 独占）；
    - 认领 + 同轮回报流程、任务状态机迁移；
    - 所有任务 completed 即收尾（跳过裁判裁决）；
    - _agent_turn 工具注入：合作子 Agent 见 claim_task/report_result，
      主 Agent 见 delegate_task。
    """
    # 不带显式立场：验证合作模式自动弱化为 NEUTRAL
    NO_STANCE_CFG = [
        {"name": "统筹", "role": "统筹", "personality": "", "provider": "default"},
        {"name": "助手A", "role": "执行", "personality": "", "provider": "default"},
        {"name": "助手B", "role": "执行", "personality": "", "provider": "default"},
    ]

    async def _configure(self, cfgs=None, max_rounds=3, topic="合作模式测试"):
        orchestrator.configure(cfgs or self.NO_STANCE_CFG,
                               topic=topic, max_rounds=max_rounds,
                               mode="collaboration")
        q = orchestrator.event_bus.subscribe()
        while True:
            try:
                await asyncio.wait_for(q.get(), timeout=0.1)
            except asyncio.TimeoutError:
                break
        return q

    @staticmethod
    def _collab(name):
        for c in orchestrator.collaborators:
            if c.name == name:
                return c
        raise AssertionError(f"无 {name} 协作 Agent")

    async def test_configure_sets_collaboration_mode(self):
        await self._configure()
        orch = orchestrator
        self.assertEqual(orch.mode, "collaboration")
        # 主位 HOST，其余席位一律 NEUTRAL（合作模式弱化立场）
        self.assertEqual(orch.main.stance, "HOST")
        for c in orch.collaborators:
            self.assertEqual(c.stance, "NEUTRAL")
        # 合作模式无辩论对抗，不构建独立裁判
        self.assertIsNone(orch.judge)
        # 状态同步透传模式
        self.assertEqual(orch._status_sync()["mode"], "collaboration")

    async def test_create_task_pending_without_recipient(self):
        await self._configure([HOST_CFG, PRO_CFG, CON_CFG])
        orch = orchestrator
        orch.tasks.clear()
        orch.shared_context.clear()
        orch.task_counter = 0
        # recipient 留空 → 放入任务池，状态 pending
        t = orch._create_task({
            "content": "调研就业数据", "expected_outcome": "给出关键数字",
            "var_name": "research"})
        self.assertEqual(t["status"], "pending")
        self.assertEqual(t["recipient"], "")
        self.assertFalse(t["completed"])
        self.assertIn("research", orch.shared_context)
        # 指定 recipient → 直接分派，状态 claimed
        t2 = orch._create_task({
            "recipient": PRO_CFG["name"], "content": "撰写报告",
            "expected_outcome": "输出段落", "var_name": "report"})
        self.assertEqual(t2["status"], "claimed")
        self.assertEqual(t2["recipient"], PRO_CFG["name"])

    async def test_claim_is_exclusive(self):
        await self._configure([HOST_CFG, PRO_CFG, CON_CFG])
        orch = orchestrator
        orch.tasks.clear()
        orch.task_counter = 0
        t = orch._create_task({
            "content": "独占任务", "expected_outcome": "结果",
            "var_name": "v1"})
        pro = self._collab(PRO_CFG["name"])
        con = self._collab(CON_CFG["name"])
        orch._apply_claim(pro, {"task_id": t["id"]})
        self.assertEqual(t["status"], "claimed")
        self.assertEqual(t["recipient"], pro.name)
        self.assertEqual(t["rounds_waiting"], 0)
        # 重复认领（他人尝试）被忽略：认领者独占
        orch._apply_claim(con, {"task_id": t["id"]})
        self.assertEqual(t["status"], "claimed")
        self.assertEqual(t["recipient"], pro.name)
        # 认领不存在的任务被忽略（不抛异常）
        orch._apply_claim(con, {"task_id": 9999})

    async def test_claim_and_report_in_same_turn(self):
        await self._configure([HOST_CFG, PRO_CFG, CON_CFG])
        orch = orchestrator
        orch.tasks.clear()
        orch.shared_context.clear()
        orch.task_counter = 0
        t = orch._create_task({
            "content": "调研", "expected_outcome": "数据", "var_name": "research"})
        pro = self._collab(PRO_CFG["name"])
        orch.round = 1
        orig = orch._agent_turn

        async def fake_agent_turn(agent, user_msg, mode, allow_delegate=True, stream=False):
            self.assertEqual(mode, "collab")  # 合作子 Agent 回合走 collab 模式
            return {"content": "我来认领并完成", "delegate": [],
                    "claim": [{"task_id": t["id"]}],
                    "report": {"var_name": "research", "var_value": "8.0%",
                               "completed": True, "message": "已查得数据"},
                    "streamed": True, "token_cost": 0}

        orch._agent_turn = fake_agent_turn
        try:
            await orch._collab_work_turn(pro)
        finally:
            orch._agent_turn = orig
        # 同轮认领 + 回报：任务 completed、认领者落库、共享上下文回填
        self.assertTrue(t["completed"])
        self.assertEqual(t["status"], "completed")
        self.assertEqual(t["recipient"], pro.name)
        self.assertEqual(orch.shared_context["research"]["value"], "8.0%")
        self.assertEqual(orch.shared_context["research"]["by"], orch.main.name)

    async def test_all_tasks_complete_ends_loop_skip_judge(self):
        await self._configure([HOST_CFG, PRO_CFG], max_rounds=5, topic="全完成收尾")
        orch = orchestrator
        orch.tasks.clear()
        orch.task_counter = 0
        orig = orch._agent_turn

        async def fake_agent_turn(agent, user_msg, mode, allow_delegate=True, stream=False):
            if agent.is_main and allow_delegate:
                # 主 Agent 第 1 轮拆解整体任务入池
                return {"content": "我来拆解任务", "delegate": [
                    {"content": "调研A", "expected_outcome": "结果A", "var_name": "a"}],
                    "report": None, "claim": [], "streamed": True, "token_cost": 0}
            if agent.is_main and not allow_delegate:
                # 最终汇总轮（allow_delegate=False）
                return {"content":
                        "## 核心结论\n\n整体任务完成\n\n"
                        "## 子任务与成果\n\n- 任务#1：调研A ✅\n\n"
                        "## 遗留问题与后续建议\n\n无\n",
                        "delegate": [], "report": None, "claim": [],
                        "streamed": True, "token_cost": 0}
            # 子 Agent：认领并同轮回报
            return {"content": "完成调研A", "delegate": [],
                    "claim": [{"task_id": orch.tasks[0]["id"]}] if orch.tasks else [],
                    "report": {"var_name": "a", "var_value": "结果A",
                               "completed": True, "message": "完成"},
                    "streamed": True, "token_cost": 0}

        orch._agent_turn = fake_agent_turn
        try:
            await orch.run_discussion()
        finally:
            orch._agent_turn = orig
        # 第 1 轮任务全完成即提前收尾：round 不再推进
        self.assertEqual(orch.round, 1)
        self.assertEqual(orch.status, "done")
        self.assertEqual(orch.phase, "finished")
        self.assertTrue(orch.tasks and all(t["completed"] for t in orch.tasks))
        # 合作模式无裁判裁决
        self.assertIsNone(orch.judgment)
        # 三段式结论已产出（无裁判裁决：judge_ruling 空串）
        self.assertIsNotNone(orch.conclusion)
        self.assertEqual(orch.conclusion["judge_ruling"], "")
        self.assertIn("整体任务完成", orch.conclusion["core"])

    async def test_tool_injection_per_mode(self):
        await self._configure([HOST_CFG, PRO_CFG, CON_CFG])
        orch = orchestrator
        seen_tools = []

        async def fake_consume(messages, model_config, tools=None, on_delta=None,
                               on_tool_start=None, **kwargs):
            seen_tools[:] = [t.get("function", {}).get("name")
                              for t in (tools or []) if t.get("function", {}).get("name")]
            return {"content": "无工具调用", "tool_calls": [], "assistant_message": None,
                    "usage": {"total_tokens": 1}, "tool_starts": []}

        orig = orch._consume_stream
        orch._consume_stream = fake_consume
        try:
            # 合作子 Agent（mode=collab）：认领 + 回报工具
            pro = self._collab(PRO_CFG["name"])
            await orch._agent_turn(pro, "请处理任务", mode="collab", stream=True)
            self.assertIn("claim_task", seen_tools)
            self.assertIn("report_result", seen_tools)
            self.assertNotIn("delegate_task", seen_tools)
            # 主 Agent（mode=collaboration，允许分派）：delegate_task
            await orch._agent_turn(orch.main, "请拆解任务", mode="collaboration", stream=True)
            self.assertIn("delegate_task", seen_tools)
            self.assertNotIn("claim_task", seen_tools)
            self.assertNotIn("report_result", seen_tools)
        finally:
            orch._consume_stream = orig


if __name__ == "__main__":
    unittest.main()
