"""
test_ci_fast.py — 快速确定性单元测试（CI 用，无需真实 LLM API）

覆盖本次控制流修复的三个核心行为：
  1. pause() / resume() / end() 会向事件总线广播 status 事件（前端据此切换按钮）；
  2. 运行中手动 end() 后：状态回到 stopped、phase=idle，并跳过裁判裁决与主持人汇总；
  3. 正常路径完整跑完一轮：仍会产出 judgment / summary / status=done 事件，流程未被破坏。
  4. 预算规划器与降级阶梯：预算充足用满 max_tokens；吃紧自动降上限；
     仍不足强制压缩历史；允许小幅超支跑完辩论（熔断仅作最后防线）。

运行方式（无需服务器、无需真实 LLM）：
    python -m unittest tests.test_ci_fast -v
"""
import asyncio
import math
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from app.config import settings
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


class OrchestratorControlTest(unittest.IsolatedAsyncioTestCase):
    """编排器控制流（暂停/继续/结束 + 手动结束收尾）测试。"""

    def setUp(self):
        # 隔离校准档案：run_discussion 会 load/save，避免用 mock LLM 数据污染真实档案
        self._tmp_dir = tempfile.TemporaryDirectory()
        self._old_profile_path = settings.BUDGET_PROFILE_PATH
        settings.BUDGET_PROFILE_PATH = os.path.join(
            self._tmp_dir.name, "budget_profile.json")

    def tearDown(self):
        settings.BUDGET_PROFILE_PATH = self._old_profile_path
        self._tmp_dir.cleanup()

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


class BudgetPlannerTest(unittest.IsolatedAsyncioTestCase):
    """预算规划器与降级阶梯（预算充足 / 吃紧 / 强制压缩 / 小额超支不中断）。"""

    def setUp(self):
        # 隔离校准档案：run_discussion 相关测试会 load/save，避免污染真实档案
        self._tmp_dir = tempfile.TemporaryDirectory()
        self._old_profile_path = settings.BUDGET_PROFILE_PATH
        settings.BUDGET_PROFILE_PATH = os.path.join(
            self._tmp_dir.name, "budget_profile.json")

    def tearDown(self):
        settings.BUDGET_PROFILE_PATH = self._old_profile_path
        self._tmp_dir.cleanup()

    async def _configure(self, max_rounds=3):
        orchestrator.configure([HOST_CFG, PRO_CFG], topic="预算规划测试",
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

    async def test_planner_large_budget_uses_max_tokens(self):
        """预算充足：生效上限=用户 max_tokens，无需降级、无超支。"""
        await self._configure(max_rounds=3)
        orchestrator.budget = 1_000_000
        orchestrator.spent = 0
        plan = orchestrator._compute_plan("debate", 3)
        self.assertEqual(plan["effective_max_tokens"], orchestrator.max_tokens)
        self.assertFalse(plan["overshoot"])
        self.assertFalse(plan["degraded"])
        self.assertGreaterEqual(plan["feasible_rounds"], 3)

    async def test_planner_tight_budget_lowers_cap(self):
        """预算吃紧：自动下调生效输出上限（低于 max_tokens，但不低于下限、不超支）。

        注：预算数值需随规划估算精度的提升保持"吃紧"语义——当前数学下
        80000 足够 2 席 3 轮（projected≈71K），不再触发降级，故取 60000。
        """
        await self._configure(max_rounds=3)
        orchestrator.budget = 60000
        orchestrator.spent = 0
        plan = orchestrator._compute_plan("debate", 3)
        self.assertLess(plan["effective_max_tokens"], orchestrator.max_tokens)
        self.assertGreaterEqual(plan["effective_max_tokens"], settings.BUDGET_OUTPUT_FLOOR)
        self.assertFalse(plan["overshoot"])
        self.assertTrue(plan["degraded"])

    async def test_planner_degradation_forces_compression(self):
        """预算不足下限：触发对所有 Agent 的强制历史压缩，之后仍允许超支跑完。"""
        await self._configure(max_rounds=3)
        orchestrator.budget = 1000
        orchestrator.spent = 0
        calls = []
        for a in orchestrator.agents:
            async def fake_compress(agent=a, **kwargs):
                calls.append((agent.name, kwargs.get("force"), kwargs.get("keep_rounds")))
                if len(agent.history) > 2:
                    agent.history = agent.history[-2:]
                return {"total_tokens": 10}
            a.compress_history = fake_compress

        plan = await orchestrator._plan_for_round(3)
        self.assertEqual(len(calls), len(orchestrator.agents))
        self.assertTrue(all(force is True for _, force, _ in calls))
        # 需求 6：强压缩按轮保留（keep_rounds = BUDGET_KEEP_LAST_ROUNDS）
        self.assertTrue(all(k == settings.BUDGET_KEEP_LAST_ROUNDS for _, _, k in calls))
        self.assertGreaterEqual(orchestrator.run_stats.get("compressions", 0), 1)
        self.assertEqual(plan["effective_max_tokens"],
                         min(settings.BUDGET_OUTPUT_FLOOR, orchestrator.max_tokens))
        self.assertTrue(plan["overshoot"])
        self.assertEqual(orchestrator.effective_max_tokens, plan["effective_max_tokens"])

    async def test_planner_overshoot_logs_but_run_completes(self):
        """极小预算：整场辩论仍完整跑完（judge + summary 均产出），熔断未触发。"""
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

        orchestrator.status = "idle"
        task = asyncio.create_task(orchestrator.run_discussion(budget=200))
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
        self.assertIn("budget.overshoot", seen)
        self.assertNotIn("budget.circuit_break", seen)

        orchestrator.event_bus.unsubscribe(q)

    async def test_status_sync_has_budget_plan_fields(self):
        """_status_sync 应携带预算规划字段供前端展示。"""
        await self._configure(max_rounds=3)
        orchestrator.budget = 20000
        orchestrator.spent = 0
        await orchestrator._plan_for_round(3)
        s = orchestrator._status_sync()
        for key in ("effective_max_tokens", "feasible_rounds", "projected_total",
                    "budget_overshoot", "budget_degraded", "est_factor", "call_factor"):
            self.assertIn(key, s)
        self.assertIsInstance(s["effective_max_tokens"], int)
        self.assertIsInstance(s["feasible_rounds"], int)
        self.assertIsInstance(s["budget_overshoot"], bool)
        self.assertIsInstance(s["est_factor"], float)
        self.assertIsInstance(s["call_factor"], float)

    async def test_planner_est_factor_ema_calibration(self):
        """自适应校准：实测/估算比值经 EMA 平滑更新因子，并钳制异常观测。"""
        await self._configure(max_rounds=3)
        orchestrator._est_factor = 1.0
        orchestrator._est_factor_samples = 0
        # 实测 500 / 估算 100 → 5×，EMA α=0.2：1.0 → 1.8
        orchestrator._update_est_factor(real_total=500, heuristic_total=100)
        self.assertAlmostEqual(orchestrator._est_factor, 1.8)
        self.assertEqual(orchestrator._est_factor_samples, 1)
        # 实测 60 / 估算 100 → 0.6×，继续平滑：1.8 → 1.56
        orchestrator._update_est_factor(real_total=60, heuristic_total=100)
        self.assertAlmostEqual(orchestrator._est_factor, 0.8 * 1.8 + 0.2 * 0.6)
        # 钳制上限：因子不超 10×
        orchestrator._est_factor = 10.0
        orchestrator._update_est_factor(real_total=10000, heuristic_total=1)
        self.assertLessEqual(orchestrator._est_factor, 10.0)
        # 钳制下限：因子不低于 0.5×
        orchestrator._est_factor = 0.5
        orchestrator._update_est_factor(real_total=1, heuristic_total=10000)
        self.assertGreaterEqual(orchestrator._est_factor, 0.5)
        # 非法样本（实测或估算≤0）不污染因子
        prev = orchestrator._est_factor
        orchestrator._update_est_factor(real_total=0, heuristic_total=100)
        orchestrator._update_est_factor(real_total=100, heuristic_total=0)
        self.assertEqual(orchestrator._est_factor, prev)

    async def test_planner_est_factor_scales_forward_estimates(self):
        """校准因子会放大前置估算（agent 调用 / 裁判 / 汇总）。"""
        await self._configure(max_rounds=3)
        orchestrator._est_factor = 1.0
        p_base = orchestrator._estimate_call_prompt(orchestrator.main, "测试指令")
        orchestrator._est_factor = 3.0
        p_scaled = orchestrator._estimate_call_prompt(orchestrator.main, "测试指令")
        self.assertAlmostEqual(p_scaled, p_base * 3.0, delta=max(2, p_base))
        # 裁判与汇总的提示词估算同样被放大（在 1.0 因子下取基准）
        orchestrator._est_factor = 1.0
        j_base = orchestrator._judge_prompt_tokens_now()
        s_base = orchestrator._summary_prompt_tokens()
        orchestrator._est_factor = 2.0
        self.assertAlmostEqual(
            orchestrator._judge_prompt_tokens_now(), j_base * 2.0, delta=1)
        self.assertAlmostEqual(
            orchestrator._summary_prompt_tokens(), s_base * 2.0, delta=1)

    async def test_planner_round_prompt_models_n_calls(self):
        """每轮提示词估算应按 N 次 LLM 调用折算（而非每位辩手一次）。"""
        await self._configure(max_rounds=3)
        per_agent = (orchestrator._estimate_call_prompt(
                         orchestrator.main,
                         orchestrator._main_directive(orchestrator.round, False, False, False))
                     + orchestrator._estimate_call_prompt(
                         orchestrator.collaborators[0],
                         orchestrator._debate_prompt(orchestrator.collaborators[0])))
        round_p = orchestrator._estimate_round_prompt_tokens()
        base = max(1, 1 + len(orchestrator.collaborators))  # 2
        n = orchestrator._calls_per_round()                  # ceil(2×1.5)=3
        self.assertEqual(round_p, round(per_agent * n / base))
        self.assertGreater(round_p, per_agent)

    async def test_planner_call_factor_ema_calibration(self):
        """自适应 N：本轮实测调用/规划基数的比值经 EMA 平滑更新因子，并钳制异常观测。"""
        await self._configure(max_rounds=3)
        orchestrator._call_factor = 1.0
        orchestrator._call_factor_samples = 0
        base_n = max(1, math.ceil(
            (1 + len(orchestrator.collaborators)) * settings.BUDGET_TOOL_OVERHEAD))
        # 实测 3×基数 → 比值 3.0，EMA α=0.2：1.0 → 1.4
        orchestrator._update_call_factor(real_calls=3 * base_n)
        self.assertAlmostEqual(orchestrator._call_factor, 1.4)
        self.assertEqual(orchestrator._call_factor_samples, 1)
        # 实测恰为基数 → 比值 1.0，继续平滑：1.4 → 1.32
        orchestrator._update_call_factor(real_calls=base_n)
        self.assertAlmostEqual(orchestrator._call_factor, 0.8 * 1.4 + 0.2 * 1.0)
        # 钳制上限：因子不超 5.0
        orchestrator._call_factor = 5.0
        orchestrator._update_call_factor(real_calls=100 * base_n)
        self.assertLessEqual(orchestrator._call_factor, 5.0)
        # 钳制下限：因子不低于 0.5
        orchestrator._call_factor = 0.5
        orchestrator._update_call_factor(real_calls=1)
        self.assertGreaterEqual(orchestrator._call_factor, 0.5)
        # 非法样本（0 次调用）不污染因子
        prev = orchestrator._call_factor
        orchestrator._update_call_factor(real_calls=0)
        self.assertEqual(orchestrator._call_factor, prev)

    async def test_planner_call_factor_scales_calls_per_round(self):
        """自适应 N 因子会放大 _calls_per_round（进而放大每轮成本与输出量）。"""
        await self._configure(max_rounds=3)
        orchestrator._call_factor = 1.0
        n0 = orchestrator._calls_per_round()
        self.assertEqual(n0, math.ceil(
            (1 + len(orchestrator.collaborators)) * settings.BUDGET_TOOL_OVERHEAD))
        orchestrator._call_factor = 2.5
        self.assertEqual(
            orchestrator._calls_per_round(), max(1, round(n0 * 2.5)))
        # 因子作用于每轮提示词估算（总估算按 N/base 折算，N 变大则总成本变大）
        orchestrator._call_factor = 1.0
        p0 = orchestrator._estimate_round_prompt_tokens()
        orchestrator._call_factor = 2.0
        p1 = orchestrator._estimate_round_prompt_tokens()
        self.assertGreater(p1, p0)

    # ---------------- 历史增长模型 g / 档案持久化 / 自动削减轮次 ----------------

    async def test_planner_growth_ratio_ema_calibration(self):
        """自适应历史增长比 g：环比 EMA 平滑，样本#1 跳过，负增长钳 0，钳制 [MIN, MAX]。"""
        await self._configure(max_rounds=3)
        orchestrator._growth_ratio = settings.BUDGET_GROWTH_RATIO_INIT
        orchestrator._growth_ratio_samples = 0
        # 样本#1（无上轮基准）：跳过更新，仅记录 prev 供第 2 轮使用
        orchestrator._update_growth_ratio(real_round_prompt=100)
        self.assertEqual(orchestrator._growth_ratio, settings.BUDGET_GROWTH_RATIO_INIT)
        self.assertEqual(orchestrator._growth_ratio_samples, 0)
        # 第 2 轮环比 (300-100)/100=2.0，EMA α=0.2：0.5 → 0.8
        orchestrator._update_growth_ratio(real_round_prompt=300)
        self.assertAlmostEqual(orchestrator._growth_ratio, 0.8)
        self.assertEqual(orchestrator._growth_ratio_samples, 1)
        # 负增长钳 0：环比 (200-300)/300<0 → 0，EMA 0.8 → 0.64
        orchestrator._update_growth_ratio(real_round_prompt=200)
        self.assertAlmostEqual(
            orchestrator._growth_ratio, 0.8 * 0.8 + 0.2 * 0.0)
        # 钳制上限：因子不超 BUDGET_GROWTH_RATIO_MAX
        orchestrator._growth_ratio = settings.BUDGET_GROWTH_RATIO_MAX
        orchestrator._prev_round_prompt_real = 1
        orchestrator._update_growth_ratio(real_round_prompt=1000)
        self.assertLessEqual(orchestrator._growth_ratio, settings.BUDGET_GROWTH_RATIO_MAX)
        # 钳制下限：因子不低于 BUDGET_GROWTH_RATIO_MIN
        orchestrator._growth_ratio = settings.BUDGET_GROWTH_RATIO_MIN
        orchestrator._prev_round_prompt_real = 1000
        orchestrator._update_growth_ratio(real_round_prompt=1)
        self.assertGreaterEqual(orchestrator._growth_ratio, settings.BUDGET_GROWTH_RATIO_MIN)
        # 非法样本（0 用量）不污染因子、也不改 prev 基准
        prev_factor = orchestrator._growth_ratio
        prev_prompt = orchestrator._prev_round_prompt_real
        orchestrator._update_growth_ratio(real_round_prompt=0)
        self.assertEqual(orchestrator._growth_ratio, prev_factor)
        self.assertEqual(orchestrator._prev_round_prompt_real, prev_prompt)

    async def test_planner_growth_model_is_conservative(self):
        """历史增长模型生效：同预算下带 growth 的 prompt_forecast 更大、E 更保守。"""
        await self._configure(max_rounds=3)
        orchestrator.budget = 80000
        orchestrator.spent = 0
        orchestrator._growth_ratio = 0.5
        plan_growth = orchestrator._compute_plan("debate", 3)
        orchestrator._growth_ratio = 0.0
        plan_flat = orchestrator._compute_plan("debate", 3)
        self.assertGreater(plan_growth["prompt_forecast"], plan_flat["prompt_forecast"])
        self.assertLessEqual(
            plan_growth["effective_max_tokens"], plan_flat["effective_max_tokens"])
        # plan dict 应携带增长模型观测字段
        for key in ("growth_ratio", "safety_margin", "prompt_forecast"):
            self.assertIn(key, plan_growth)

    async def test_tail_base_tokens_reserves_outputs(self):
        """尾段基础成本 ≥ 裁判/汇总提示词 + 两者输出保底。"""
        await self._configure(max_rounds=3)
        j0 = (0 if not orchestrator.collaborators
              else orchestrator._judge_prompt_tokens_now())
        s0 = orchestrator._summary_prompt_tokens()
        base = orchestrator._tail_base_tokens()
        self.assertGreaterEqual(
            base, j0 + s0 + settings.JUDGE_MIN_OUTPUT + settings.SUMMARY_MIN_OUTPUT)
        self.assertGreaterEqual(base, settings.BUDGET_TAIL_RESERVE)

    async def test_plan_for_phase_judge_overshoot_keeps_min_output(self):
        """预算不足时，裁判阶段 E 抬到 JUDGE_MIN_OUTPUT 保底（避免 JSON 截断→确定性回退）。"""
        await self._configure(max_rounds=3)
        orchestrator.budget = 5000
        orchestrator.spent = 0
        orchestrator.max_tokens = 4096
        plan = await orchestrator._plan_for_phase("judge")
        self.assertTrue(plan["overshoot"])
        self.assertGreaterEqual(plan["effective_max_tokens"], settings.JUDGE_MIN_OUTPUT)

    async def test_planner_auto_trim_rounds(self):
        """BUDGET_AUTO_TRIM_ROUNDS=1：预算不足时自动削减 max_rounds（不削减到低于当前轮）。"""
        await self._configure(max_rounds=8)
        orchestrator.budget = 20000
        orchestrator.spent = 0
        orchestrator.round = 1  # 模拟规划发生在第 1 轮
        old_trim = settings.BUDGET_AUTO_TRIM_ROUNDS
        settings.BUDGET_AUTO_TRIM_ROUNDS = 1
        try:
            plan = await orchestrator._plan_for_round(8)
            self.assertTrue(plan["overshoot"])
            self.assertLess(orchestrator.max_rounds, 8)
            self.assertGreaterEqual(orchestrator.max_rounds, orchestrator.round)
        finally:
            settings.BUDGET_AUTO_TRIM_ROUNDS = old_trim

    async def test_planner_auto_trim_disabled_keeps_rounds(self):
        """BUDGET_AUTO_TRIM_ROUNDS=0：预算不足也不削减轮次（保持现状跑满）。"""
        await self._configure(max_rounds=8)
        orchestrator.budget = 20000
        orchestrator.spent = 0
        orchestrator.round = 1
        old_trim = settings.BUDGET_AUTO_TRIM_ROUNDS
        settings.BUDGET_AUTO_TRIM_ROUNDS = 0
        try:
            await orchestrator._plan_for_round(8)
            self.assertEqual(orchestrator.max_rounds, 8)
        finally:
            settings.BUDGET_AUTO_TRIM_ROUNDS = old_trim

    async def test_profile_save_load_roundtrip(self):
        """校准档案保存/载入往返（临时路径，不污染真实档案）。"""
        await self._configure(max_rounds=3)
        path = os.path.join(self._tmp_dir.name, "roundtrip.json")
        old_path = settings.BUDGET_PROFILE_PATH
        settings.BUDGET_PROFILE_PATH = path
        try:
            orchestrator._est_factor = 3.5
            orchestrator._est_factor_samples = 7
            orchestrator._call_factor = 2.2
            orchestrator._call_factor_samples = 3
            orchestrator._growth_ratio = 1.3
            orchestrator._growth_ratio_samples = 4
            orchestrator._save_profile()
            self.assertTrue(os.path.exists(path))
            # 重置后载入应还原
            orchestrator._est_factor = 1.0
            orchestrator._est_factor_samples = 0
            orchestrator._call_factor = 1.0
            orchestrator._call_factor_samples = 0
            orchestrator._growth_ratio = 0.5
            orchestrator._growth_ratio_samples = 0
            orchestrator._load_profile()
            self.assertAlmostEqual(orchestrator._est_factor, 3.5)
            self.assertEqual(orchestrator._est_factor_samples, 7)
            self.assertAlmostEqual(orchestrator._call_factor, 2.2)
            self.assertEqual(orchestrator._call_factor_samples, 3)
            self.assertAlmostEqual(orchestrator._growth_ratio, 1.3)
            self.assertEqual(orchestrator._growth_ratio_samples, 4)
        finally:
            settings.BUDGET_PROFILE_PATH = old_path

    async def test_profile_load_missing_uses_conservative_prior(self):
        """无档案时载入保守先验 EST_FACTOR_INIT；历史乐观档案的 est_factor 被钳制 ≥1.0。"""
        await self._configure(max_rounds=3)
        path = os.path.join(self._tmp_dir.name, "missing.json")
        old_path = settings.BUDGET_PROFILE_PATH
        settings.BUDGET_PROFILE_PATH = path
        try:
            orchestrator._load_profile()
            self.assertEqual(orchestrator._est_factor, settings.EST_FACTOR_INIT)
            self.assertEqual(orchestrator._est_factor_samples, 0)
            self.assertEqual(orchestrator._call_factor, 1.0)
            self.assertEqual(orchestrator._growth_ratio, settings.BUDGET_GROWTH_RATIO_INIT)
            # 载入乐观档案（est<1.0）应被钳制到 ≥1.0
            with open(path, "w", encoding="utf-8") as f:
                f.write('{"est_factor": 0.4, "est_samples": 5, '
                        '"call_factor": 0.6, "call_samples": 2, '
                        '"growth_ratio": 0.9, "growth_samples": 3}')
            orchestrator._load_profile()
            self.assertGreaterEqual(orchestrator._est_factor, 1.0)
            self.assertEqual(orchestrator._est_factor_samples, 5)
            self.assertGreaterEqual(orchestrator._call_factor, 0.5)
            self.assertGreaterEqual(orchestrator._growth_ratio, settings.BUDGET_GROWTH_RATIO_MIN)
        finally:
            settings.BUDGET_PROFILE_PATH = old_path

    async def test_status_sync_includes_growth_ratio(self):
        """_status_sync 携带 growth_ratio 字段供前端/观测。"""
        await self._configure(max_rounds=3)
        orchestrator._growth_ratio = 1.25
        s = orchestrator._status_sync()
        self.assertIn("growth_ratio", s)
        self.assertIsInstance(s["growth_ratio"], float)
        self.assertAlmostEqual(s["growth_ratio"], 1.25)


class BudgetMathTest(unittest.TestCase):
    """app/budget.py 纯数学核心（无外部依赖）。"""

    def test_prompt_growth_sum(self):
        """提示词算术级数和：R·P + g·P·R·(R-1)/2。"""
        from app import budget as budget_math
        self.assertEqual(budget_math.prompt_growth_sum(100, 0.5, 3), 450)
        self.assertEqual(budget_math.prompt_growth_sum(100, 0.0, 3), 300)
        self.assertEqual(budget_math.prompt_growth_sum(100, 0.5, 1), 100)
        self.assertEqual(budget_math.prompt_growth_sum(100, 0.5, 0), 0)
        self.assertEqual(budget_math.prompt_growth_sum(0, 0.5, 3), 0)

    def test_solve_output_cap(self):
        """反解输出上限：充足取 cap，吃紧降 cap，不足取 floor；钳制在 [floor, cap]。"""
        from app import budget as budget_math
        # 充足：usable - base - prompt ≥ denom·cap → 取 cap。
        # 验算（remaining=200000, usable=184000, prompt=45000, denom=14）：
        #   e = (184000-8000-45000)/14 = 9357 → 钳制到 4096。
        cap = budget_math.solve_output_cap(
            remaining=200000, base=8000, P=10000, g=0.5, N=3, S=1, R=3,
            floor=256, cap=4096, margin=0.08)
        self.assertEqual(cap, 4096)
        # 吃紧：numer/14 ∈ (256, 4096) → 降 cap 但不触底。
        #   remaining=80000, usable=73600, e = (73600-8000-45000)/14 ≈ 1471。
        e = budget_math.solve_output_cap(
            remaining=80000, base=8000, P=10000, g=0.5, N=3, S=1, R=3,
            floor=256, cap=4096, margin=0.08)
        self.assertGreater(e, 256)
        self.assertLess(e, 4096)
        # 不足：numer ≤ 0 → 取 floor。
        f = budget_math.solve_output_cap(
            remaining=5000, base=8000, P=10000, g=0.5, N=3, S=1, R=3,
            floor=256, cap=4096, margin=0.08)
        self.assertEqual(f, 256)

    def test_overshoot_at_floor(self):
        """下限输出仍超预算才判定 overshoot（margin 不参与判定）。"""
        from app import budget as budget_math
        self.assertTrue(budget_math.overshoot_at_floor(
            remaining=5000, base=8000, P=10000, g=0.5, N=3, S=1, R=3, floor=256))
        self.assertFalse(budget_math.overshoot_at_floor(
            remaining=100000, base=8000, P=10000, g=0.5, N=3, S=1, R=3, floor=256))

    def test_feasible_rounds(self):
        """从 max_r 向下迭代返回首个可行轮次（含 0）。"""
        from app import budget as budget_math
        # 充足：max_r 全可行
        self.assertEqual(budget_math.feasible_rounds(
            remaining=50000, base=5000, P=1000, g=0.1, N=3, S=1,
            E=100, max_r=5), 5)
        # 中间：仅部分轮次可行
        self.assertEqual(budget_math.feasible_rounds(
            remaining=40000, base=8000, P=10000, g=0.5, N=3, S=1,
            E=256, max_r=5), 2)
        # 不足：连尾段都覆盖不了 → 0
        self.assertEqual(budget_math.feasible_rounds(
            remaining=2000, base=8000, P=10000, g=0.5, N=3, S=1,
            E=256, max_r=5), 0)


class DebateMechanismTest(unittest.IsolatedAsyncioTestCase):
    """辩论机制改造（需求 1-6）：对手定向反驳、输出约束、按轮强压缩。"""

    def setUp(self):
        # 隔离校准档案
        self._tmp_dir = tempfile.TemporaryDirectory()
        self._old_profile_path = settings.BUDGET_PROFILE_PATH
        settings.BUDGET_PROFILE_PATH = os.path.join(
            self._tmp_dir.name, "budget_profile.json")

    def tearDown(self):
        settings.BUDGET_PROFILE_PATH = self._old_profile_path
        self._tmp_dir.cleanup()

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
        """需求 1/5：第 1 轮立论 + 全轮字数提示；第 2 轮起注入对手发言。"""
        await self._configure4()
        pro = self._collab("PRO")
        limit = max(1, int(orchestrator.effective_max_tokens * settings.OUTPUT_CHAR_PER_TOKEN))
        # 第 1 轮：立论 + 字数提示
        orchestrator.round = 1
        p1 = orchestrator._debate_prompt(pro)
        self.assertIn("立论", p1)
        self.assertIn(f"{limit} 字", p1)
        # 第 2 轮：对手发言注入
        orchestrator.round = 2
        orchestrator._opponent_prep[pro.name] = {
            "speaker": "反方", "stance": "反方", "round": 1, "text": "必须重视成本"}
        p2 = orchestrator._debate_prompt(pro)
        self.assertIn("必须重视成本", p2)
        self.assertIn(f"{limit} 字", p2)

    # ---------------- 需求 5：输出截断兜底 ----------------

    async def test_agent_turn_truncates_long_discussion_output(self):
        """discussion 模式即使 LLM 超长输出，也按字数上限截断兜底。"""
        await self._configure4()
        long_text = "长" * 500

        async def _fake_retry(*a, **k):
            return _fake_llm_result(content=long_text)
        orchestrator._run_llm_retry = _fake_retry
        orchestrator.effective_max_tokens = 50
        out = await orchestrator._agent_turn(
            self._collab("PRO"), "请发言", mode="discussion", stream=False)
        limit = max(1, int(50 * settings.OUTPUT_CHAR_PER_TOKEN))
        self.assertLessEqual(len(out["content"]), limit)
        # 未超长时不截断
        orchestrator.effective_max_tokens = 4096
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
