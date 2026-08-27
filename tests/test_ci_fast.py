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
import unittest

from app.config import settings
from app.orchestrator import orchestrator
from app.protocol import Judgment

HOST_CFG = {"name": "主持", "role": "主持", "personality": "",
            "provider": "default", "stance": "HOST"}
PRO_CFG = {"name": "正方", "role": "正方", "personality": "",
           "provider": "default", "stance": "PRO"}


def _fake_llm_result(content="发言"):
    return {"content": content, "tool_calls": [], "assistant_message": None,
            "usage": {"total_tokens": 1}}


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
        """预算吃紧：自动下调生效输出上限（低于 max_tokens，但不低于下限、不超支）。"""
        await self._configure(max_rounds=3)
        orchestrator.budget = 50000
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
                calls.append((agent.name, kwargs.get("force")))
                if len(agent.history) > 2:
                    agent.history = agent.history[-2:]
                return {"total_tokens": 10}
            a.compress_history = fake_compress

        plan = await orchestrator._plan_for_round(3)
        self.assertEqual(len(calls), len(orchestrator.agents))
        self.assertTrue(all(force is True for _, force in calls))
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
                    "budget_overshoot", "budget_degraded"):
            self.assertIn(key, s)
        self.assertIsInstance(s["effective_max_tokens"], int)
        self.assertIsInstance(s["feasible_rounds"], int)
        self.assertIsInstance(s["budget_overshoot"], bool)


if __name__ == "__main__":
    unittest.main()
