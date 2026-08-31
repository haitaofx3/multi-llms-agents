"""
test_ci_fast.py — 快速确定性单元测试（CI 用，无需真实 LLM API）

覆盖本次控制流修复的核心行为：
  1. pause() / resume() / end() 会向事件总线广播 status 事件（前端据此切换按钮）；
  2. 运行中手动 end() 后：状态回到 stopped、phase=idle，并跳过裁判裁决与主持人汇总；
  3. 正常路径完整跑完一轮：仍会产出 judgment / summary / status=done 事件，流程未被破坏。
  4. 多层公平性治理（FairnessGovernor，替代旧的 token 预算规划器）：
     L1 输出上限（相对 × 比例 + 硬上限，事后字符截断）/ L2 有效回答比例 /
     L3 输入对称性 / L4 轮次对称性 / L5 采样参数对齐 / L6 长度中性评审；
     不再做 total_token 约束，也不再向 API 下发 max_tokens。

运行方式（无需服务器、无需真实 LLM）：
    python -m unittest tests.test_ci_fast -v
"""
import asyncio
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from app.config import settings
from app.fairness import FairnessGovernor
from app.memory import MemorySystem
from app.orchestrator import orchestrator
from app.protocol import Judgment

HOST_CFG = {"name": "主持", "role": "主持", "personality": "",
            "provider": "default", "stance": "HOST"}
PRO_CFG = {"name": "正方", "role": "正方", "personality": "",
           "provider": "default", "stance": "PRO"}
CON_CFG = {"name": "反方", "role": "反方", "personality": "",
           "provider": "default", "stance": "CON"}
NEUTRAL_CFG = {"name": "中立", "role": "中立", "personality": "",
               "provider": "default", "stance": "NEUTRAL"}


def _fake_llm_result(content="发言"):
    return {"content": content, "tool_calls": [], "assistant_message": None,
            "usage": {"total_tokens": 1}}


def _restore_llm_fakes(orch):
    """删除在 orchestrator 实例上注入的 LLM 假方法，回落到真实类方法。

    这些测试会 monkeypatch 全局 orchestrator 的 LLM 调用点；若测试结束后
    不还原，会污染后续执行的测试模块（例如 test_features_v2 看到的是残留的
    fake _final_summary，而不是真实实现）。
    """
    for name in ("_judge_round", "_final_summary", "_consume_stream", "_run_llm_retry"):
        if name in orch.__dict__:
            delattr(orch, name)


class OrchestratorControlTest(unittest.IsolatedAsyncioTestCase):
    """编排器控制流（暂停/继续/结束 + 手动结束收尾）测试。"""

    async def _configure(self, max_rounds=3):
        """在事件循环内执行 configure（_log 需要运行中的 loop）。"""
        orchestrator.configure([HOST_CFG, PRO_CFG], topic="测试主题",
                               max_rounds=max_rounds)
        q = orchestrator.event_bus.subscribe()
        await self._drain(q)
        return q

    @staticmethod
    async def _drain(q):
        """排空 configure 阶段异步发布的事件（log/status），避免干扰断言。"""
        while True:
            try:
                await asyncio.wait_for(q.get(), timeout=0.1)
            except asyncio.TimeoutError:
                break

    async def test_pause_resume_end_broadcast_status_events(self):
        q = await self._configure()
        orchestrator.status = "running"
        orchestrator._pause_event.set()

        orchestrator.pause()
        ev = await asyncio.wait_for(q.get(), timeout=2)
        self.assertEqual(ev["type"], "status")
        self.assertEqual(ev["status"], "paused")

        orchestrator.resume()
        ev = await asyncio.wait_for(q.get(), timeout=2)
        self.assertEqual(ev["status"], "running")

        orchestrator.end()
        ev = await asyncio.wait_for(q.get(), timeout=2)
        self.assertEqual(ev["status"], "stopped")
        self.assertEqual(orchestrator.status, "stopped")

        orchestrator.event_bus.unsubscribe(q)

    async def test_manual_end_skips_judge_and_summary(self):
        q = await self._configure(max_rounds=3)
        called = {"judge": 0, "summary": 0}

        async def fake_judge():
            called["judge"] += 1
            return Judgment(winner="DRAW", critique="x", convergence_flag=False)

        async def fake_summary(judgment=None):
            called["summary"] += 1
            return "summary"

        async def fake_consume(messages, model_config, tools=None, on_delta=None, **kwargs):
            await asyncio.sleep(0.2)  # 拉长单回合，给 end() 留插入窗口
            if on_delta:
                await on_delta("发言")
            return _fake_llm_result()

        orchestrator._judge_round = fake_judge
        orchestrator._final_summary = fake_summary
        orchestrator._consume_stream = fake_consume
        orchestrator._run_llm_retry = lambda *a, **k: _fake_llm_result()
        self.addCleanup(_restore_llm_fakes, orchestrator)

        orchestrator.status = "idle"
        task = asyncio.create_task(orchestrator.run_discussion())
        await asyncio.sleep(0.3)  # 等 run 进入第一轮，然后手动结束
        orchestrator.end()
        await asyncio.wait_for(task, timeout=10)

        self.assertEqual(orchestrator.status, "stopped")
        self.assertEqual(orchestrator.phase, "idle")
        self.assertEqual(called["judge"], 0)
        self.assertEqual(called["summary"], 0)

        orchestrator.event_bus.unsubscribe(q)

    async def test_normal_run_still_emits_judgment_summary_done(self):
        q = await self._configure(max_rounds=1)

        async def fake_judge():
            return Judgment(winner="PRO", critique="测试裁决", convergence_flag=False)

        async def fake_summary(judgment=None):
            return "测试总结"

        async def fake_consume(messages, model_config, tools=None, on_delta=None, **kwargs):
            if on_delta:
                await on_delta("发言")
            return _fake_llm_result()

        orchestrator._judge_round = fake_judge
        orchestrator._final_summary = fake_summary
        orchestrator._consume_stream = fake_consume
        orchestrator._run_llm_retry = lambda *a, **k: _fake_llm_result()
        self.addCleanup(_restore_llm_fakes, orchestrator)

        orchestrator.status = "idle"
        task = asyncio.create_task(orchestrator.run_discussion())
        await asyncio.wait_for(task, timeout=30)

        seen = []
        while True:
            try:
                ev = await asyncio.wait_for(q.get(), timeout=0.5)
            except asyncio.TimeoutError:
                break
            if ev.get("type") in ("status", "judgment", "summary"):
                seen.append((ev["type"], ev.get("status"), ev.get("phase")))

        self.assertEqual(orchestrator.status, "done")
        self.assertEqual(orchestrator.phase, "finished")
        self.assertIsNotNone(orchestrator.judgment)
        self.assertEqual(orchestrator.judgment.winner, "PRO")
        self.assertIn("judgment", [t for t, *_ in seen])
        self.assertIn("summary", [t for t, *_ in seen])
        self.assertIn(("status", "done", "finished"),
                      [(t, s, p) for t, s, p in seen if t == "status"])

        orchestrator.event_bus.unsubscribe(q)


class FairnessGovernorTest(unittest.IsolatedAsyncioTestCase):
    """多层公平性治理（FairnessGovernor，替代旧的 token 预算规划器）。

    六层约束：
      L1 输出上限（相对上限 = 对手近 N 轮有效输出均值 × 比例，再与硬上限取 min；
        超限事后截断保留头尾，不向 API 下发 max_tokens）
      L2 有效回答比例（剥离 <think> 思考片段后的有效回答占比）
      L3 输入对称性（PRO/CON 反驳时可见的对手历史轮数差值 ≤ 1）
      L4 轮次对称性（每模型每轮至多一次发言）
      L5 采样参数对齐（辩论/辅助 LLM 统一 temperature / top_p）
      L6 长度中性评审（裁判提示词注入「不以长度论优劣」）
    """

    async def _configure(self, max_rounds=3):
        orchestrator.configure([HOST_CFG, PRO_CFG], topic="公平性治理测试",
                               max_rounds=max_rounds)
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
    def _layer(rep: dict, layer_id: str) -> dict:
        for l in rep["layers"]:
            if l["layer"] == layer_id:
                return l
        raise AssertionError(f"报告缺少 {layer_id} 层")

    # ---------------- L1 输出上限 ----------------

    async def test_l1_opening_ceiling(self):
        """立论（第 1 轮）或尚无对手参照时，上限取 opening_char_limit。"""
        g = FairnessGovernor()
        self.assertEqual(g.ceiling_for("PRO", 1, None), settings.FAIR_OPENING_CHAR_LIMIT)
        self.assertEqual(g.ceiling_for("CON", 2, None), settings.FAIR_OPENING_CHAR_LIMIT)

    async def test_l1_relative_ceiling_and_hard_cap(self):
        """有对手参照：上限 = min(max(均值×比例, 下限), 硬上限)；截断保留头尾。"""
        g = FairnessGovernor()
        # 对手有效输出均值很小 → 相对基准下限兜底
        g.record_turn("CON", 1, visible_chars=10, think_chars=0)
        c = g.ceiling_for("PRO", 2, g.recent_opponent_chars("PRO"))
        self.assertEqual(c, max(10 * g.output_ratio, g.floor_char_limit))
        self.assertLessEqual(c, g.hard_char_ceiling)
        # 对手输出很大 → 相对上限被硬上限钳制，杜绝极端长输出
        g.record_turn("CON", 2, visible_chars=100_000, think_chars=0)
        c2 = g.ceiling_for("PRO", 3, g.recent_opponent_chars("PRO"))
        self.assertEqual(c2, g.hard_char_ceiling)
        # 截断保留头尾并标记 truncated
        text = "甲" * 100 + "…" + "乙" * 100
        t, flag = g.truncate_output(text, 50)
        self.assertTrue(flag)
        self.assertLessEqual(len(t), 50)
        self.assertTrue(t.startswith("甲"))
        self.assertTrue(t.endswith("乙"))
        # 未超限不截断
        t2, flag2 = g.truncate_output("短", 100)
        self.assertFalse(flag2)
        self.assertEqual(t2, "短")

    # ---------------- L2 有效回答比例 ----------------

    async def test_l2_think_ratio_report(self):
        """思考占比超过 FAIR_THINK_RATIO_MAX → L2 报 warning；正常 → ok。"""
        g = FairnessGovernor()
        self.assertEqual(self._layer(g.report(), "L2")["status"], "ok")
        g.record_turn("PRO", 1, visible_chars=100, think_chars=400)  # 占比 80%
        g.record_turn("CON", 1, visible_chars=100, think_chars=100)
        rep = g.report()
        self.assertEqual(self._layer(rep, "L2")["status"], "warning")
        # 有效回答占比只统计剥离思考后的正文
        from app.fairness import effective_answer_chars, think_chars
        raw = "<think>很长的推理</think>这是有效论证"
        self.assertEqual(think_chars(raw), len("<think>很长的推理</think>"))
        self.assertEqual(effective_answer_chars(raw), len("这是有效论证"))

    # ---------------- L3 输入对称性 ----------------

    async def test_l3_opponent_seen_symmetry(self):
        """双方可见对手轮数差 ≤ max_seen_diff → ok；超差 → violation。"""
        g = FairnessGovernor()
        self.assertEqual(self._layer(g.report(), "L3")["status"], "ok")  # 尚无反驳记录
        g.record_opponent_seen("PRO", 2)
        g.record_opponent_seen("CON", 2)
        self.assertEqual(self._layer(g.report(), "L3")["status"], "ok")
        # 落后方多 1 轮（差 1）仍允许，下一回合对手将补齐
        g.record_opponent_seen("CON", 3)
        self.assertEqual(self._layer(g.report(), "L3")["status"], "ok")
        # 差 2 超限 → violation
        g.record_opponent_seen("PRO", 1)
        rep = g.report()
        self.assertEqual(self._layer(rep, "L3")["status"], "violation")
        self.assertFalse(rep["ok"])

    # ---------------- L4 轮次对称性 ----------------

    async def test_l4_speech_duplicate_detection(self):
        """同一 (立场, 轮次) 重复发言 → L4 violation；正常交替 → ok。"""
        g = FairnessGovernor()
        g.record_speech("PRO", 1)
        g.record_speech("CON", 1)
        self.assertEqual(self._layer(g.report(), "L4")["status"], "ok")
        g.record_speech("PRO", 1)  # 同轮二次发言
        rep = g.report()
        self.assertEqual(self._layer(rep, "L4")["status"], "violation")
        self.assertFalse(rep["ok"])
        # 新轮次再次发言正常
        g.record_speech("PRO", 2)
        self.assertEqual(self._layer(g.report(), "L4")["status"], "violation")  # 旧违规仍保留

    # ---------------- L5 采样参数对齐 ----------------

    async def test_l5_sampling_and_judge_params(self):
        """辩论/辅助 LLM 采样参数与 FAIR_* 配置保持一致。"""
        g = FairnessGovernor()
        self.assertEqual(
            g.sampling_params(),
            {"temperature": settings.FAIR_TEMPERATURE, "top_p": settings.FAIR_TOP_P})
        self.assertEqual(
            g.judge_params(),
            {"temperature": settings.FAIR_JUDGE_TEMPERATURE,
             "top_p": settings.FAIR_JUDGE_TOP_P})

    # ---------------- L6 长度中性评审 ----------------

    async def test_l6_judge_guidance(self):
        """调用 judge_guidance 后 L6 标记为已注入；文本含长度中性要求。"""
        g = FairnessGovernor()
        self.assertEqual(self._layer(g.report(), "L6")["status"], "warning")  # 未注入
        text = g.judge_guidance()
        self.assertIn("输出长度", text)
        self.assertEqual(self._layer(g.report(), "L6")["status"], "ok")

    # ---------------- 编排器集成：状态同步 / 运行完整性 ----------------

    async def test_status_sync_has_fairness_and_token_telemetry(self):
        """_status_sync 携带 fairness 报告与只读 token 遥测（无预算字段）。"""
        await self._configure(max_rounds=3)
        s = orchestrator._status_sync()
        self.assertIn("fairness", s)
        self.assertIs(s["fairness"]["ok"], True)  # 无违规（L6 未注入仅 warning）
        # 只读遥测：实时输出 token / 累计用量 / 耗时
        for key in ("spent_tokens", "output_tokens", "elapsed_time_ms"):
            self.assertIn(key, s)
        self.assertIsInstance(s["output_tokens"], int)
        self.assertIsInstance(s["spent_tokens"], int)
        self.assertGreaterEqual(s["output_tokens"], 0)
        self.assertGreaterEqual(s["spent_tokens"], 0)
        # 不再存在任何预算字段（total_tokens 限制已删除）
        for key in ("budget", "remaining_budget", "spent_budget",
                    "effective_max_tokens", "feasible_rounds", "projected_total",
                    "budget_overshoot", "budget_degraded",
                    "est_factor", "call_factor", "growth_ratio"):
            self.assertNotIn(key, s)

    async def test_fair_run_completes_without_budget_events(self):
        """整场辩论完整跑完（judge + summary 均产出）；无 budget 熔断/超支事件，
        每轮发布 fairness.report 日志。"""
        q = await self._configure(max_rounds=3)
        called = {"judge": 0, "summary": 0}

        async def fake_judge():
            called["judge"] += 1
            return Judgment(winner="PRO", critique="测试裁决", convergence_flag=False)

        async def fake_summary(judgment=None):
            called["summary"] += 1
            return "测试总结"

        async def fake_consume(messages, model_config, tools=None, on_delta=None, **kwargs):
            if on_delta:
                await on_delta("发言")
            return _fake_llm_result()

        orchestrator._judge_round = fake_judge
        orchestrator._final_summary = fake_summary
        orchestrator._consume_stream = fake_consume
        orchestrator._run_llm_retry = lambda *a, **k: _fake_llm_result()
        self.addCleanup(_restore_llm_fakes, orchestrator)

        orchestrator.status = "idle"
        task = asyncio.create_task(orchestrator.run_discussion())
        await asyncio.wait_for(task, timeout=30)

        seen = []
        while True:
            try:
                ev = await asyncio.wait_for(q.get(), timeout=0.5)
            except asyncio.TimeoutError:
                break
            if ev.get("type") == "log":
                seen.append(ev.get("event"))

        self.assertEqual(orchestrator.status, "done")
        self.assertGreaterEqual(called["judge"], 1)
        self.assertGreaterEqual(called["summary"], 1)
        # 公平性治理：不再有预算熔断/超支事件
        self.assertNotIn("budget.circuit_break", seen)
        self.assertNotIn("budget.overshoot", seen)
        self.assertIn("fairness.report", seen)

        orchestrator.event_bus.unsubscribe(q)


class DebateMechanismTest(unittest.IsolatedAsyncioTestCase):
    """辩论机制改造（需求 1-6）：对手定向反驳、输出约束、按轮强压缩。"""

    @staticmethod
    async def _drain(q):
        while True:
            try:
                await asyncio.wait_for(q.get(), timeout=0.1)
            except asyncio.TimeoutError:
                break

    async def _configure4(self, max_rounds=3):
        """HOST + PRO + CON + NEUTRAL 四席位，覆盖对手映射与无对手回退。"""
        orchestrator.configure([HOST_CFG, PRO_CFG, CON_CFG, NEUTRAL_CFG],
                               topic="对手反驳测试", max_rounds=max_rounds)
        q = orchestrator.event_bus.subscribe()
        await self._drain(q)
        return q

    @staticmethod
    def _collab(stance):
        for c in orchestrator.collaborators:
            if c.stance == stance:
                return c
        raise AssertionError(f"无 {stance} 立场协作辩手")

    # ---------------- 需求 5/6：history_rounds 平行维护 ----------------

    async def test_append_history_tracks_rounds_and_trims_sync(self):
        """append_history 记录轮次；条数/字符裁剪与 history_rounds 保持平行。"""
        await self._configure4()
        a = self._collab("PRO")
        a.reset_history()
        a.append_history("user", "u1")
        a.append_history("assistant", "a1")
        self.assertEqual(a.history_rounds, [1, 1])
        a.append_history("user", "u2", round_no=2)
        a.append_history("assistant", "a2", round_no=2)
        self.assertEqual(a.history_rounds, [1, 1, 2, 2])
        # 条数裁剪同步
        a.max_history = 3
        a.append_history("user", "u3", round_no=3)
        self.assertEqual(a.history_rounds, [2, 2, 3])
        self.assertEqual(len(a.history), len(a.history_rounds))
        # 缺省 round 取当前最大轮次
        a.append_history("assistant", "a3")
        self.assertEqual(a.history_rounds[-1], 3)
        # 字符硬上限裁剪同步（长内容）
        a.max_history = 100
        a.reset_history()
        for r in range(1, 40):
            a.append_history("user", "长" * 400, round_no=r)
        self.assertEqual(len(a.history), len(a.history_rounds))
        self.assertLessEqual(a._history_chars(), settings.CONTEXT_MAX_CHARS)
        self.assertGreater(len(a.history), 2)
        # reset 清空
        a.reset_history()
        self.assertEqual(a.history, [])
        self.assertEqual(a.history_rounds, [])

    async def test_compress_history_keep_rounds(self):
        """按轮强压缩：保留最近 N 轮原文，更早轮次压入 compressed_summary。"""
        await self._configure4()
        a = self._collab("PRO")
        a.reset_history()
        for r in (1, 2, 3):
            a.append_history("user", f"u{r}", round_no=r)
            a.append_history("assistant", f"a{r}", round_no=r)
        # 不同轮次数 ≤ keep_rounds → 无需压缩
        self.assertIsNone(await a.compress_history(force=True, keep_rounds=3))

        async def fake_summarize(texts):
            return "历史要点", {"total_tokens": 10}
        a._summarize = fake_summarize
        usage = await a.compress_history(force=True, keep_rounds=2)
        self.assertIsNotNone(usage)
        self.assertEqual(usage.get("total_tokens"), 10)
        self.assertEqual(a.history_rounds, [2, 2, 3, 3])
        self.assertEqual(len(a.history), len(a.history_rounds))
        self.assertIn("历史要点", a.compressed_summary)
        # 二次压缩幂等：仍只剩 2 轮时无需再压
        self.assertIsNone(await a.compress_history(force=True, keep_rounds=2))

    # ---------------- 需求 2/3：对手定向反驳 ----------------

    async def test_find_opponent_pro_con_mapping(self):
        """PRO↔CON 互为对手；无对立场时回退全局最近；NEUTRAL 无对手。"""
        await self._configure4()
        pro = self._collab("PRO")
        con = self._collab("CON")
        neutral = self._collab("NEUTRAL")
        orchestrator.round = 2
        orchestrator.round_arguments[1] = {pro.name: "P1", con.name: "C1"}
        self.assertEqual(orchestrator._find_opponent(pro).name, con.name)
        self.assertEqual(orchestrator._find_opponent(con).name, pro.name)
        # 上一轮无对立场 → 回退全局最近发言
        orchestrator.round_arguments[1] = {pro.name: "P1"}
        orchestrator.latest_arguments[con.name] = {"text": "C1", "round": 1}
        self.assertEqual(orchestrator._find_opponent(pro).name, con.name)
        # 彻底无对立场 → None
        orchestrator.latest_arguments = {}
        self.assertIsNone(orchestrator._find_opponent(pro))
        # NEUTRAL 无固定对手
        self.assertIsNone(orchestrator._find_opponent(neutral))

    async def test_prepare_opponent_context_snippet_and_summary(self):
        """第 2 轮原文截断；第 3 轮起 LLM 摘要（缓存 + 预算记账）；第 1 轮不准备。"""
        await self._configure4()
        pro = self._collab("PRO")
        con = self._collab("CON")
        # 第 1 轮：不准备
        orchestrator.round = 1
        await orchestrator._prepare_opponent_context(pro)
        self.assertNotIn(pro.name, orchestrator._opponent_prep)
        # 第 2 轮：原文截断到 OPPONENT_SNIPPET_MAX_CHARS
        orchestrator.round = 2
        orchestrator.round_arguments[1] = {pro.name: "P1", con.name: "C" * 1000}
        await orchestrator._prepare_opponent_context(pro)
        prep = orchestrator._opponent_prep[pro.name]
        self.assertEqual(prep["speaker"], con.name)
        self.assertEqual(prep["round"], 1)
        self.assertEqual(len(prep["text"]), settings.OPPONENT_SNIPPET_MAX_CHARS)
        # 第 3 轮：LLM 摘要（mock summarize_text），并缓存
        from app import orchestrator as orch_mod
        orchestrator.round = 3
        orchestrator.round_arguments[2] = {con.name: "C" * 1000}
        calls = []

        async def fake_summarize_text(model_config, text, max_chars=150):
            calls.append(max_chars)
            return "浓缩要点", {"total_tokens": 20}
        orig = orch_mod.summarize_text
        orch_mod.summarize_text = fake_summarize_text
        try:
            await orchestrator._prepare_opponent_context(pro)
        finally:
            orch_mod.summarize_text = orig
        prep = orchestrator._opponent_prep[pro.name]
        self.assertEqual(prep["text"], "浓缩要点")
        self.assertEqual(calls, [settings.OPPONENT_SUMMARY_MAX_CHARS])
        # 同一 (对手,轮次) 再次准备：命中缓存，不重复调用 LLM
        await orchestrator._prepare_opponent_context(pro)
        self.assertEqual(len(calls), 1)
        # 摘要调用计入预算
        self.assertGreaterEqual(orchestrator.spent, 20)

    async def test_debate_prompt_injects_opponent_and_char_hint(self):
        """需求 1/5：第 1 轮立论 + 公平性 L1 字数提示；第 2 轮起注入对手发言。"""
        await self._configure4()
        pro = self._collab("PRO")
        # 第 1 轮：立论 + 字数提示（无对手参照 → 立论上限）
        orchestrator.round = 1
        limit1 = orchestrator.governor.ceiling_for("PRO", 1, None)
        p1 = orchestrator._debate_prompt(pro)
        self.assertIn("立论", p1)
        self.assertIn(f"{limit1} 字", p1)
        # 第 2 轮：对手发言注入；尚无对手输出参照时沿用立论上限
        orchestrator.round = 2
        orchestrator._opponent_prep[pro.name] = {
            "speaker": "反方", "stance": "反方", "round": 1, "text": "必须重视成本"}
        limit2 = orchestrator.governor.ceiling_for(
            "PRO", 2, orchestrator.governor.recent_opponent_chars("PRO"))
        p2 = orchestrator._debate_prompt(pro)
        self.assertIn("必须重视成本", p2)
        self.assertIn(f"{limit2} 字", p2)

    # ---------------- 需求 5：输出截断兜底 ----------------

    async def test_agent_turn_truncates_long_discussion_output(self):
        """discussion 模式即使 LLM 超长输出，也按公平性 L1 上限截断兜底（不依赖 max_tokens）。"""
        await self._configure4()
        long_text = "长" * 500

        async def _fake_retry(*a, **k):
            return _fake_llm_result(content=long_text)
        orchestrator._run_llm_retry = _fake_retry
        # 对手最近有效输出极短 → 相对基准下限兜底；PRO 第 2 轮反驳输出超限被截断
        orchestrator.governor.record_turn("CON", 1, visible_chars=10, think_chars=0)
        orchestrator.round = 2
        out = await orchestrator._agent_turn(
            self._collab("PRO"), "请发言", mode="discussion", stream=False)
        ceiling = orchestrator.governor.ceiling_for(
            "PRO", 2, orchestrator.governor.recent_opponent_chars("PRO"))
        self.assertLessEqual(len(out["content"]), ceiling)
        self.assertLess(len(out["content"]), len(long_text))  # 确实发生截断
        # 未超长时不截断（第 1 轮立论上限足够容纳 500 字）
        orchestrator.round = 1
        out2 = await orchestrator._agent_turn(
            self._collab("CON"), "请发言", mode="discussion", stream=False)
        self.assertEqual(out2["content"], long_text)

    # ---------------- 需求 3：摘要回退 ----------------

    async def test_summarize_text_fallback_truncates(self):
        """LLM 摘要失败时确定性头尾截断回退，绝不超过 max_chars。"""
        from app.llm import summarize_text
        text = "甲乙丙丁" * 200  # 800 字
        s, usage = await summarize_text({}, text, max_chars=100)
        self.assertIsNone(usage)
        self.assertLessEqual(len(s), 100)
        self.assertIn("…", s)
        self.assertTrue(s.startswith("甲"))
        # 短文本原样返回
        s2, _ = await summarize_text({}, "短文本", max_chars=100)
        self.assertEqual(s2, "短文本")


class MemorySummarizeTest(unittest.IsolatedAsyncioTestCase):
    """短期 → 情境记忆的 LLM 摘要（开启 LLM 摘要替换简单拼接）。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.mem = MemorySystem("test_mem", Path(self._tmp.name))

    def tearDown(self):
        self._tmp.cleanup()

    async def test_default_concat_when_no_summarizer(self):
        """未提供 summarizer 时保持简单拼接（近期要点前缀），短期清空。"""
        self.mem.add_short("要点甲", importance=0.6)
        self.mem.add_short("要点乙", importance=0.6)
        out = await self.mem.summarize_short_to_context()
        self.assertTrue(out.startswith("近期要点："))
        self.assertIn("要点甲", out)
        self.assertEqual(len(self.mem.contextual), 1)
        self.assertEqual(self.mem.contextual[0]["text"], out)
        self.assertEqual(self.mem.short_term, [])  # 压缩后短期清空

    async def test_sync_summarizer_used(self):
        """同步 summarizer 的返回值作为情境摘要落库。"""
        self.mem.add_short("a", importance=0.6)
        self.mem.add_short("b", importance=0.6)
        out = await self.mem.summarize_short_to_context(
            lambda texts: "LLM 摘要结果")
        self.assertEqual(out, "LLM 摘要结果")
        self.assertEqual(self.mem.contextual[0]["text"], "LLM 摘要结果")

    async def test_async_summarizer_used(self):
        """async summarizer（协程）同样被 await。"""
        self.mem.add_short("a", importance=0.6)

        async def _sum(texts):
            await asyncio.sleep(0)
            return "异步摘要结果"
        out = await self.mem.summarize_short_to_context(_sum)
        self.assertEqual(out, "异步摘要结果")
        self.assertEqual(self.mem.contextual[0]["text"], "异步摘要结果")

    async def test_summarizer_exception_falls_back(self):
        """summarizer 抛异常时回退拼接，不中断。"""
        self.mem.add_short("甲", importance=0.6)
        self.mem.add_short("乙", importance=0.6)

        def _boom(texts):
            raise RuntimeError("LLM 挂了")
        out = await self.mem.summarize_short_to_context(_boom)
        self.assertEqual(out, "甲；乙")

    async def test_summarizer_empty_result_falls_back(self):
        """summarizer 返回空串时回退拼接，避免空摘要落库。"""
        self.mem.add_short("甲", importance=0.6)
        out = await self.mem.summarize_short_to_context(lambda texts: "")
        self.assertEqual(out, "甲")

    async def test_empty_short_term_noop(self):
        """无短期记忆时不调用 summarizer，返回 None。"""
        called = []

        def _should_not_run(texts):
            called.append(1)
            return "x"
        out = await self.mem.summarize_short_to_context(_should_not_run)
        self.assertIsNone(out)
        self.assertEqual(called, [])
        self.assertEqual(self.mem.contextual, [])

    # ---------------- 编排器接入：LLM 摘要器 + 预算记账 ----------------

    async def test_orchestrator_llm_summarizer_wires_budget(self):
        """_memory_summarizer 用 summarize_text 生成摘要并把 usage 计入预算。"""
        orchestrator.configure([PRO_CFG, CON_CFG], topic="记忆摘要", max_rounds=3)
        a = orchestrator.collaborators[0]
        # 清掉磁盘/历史残留，保证断言只针对本用例新增内容
        a.memory.short_term = []
        a.memory.contextual = []
        a.memory.long_term = []
        a.memory.add_short("甲：观点一", importance=0.6)
        a.memory.add_short("乙：观点二", importance=0.6)
        orchestrator.spent = 100
        orchestrator.run_stats = {"compressions": 0}
        fake_usage = {"total_tokens": 42}
        with patch("app.orchestrator.summarize_text",
                   new=AsyncMock(return_value=("LLM 压缩摘要", fake_usage))):
            await a.memory.summarize_short_to_context(
                orchestrator._memory_summarizer(a))
        self.assertEqual(a.memory.contextual[0]["text"], "LLM 压缩摘要")
        self.assertEqual(a.memory.short_term, [])
        self.assertEqual(orchestrator.spent, 142)  # 100 + 42 记账
        self.assertEqual(orchestrator.run_stats.get("memory_summaries"), 1)


if __name__ == "__main__":
    unittest.main()
