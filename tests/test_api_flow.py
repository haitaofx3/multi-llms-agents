"""
test_api_flow.py — HTTP 接口级集成测试（无需真实 LLM）

使用 httpx.AsyncClient + ASGITransport 直接驱动 FastAPI 应用，
monkeypatch 编排器的 LLM 调用点（_consume_stream / _run_llm_retry /
_judge_round / _final_summary）实现确定性输出，覆盖：

  1. configure → start → running → done → export → save_markdown → history 全链路；
  2. /api/save_markdown 路径穿越防护（filename 仅保留 basename）；
  3. /api/save_markdown 空请求体（前端发送无 body 的 POST）可用默认文件名；
  4. /api/configure 空 Agent 列表返回既有 400 错误契约；
  5. /api/configure 返回公平性治理报告（fairness），不再有 budget/max_tokens 字段；
  6. /api/status 携带公平性治理报告与只读 token 遥测（spent_tokens/output_tokens）。

运行方式（无需服务器、无需真实 LLM）：
    python -m unittest tests.test_api_flow -v
"""
import asyncio
import unittest

import httpx

from app import main as main_module
from app.orchestrator import orchestrator
from app.protocol import Judgment

HOST_CFG = {"name": "主持", "role": "主持", "personality": "",
            "provider": "custom", "stance": "HOST",
            "base_url": "https://example.invalid/v1",
            "api_key": "sk-test", "model_id": "test-model",
            "agent_id": "agent_host"}
PRO_CFG = {**HOST_CFG, "name": "正方", "role": "正方", "stance": "PRO",
           "agent_id": "agent_pro"}
CON_CFG = {**HOST_CFG, "name": "反方", "role": "反方", "stance": "CON",
           "agent_id": "agent_con"}


def _fake_llm_result(content="发言"):
    return {"content": content, "tool_calls": [], "assistant_message": None,
            "usage": {"total_tokens": 1}}


class ApiFlowTest(unittest.IsolatedAsyncioTestCase):
    """HTTP 接口全流程（无真实 LLM，CI 可用）。"""

    async def asyncSetUp(self):
        self.client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=main_module.app),
            base_url="http://test",
        )
        # 屏蔽所有 LLM 调用点：确定性返回，避免真实网络请求
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

    async def asyncTearDown(self):
        orchestrator.end()
        await self.client.aclose()

    async def _configure_and_run(self, agents, topic="测试主题", max_rounds=1):
        """配置 + 启动，并轮询 /api/status 直到 done；返回最终状态体。"""
        cfg = await self.client.post("/api/configure", json={
            "agents": agents, "topic": topic, "max_rounds": max_rounds,
        })
        self.assertEqual(cfg.status_code, 200, cfg.text)
        self.assertTrue(cfg.json()["ok"])

        st = await self.client.post("/api/start", json={"max_rounds": max_rounds})
        self.assertEqual(st.status_code, 200, st.text)

        body = None
        for _ in range(150):
            await asyncio.sleep(0.1)
            r = await self.client.get("/api/status")
            body = r.json()
            if body["status"] == "done":
                break
        else:
            self.fail("讨论未在时限内结束")
        return body

    async def test_full_flow_via_http(self):
        """configure → start → done → export → save_markdown → history 全链路。"""
        body = await self._configure_and_run(
            [HOST_CFG, PRO_CFG, CON_CFG], topic="机器人应不应该被赋予人权？")
        self.assertEqual(body["phase"], "finished")
        self.assertIsNotNone(body["judgment"])
        self.assertEqual(body["judgment"]["winner"], "PRO")

        # 导出 Markdown 应包含主题与裁决
        ex = await self.client.get("/api/export")
        self.assertEqual(ex.status_code, 200)
        md = ex.json()["markdown"]
        self.assertIn("机器人应不应该被赋予人权", md)

        # 保存后可通过历史接口读回
        saved = await self.client.post(
            "/api/save_markdown", json={"filename": "api_flow_test.md"})
        self.assertEqual(saved.status_code, 200)
        self.assertEqual(saved.json()["filename"], "api_flow_test.md")
        self.assertTrue(saved.json()["path"].endswith("api_flow_test.md"))

        h = await self.client.get("/api/history/api_flow_test.md")
        self.assertEqual(h.status_code, 200)
        self.assertIn("机器人应不应该被赋予人权", h.json()["markdown"])

    async def test_save_markdown_rejects_path_traversal(self):
        """../../evil.md 应被清洗为 evil.md 且落在 DISCUSSIONS_DIR 内。"""
        await self._configure_and_run([HOST_CFG, PRO_CFG])

        resp = await self.client.post(
            "/api/save_markdown", json={"filename": "../../evil.md"})
        self.assertEqual(resp.status_code, 200)
        fname = resp.json()["filename"]
        self.assertEqual(fname, "evil.md")  # 只保留 basename

        target = main_module.DISCUSSIONS_DIR / fname
        self.assertTrue(target.is_file())  # 写入发生在讨论目录内
        # 项目目录之外不应出现 evil.md
        self.assertFalse((main_module.PROJECT_ROOT.parent / "evil.md").exists())

    async def test_save_markdown_empty_body_uses_default_name(self):
        """前端无 body 的 POST 也应成功，并生成默认文件名。"""
        await self._configure_and_run([HOST_CFG, PRO_CFG])

        resp = await self.client.post("/api/save_markdown")
        self.assertEqual(resp.status_code, 200)
        fname = resp.json()["filename"]
        self.assertTrue(fname.startswith("discussion_"))
        self.assertTrue(fname.endswith(".md"))

    async def test_configure_rejects_empty_agents(self):
        """空 Agent 列表保持既有 400 错误契约（{ok, error}）。"""
        resp = await self.client.post("/api/configure", json={"agents": []})
        self.assertEqual(resp.status_code, 400)
        self.assertIn("error", resp.json())

    async def test_configure_returns_fairness_only(self):
        """/api/configure 应返回公平性治理报告（L1-L6），不再返回 budget/max_tokens。"""
        cfg = await self.client.post("/api/configure", json={
            "agents": [HOST_CFG, PRO_CFG, CON_CFG], "topic": "公平性可行性",
            "max_rounds": 3,
        })
        self.assertEqual(cfg.status_code, 200, cfg.text)
        data = cfg.json()
        self.assertTrue(data["ok"])
        # 公平性治理报告（六层）
        fair = data.get("fairness")
        self.assertIsNotNone(fair)
        self.assertIn("ok", fair)
        self.assertIsInstance(fair["layers"], list)
        self.assertEqual({l["layer"] for l in fair["layers"]},
                         {"L1", "L2", "L3", "L4", "L5", "L6"})
        # total_tokens 预算限制已删除：不再返回 budget / max_tokens / feasibility
        for key in ("budget", "max_tokens", "feasibility",
                    "effective_max_tokens", "projected_total"):
            self.assertNotIn(key, data)

    async def test_api_status_has_fairness_and_token_telemetry(self):
        """/api/status 应携带公平性治理报告与只读 token 遥测（实时显示用）。"""
        cfg = await self.client.post("/api/configure", json={
            "agents": [HOST_CFG, PRO_CFG], "topic": "公平性状态",
            "max_rounds": 2,
        })
        self.assertEqual(cfg.status_code, 200, cfg.text)
        r = await self.client.get("/api/status")
        self.assertEqual(r.status_code, 200)
        s = r.json()
        self.assertIn("fairness", s)
        self.assertIsInstance(s["fairness"]["ok"], bool)
        self.assertEqual({l["layer"] for l in s["fairness"]["layers"]},
                         {"L1", "L2", "L3", "L4", "L5", "L6"})
        # 只读遥测：累计输出 token / 累计用量 / 耗时（前端实时显示）
        for key in ("spent_tokens", "output_tokens", "elapsed_time_ms"):
            self.assertIn(key, s)
        self.assertIsInstance(s["output_tokens"], int)
        self.assertIsInstance(s["spent_tokens"], int)
        self.assertGreaterEqual(s["output_tokens"], 0)
        self.assertGreaterEqual(s["spent_tokens"], 0)
        self.assertIsInstance(s["elapsed_time_ms"], int)
        # total_tokens 预算限制已删除：不再返回任何预算字段
        for key in ("budget", "remaining_budget", "spent_budget",
                    "effective_max_tokens", "feasible_rounds", "projected_total",
                    "budget_overshoot", "budget_degraded",
                    "est_factor", "call_factor", "growth_ratio",
                    "safety_margin", "prompt_forecast"):
            self.assertNotIn(key, s)


if __name__ == "__main__":
    unittest.main()
