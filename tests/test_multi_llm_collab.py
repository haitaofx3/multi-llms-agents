"""
test_multi_llm_collab.py — 多模型协同试点新增能力的回归测试

覆盖六块新增能力（对应需求 1-6）：

  1. 多模型协同：模型花名册（/api/models）、协同配方（/api/recipes*）与按配方配置席位；
  2. 可观测性：结构化日志查询（/api/logs）、trace 汇总（/api/trace）、运行摘要（/api/summary）、
     复盘导出三种格式（md / json / html）、运行事件日志（runlog）；
  3. 接口级测试：/api/dev/endpoints 清单、/api/dev/invoke 通用调用、/api/dev/smoke 冒烟；
  4. 中途注入纠偏：SteeringQueue 语义（作用域 / 优先级 / 撤销 / 渲染）、
     /api/steer 端点流程、指令真正进入 LLM 的 messages；
  5. 补充材料续轮：上一轮归档 + 承接包生成 + 迭代号推进 + 运行中拒绝续轮；
  6. 对外集成：集成状态、DSH manifest / invoke、飞书 challenge 与消息回调、SSE 首帧。

设计约定：全部用例不触网、不调用真实 LLM（打桩 `_run_llm_retry` / `_consume_stream`），
并把会话目录 / 运行日志目录重定向到临时目录，避免污染 data/。
"""
import json
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

import httpx

from muti_llm_agent import main as main_module
from muti_llm_agent import review, runlog
from muti_llm_agent.config import settings
from muti_llm_agent.logger import log, query_logs
from muti_llm_agent.manager import manager
from muti_llm_agent.orchestrator import Orchestrator
from muti_llm_agent.steering import (
    KIND_LABELS,
    SteeringQueue,
)
from muti_llm_agent.summary import build_summary

HOST_CFG = {"name": "主持", "role": "主持", "personality": "中立",
            "provider": "default", "stance": "HOST"}


def _fake_result(content: str = "回应内容") -> dict:
    """构造 llm 返回结构（与 orchestrator._consume_stream 的契约一致）。"""
    return {
        "content": content,
        "tool_calls": [],
        "assistant_message": None,
        "usage": {"total_tokens": 12, "completion_tokens": 6},
        "metrics": {"ttft_s": 0.2, "gen_s": 0.8, "chars": len(content)},
    }


class _TempDirsMixin:
    """把会话目录 / 运行日志目录重定向到临时目录。"""

    def _isolate_dirs(self):
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)

        p1 = patch("muti_llm_agent.manager.SESSIONS_DIR", root / "sessions")
        p2 = patch("muti_llm_agent.runlog.RUNS_DIR", root / "runs")
        p3 = patch("muti_llm_agent.runlog.INDEX_PATH", root / "runs" / "index.json")
        p4 = patch("muti_llm_agent.review.REVIEWS_DIR", root / "reviews")
        p5 = patch("muti_llm_agent.agent.MEMORY_DIR", root / "mem")
        for p in (p1, p2, p3, p4, p5):
            p.start()
            self.addCleanup(p.stop)
        (root / "runs").mkdir(parents=True, exist_ok=True)
        (root / "reviews").mkdir(parents=True, exist_ok=True)
        (root / "sessions").mkdir(parents=True, exist_ok=True)
        self.addCleanup(self._tmp.cleanup)
        self.root = root


# ============================================================
# 1. 中途指令队列（纯单元）
# ============================================================
class SteeringQueueTest(unittest.TestCase):
    def test_normal_directive_consumed_once(self):
        q = SteeringQueue()
        q.add("把方案收敛到 3 个选项", kind="redirect")
        first = q.take("甲", is_main=False, round_no=1)
        self.assertEqual(len(first), 1)
        self.assertEqual(first[0]["kind"], "redirect")
        self.assertEqual(first[0]["status"], "applied")
        # 一次性指令不应再次注入
        self.assertEqual(q.take("甲", is_main=False, round_no=1), [])

    def test_high_priority_lasts_whole_round(self):
        q = SteeringQueue()
        q.add("硬约束：只用开源方案", kind="constrain", priority="high")
        self.assertEqual(len(q.take("甲", False, 2)), 1)
        self.assertEqual(len(q.take("乙", False, 2)), 1)  # 同轮其它席位也拿到
        self.assertEqual(q.end_round(2)[0]["status"], "applied")
        self.assertEqual(q.take("丙", False, 3), [])  # 下一轮不再注入

    def test_scope_main_only_matches_main_agent(self):
        q = SteeringQueue()
        q.add("主持人请重新拆解任务", kind="correct", scope="main")
        self.assertEqual(q.take("外席", False, 1), [])
        self.assertEqual(len(q.take("主持", True, 1)), 1)

    def test_scope_agent_matches_target(self):
        q = SteeringQueue()
        q.add("研究员补充出处", kind="fact", scope="agent", target="研究员")
        self.assertEqual(q.take("写作", False, 1), [])
        self.assertEqual(len(q.take("研究员", False, 1)), 1)

    def test_agent_scope_without_target_degrades_to_all(self):
        q = SteeringQueue()
        info = q.add("没写目标", kind="correct", scope="agent", target="")
        self.assertEqual(info["scope"], "all")
        self.assertEqual(len(q.take("任意", False, 1)), 1)

    def test_at_round_delays_effect(self):
        q = SteeringQueue()
        q.add("第三轮起换成英文输出", kind="constrain", at_round=3)
        self.assertEqual(q.take("甲", False, 1), [])
        self.assertEqual(q.take("甲", False, 2), [])
        self.assertEqual(len(q.take("甲", False, 3)), 1)

    def test_cancel(self):
        q = SteeringQueue()
        info = q.add("我要撤销", kind="correct")
        self.assertTrue(q.cancel(info["directive_id"]))
        self.assertEqual(q.take("甲", False, 1), [])
        self.assertFalse(q.cancel(info["directive_id"]))  # 已撤销的不能再撤

    def test_empty_text_rejected(self):
        q = SteeringQueue()
        with self.assertRaises(ValueError):
            q.add("   ")

    def test_unknown_kind_falls_back_to_correct(self):
        q = SteeringQueue()
        info = q.add("未知类型", kind="not-a-kind")
        self.assertEqual(info["kind"], "correct")

    def test_render_block(self):
        q = SteeringQueue()
        q.add("别再讨论技术细节", kind="stop")
        text = SteeringQueue.render(q.all())
        self.assertIn("用户中途指令", text)
        self.assertIn(KIND_LABELS["stop"], text)
        self.assertIn("别再讨论技术细节", text)

    def test_render_empty(self):
        self.assertEqual(SteeringQueue.render([]), "")

    def test_stats_counts(self):
        q = SteeringQueue()
        q.add("a", kind="correct")
        q.add("b", kind="fact", priority="high")
        st = q.stats()
        self.assertEqual(st["added"], 2)
        self.assertEqual(st["by_kind"]["correct"], 1)
        self.assertEqual(st["by_kind"]["fact"], 1)
        self.assertEqual(st["pending"], 2)

    def test_text_truncated_to_limit(self):
        q = SteeringQueue(text_max=50)
        info = q.add("x" * 200)
        self.assertLessEqual(len(info["text"]), 51)


# ============================================================
# 2. 编排器：指令注入 / 摘要 / 续轮
# ============================================================
class OrchestratorCollabTest(_TempDirsMixin, unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._isolate_dirs()
        self.orch = Orchestrator(session_id=f"collab_{uuid.uuid4().hex[:8]}")
        self.orch.configure([HOST_CFG,
                             {"name": "研究员", "role": "资料", "personality": "严谨",
                              "provider": "default", "stance": "NEUTRAL"}],
                            topic="多模型协同测试", max_rounds=2, mode="collaboration")

    async def test_directive_reaches_llm_messages(self):
        seen = {}

        async def fake_retry(messages, model_config, tools=None, **kwargs):
            seen["messages"] = messages
            return _fake_result("已按指令调整")

        self.orch.add_directive("停止讨论成本，专注用户价值", kind="correct")
        with patch.object(self.orch, "_run_llm_retry", fake_retry), \
                patch.object(self.orch, "_consume_stream", fake_retry):
            out = await self.orch._agent_turn(self.orch.main_agent, "请开始",
                                              mode="collaboration")
        user_msg = seen["messages"][-1]["content"]
        self.assertIn("用户中途指令", user_msg)
        self.assertIn("专注用户价值", user_msg)
        self.assertEqual(out["content"], "已按指令调整")
        # 指令文本同时进入 transcript，复盘时能看到「谁在第几轮被纠偏」
        self.assertTrue(any(e.get("type") == "directive" for e in self.orch.transcript))
        self.assertEqual(self.orch.steering.stats()["injected"], 1)

    async def test_followup_seed_reaches_first_turn(self):
        seen = {}

        async def fake_retry(messages, model_config, tools=None, **kwargs):
            seen["messages"] = messages
            return _fake_result("承接输出")

        self.orch.followup_seed_active = "## 本轮补充材料\n新数据：转化率 3.2%"
        self.orch.round = 1
        with patch.object(self.orch, "_run_llm_retry", fake_retry), \
                patch.object(self.orch, "_consume_stream", fake_retry):
            await self.orch._agent_turn(self.orch.main_agent, "开始", mode="collaboration")
        self.assertIn("转化率 3.2%", seen["messages"][-1]["content"])

    async def test_summary_shape(self):
        self.orch.spent = 100
        self.orch.output_tokens = 40
        self.orch.add_directive("测试指令", kind="fact")
        s = build_summary(self.orch)
        for key in ("session_id", "trace_id", "mode", "status", "rounds_executed",
                    "agents", "tokens", "cost_rmb", "steering", "highlights", "errors"):
            self.assertIn(key, s)
        self.assertEqual(s["tokens"]["output"], 40)
        self.assertEqual(s["steering"]["added"], 1)
        self.assertEqual(len(s["agents"]), 2)
        self.assertTrue(s["highlights"])
        self.assertEqual(self.orch.summary()["trace_id"], self.orch.trace_id)

    async def test_followup_archives_and_advances_iteration(self):
        # 造一个「已结束且有内容」的运行
        self.orch.status = "done"
        self.orch.round = 2
        self.orch.transcript = [
            {"type": "speech", "round": 1, "speaker": "主持", "is_main": True,
             "text": "第一轮结论 A"},
            {"type": "speech", "round": 2, "speaker": "研究员", "is_main": False,
             "text": "第二轮补充 B"},
        ]
        self.orch.conclusion = {"core": "核心结论 X", "positions": "各方立场 Y",
                                "judge_ruling": "", "pending": "待办 Z"}
        old_trace = self.orch.trace_id
        info = self.orch.start_followup(
            materials=[{"source": "访谈", "text": "用户说价格太贵"}],
            focus="重做定价", max_rounds=3)
        self.assertEqual(info["iteration"], 2)
        self.assertEqual(info["previous_iteration"], 1)
        self.assertEqual(len(self.orch.iterations), 1)
        self.assertEqual(self.orch.iterations[0]["trace_id"], old_trace)
        self.assertNotEqual(self.orch.trace_id, old_trace)  # 新一轮换新 trace
        self.assertIn("用户说价格太贵", self.orch.followup_seed_active)
        self.assertIn("核心结论 X", self.orch.followup_seed_active)
        self.assertIn("重做定价", self.orch.followup_seed_active)
        self.assertEqual(self.orch.max_rounds, 3)
        self.assertEqual(self.orch.status, "idle")

    async def test_followup_rejects_active_run(self):
        self.orch.status = "running"
        self.orch.transcript = [{"type": "speech", "round": 1, "speaker": "主持",
                                 "is_main": True, "text": "x"}]
        with self.assertRaises(ValueError):
            self.orch.start_followup(materials="材料")
        self.orch.status = "idle"

    async def test_followup_rejects_empty_input(self):
        self.orch.status = "done"
        self.orch.transcript = [{"type": "speech", "round": 1, "speaker": "主持",
                                 "is_main": True, "text": "x"}]
        with self.assertRaises(ValueError):
            self.orch.start_followup(materials="", focus="")

    async def test_review_exports_all_formats(self):
        self.orch.status = "done"
        self.orch.transcript = [{"type": "speech", "round": 1, "speaker": "主持",
                                 "is_main": True, "text": "结论"}]
        md = review.render_markdown_review(self.orch)
        self.assertIn("复盘摘要", md)
        self.assertIn("中途注入纠偏记录", md)
        html = review.render_html_review(self.orch)
        self.assertIn("<!DOCTYPE html>", html)
        self.assertIn("复盘报告", html)
        data = review.build_review(self.orch)
        self.assertEqual(data["kind"], "run_review")
        self.assertIn("summary", data)
        path = review.save_review(self.orch, "json")
        self.assertTrue(path.is_file())
        content, media, fname = review.export_content(self.orch, "md")
        self.assertIn("markdown", media)
        self.assertTrue(fname.endswith(".md"))
        self.assertIn("结论", content)

    async def test_runlog_roundtrip(self):
        rid = self.orch.trace_id
        runlog.write_header(rid, session_id=self.orch.session_id, topic="t",
                            mode="collaboration", iteration=1, agents=[])
        self.orch._runlog("speech", speaker="主持", chars=3, text="abc")
        events = runlog.load_events(rid)
        self.assertGreaterEqual(len(events), 2)
        self.assertEqual(events[0]["type"], "header")
        self.assertEqual(runlog.header_of(events).get("topic"), "t")
        rec = runlog.register_run(self.orch.summary())
        self.assertEqual(rec["trace_id"], rid)
        self.assertTrue(any(r["trace_id"] == rid for r in runlog.list_runs()))
        self.assertIsNotNone(runlog.get_run(rid))

    async def test_iterations_survive_restart(self):
        """续轮后进程重启：迭代链应从会话元数据恢复，而不是凭空消失。"""
        sid = self.orch.session_id
        self.orch.status = "done"
        self.orch.round = 2
        self.orch.transcript = [{"type": "speech", "round": 1, "speaker": "主持",
                                 "is_main": True, "text": "结论"}]
        self.orch.start_followup(materials="新材料", focus="聚焦点")  # 内部会 _persist_meta
        self.assertEqual(len(self.orch.iterations), 1)
        # 模拟「进程重启」：清掉内存里的会话，再按同一 session_id 取回
        manager._sessions.pop(sid, None)
        restored = manager.get_or_create(sid)
        self.assertEqual(restored.iteration_no, 2)
        self.assertEqual(len(restored.iterations), 1)
        self.assertEqual(restored.iterations[0]["iteration"], 1)


# ============================================================
# 2b. 模型健康度：Key 配了但模型不可用时，配方要自动避开
# ============================================================
class ModelHealthTest(_TempDirsMixin, unittest.TestCase):
    def setUp(self):
        self._isolate_dirs()
        from muti_llm_agent import models_registry
        self.mr = models_registry
        p = patch.object(models_registry, "_health_path",
                         lambda: self.root / "model_health.json")
        p.start()
        self.addCleanup(p.stop)
        self.mr.clear_failures()

    def tearDown(self):
        self.mr.clear_failures()

    def test_failure_marks_provider_unhealthy_and_recipe_avoids_it(self):
        before = self.mr.available_providers()
        self.assertIn("mimo", before)
        self.mr.record_failure("mimo-v2.5", "mimo", "Unsupported model mimo-v2.5")
        after = self.mr.available_providers()
        self.assertNotIn("mimo", after)
        self.assertTrue(after)
        cfg = self.mr.build_agent_configs("collab-3")
        self.assertNotIn("mimo", cfg["providers_used"])
        self.assertTrue(any("mimo" in n for n in cfg["notes"]))
        # 花名册要把失败原因暴露出来（UI 据此标红）
        roster = self.mr.roster()
        bad = [p for p in roster["providers"] if p["provider"] == "mimo"]
        self.assertTrue(bad)
        self.assertFalse(bad[0]["healthy"])
        self.assertGreaterEqual(bad[0]["failure_count"], 1)
        self.assertIn("Unsupported", bad[0]["last_error"])

    def test_failure_persists_across_reload(self):
        self.mr.record_failure("some-model", "qwen", "boom")
        # 模拟重启：清内存缓存，再从磁盘重载
        self.mr._FAILURES.clear()
        self.mr._FAILURES_LOADED = False
        items = self.mr.runtime_failures()
        self.assertTrue(any(f["model_id"] == "some-model" for f in items))

    def test_clear_failures_restores_availability(self):
        self.mr.record_failure("mimo-v2.5", "mimo", "boom")
        self.mr.clear_failures()
        self.assertIn("mimo", self.mr.available_providers())
        self.assertFalse(self.mr.runtime_failures())


# ============================================================
# 3. HTTP 接口层
# ============================================================
class HttpMultiLlmTest(_TempDirsMixin, unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._isolate_dirs()
        self.sid = f"http_{uuid.uuid4().hex[:8]}"
        self.client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=main_module.app),
            base_url="http://test")
        self.addCleanup(self._cleanup)

    async def _cleanup(self):
        await self.client.aclose()

    # ---- 多模型协同：花名册与配方 ----
    async def test_models_roster(self):
        r = await self.client.get("/api/models")
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertTrue(body["ok"])
        self.assertIn("providers", body)
        self.assertIn("recipes", body)
        self.assertGreaterEqual(len(body["recipes"]), 3)
        # 脱敏：花名册里绝不能出现 api_key
        self.assertNotIn("api_key", json.dumps(body))

    async def test_recipes_and_recipe_detail(self):
        r = await self.client.get("/api/recipes")
        self.assertEqual(r.status_code, 200)
        names = [x["name"] for x in r.json()["items"]]
        self.assertIn("collab-4", names)
        d = await self.client.get("/api/recipes/collab-4")
        self.assertEqual(d.status_code, 200, d.text)
        body = d.json()
        self.assertTrue(body["ok"])
        self.assertEqual(len(body["agents"]), 4)
        self.assertEqual(body["mode"], "collaboration")
        self.assertTrue(body["available_providers"])
        # 配方只描述意图，不得携带任何真实密钥
        for a in body["agents"]:
            self.assertEqual(a.get("api_key", ""), "")
        bad = await self.client.get("/api/recipes/not-exist")
        self.assertEqual(bad.status_code, 404)

    async def test_configure_by_recipe_then_status_and_summary(self):
        cfg = (await self.client.get("/api/recipes/collab-3")).json()
        r = await self.client.post("/api/configure", json={
            "session_id": self.sid, "agents": cfg["agents"],
            "topic": "用三模型写一份竞品分析", "max_rounds": cfg["max_rounds"],
            "mode": cfg["mode"], "judge": cfg.get("judge"),
        })
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["mode"], "collaboration")
        st = (await self.client.get(f"/api/status?session_id={self.sid}")).json()
        self.assertEqual(len(st["agents"]), 3)
        self.assertIn("steering", st)
        self.assertEqual(st["iteration"], 1)
        s = (await self.client.get(f"/api/summary?session_id={self.sid}")).json()
        self.assertTrue(s["ok"])
        self.assertEqual(len(s["summary"]["agents"]), 3)
        self.assertTrue(s["summary"]["models_used"])

    async def test_workspace_endpoint(self):
        r = await self.client.get("/api/workspace")
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertIn("paths", body)
        self.assertIn("sessions", body)

    # ---- 中途纠偏 ----
    async def test_steer_endpoint_flow(self):
        await self.client.post("/api/configure", json={
            "session_id": self.sid,
            "agents": [HOST_CFG, {"name": "研究员", "role": "资料",
                                  "provider": "default", "stance": "NEUTRAL"}],
            "topic": "纠偏接口", "mode": "collaboration"})
        r = await self.client.post("/api/steer", json={
            "session_id": self.sid, "text": "别再讨论实现细节",
            "kind": "stop", "scope": "all"})
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertTrue(body["ok"])
        self.assertEqual(body["directive"]["kind"], "stop")
        did = body["directive"]["directive_id"]
        # 队列里能看到
        lst = (await self.client.get(f"/api/steer?session_id={self.sid}")).json()
        self.assertEqual(len(lst["items"]), 1)
        self.assertEqual(lst["steering"]["pending"], 1)
        # 撤销
        d = await self.client.delete(
            f"/api/steer/{did}?session_id={self.sid}")
        self.assertEqual(d.status_code, 200, d.text)
        self.assertEqual((await self.client.delete(
            f"/api/steer/{did}?session_id={self.sid}")).status_code, 404)

    async def test_steer_rejects_bad_kind(self):
        await self.client.post("/api/configure", json={
            "session_id": self.sid, "agents": [HOST_CFG], "topic": "x"})
        r = await self.client.post("/api/steer", json={
            "session_id": self.sid, "text": "x", "kind": "nonsense"})
        self.assertEqual(r.status_code, 400)

    async def test_steer_rejects_empty_text(self):
        await self.client.post("/api/configure", json={
            "session_id": self.sid, "agents": [HOST_CFG], "topic": "x"})
        r = await self.client.post("/api/steer", json={
            "session_id": self.sid, "text": "   "})
        self.assertEqual(r.status_code, 400)

    # ---- 续轮 ----
    async def test_followup_endpoint(self):
        await self.client.post("/api/configure", json={
            "session_id": self.sid, "agents": [HOST_CFG], "topic": "续轮接口",
            "mode": "collaboration"})
        orch = manager.get(self.sid)
        # 没有内容时拒绝
        r0 = await self.client.post("/api/followup", json={
            "session_id": self.sid, "materials": "材料", "auto_start": False})
        self.assertEqual(r0.status_code, 400)
        # 手工造一个已结束且有内容的运行态
        orch.status = "done"
        orch.round = 2
        orch.transcript = [{"type": "speech", "round": 1, "speaker": "主持",
                            "is_main": True, "text": "上一轮结论"}]
        r = await self.client.post("/api/followup", json={
            "session_id": self.sid, "materials": "补充材料：预算只有 10 万",
            "focus": "压缩范围", "max_rounds": 2, "auto_start": False})
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertEqual(body["iteration"], 2)
        self.assertFalse(body["started"])
        self.assertGreater(body["materials_chars"], 0)
        self.assertIn("预算只有 10 万", orch.followup_seed_active)
        it = (await self.client.get(f"/api/iterations?session_id={self.sid}")).json()
        self.assertEqual(it["current"], 2)
        self.assertEqual(len(it["items"]), 1)

    # ---- 导出 / 复盘 ----
    async def test_export_formats(self):
        await self.client.post("/api/configure", json={
            "session_id": self.sid, "agents": [HOST_CFG], "topic": "导出格式"})
        md = (await self.client.get(f"/api/export?session_id={self.sid}")).json()
        self.assertIn("markdown", md)  # 旧契约保持
        review_md = await self.client.get(
            f"/api/review/export?session_id={self.sid}&format=md")
        self.assertEqual(review_md.status_code, 200)
        self.assertIn("复盘摘要", review_md.text)
        html = await self.client.get(
            f"/api/review/export?session_id={self.sid}&format=html")
        self.assertIn("text/html", html.headers.get("content-type", ""))
        self.assertIn("复盘报告", html.text)
        js = await self.client.get(
            f"/api/review/export?session_id={self.sid}&format=json")
        payload = json.loads(js.text)
        self.assertEqual(payload["kind"], "run_review")
        rv = (await self.client.get(f"/api/review?session_id={self.sid}")).json()
        self.assertTrue(rv["ok"])
        self.assertIn("timeline", rv["review"])
        bad = await self.client.get(
            f"/api/review/export?session_id={self.sid}&format=pdf")
        self.assertEqual(bad.status_code, 400)

    # ---- 可观测性 ----
    async def test_logs_and_trace(self):
        log("info", "test.marker", "结构化日志测试", trace_id="t-abc", k=1)
        r = await self.client.get("/api/logs?limit=5&trace_id=t-abc")
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertTrue(body["ok"])
        self.assertTrue(any(i["event"] == "test.marker" for i in body["items"]))
        self.assertIn("ring_capacity", body)
        t = await self.client.get("/api/trace/t-abc")
        self.assertEqual(t.status_code, 200)
        self.assertIn("logs", t.json())
        self.assertTrue(query_logs(trace_id="t-abc"))

    async def test_timeline_and_runs(self):
        r = await self.client.get("/api/runs")
        self.assertEqual(r.status_code, 200)
        self.assertIn("items", r.json())
        tl = await self.client.get(f"/api/timeline?session_id={self.sid}")
        self.assertEqual(tl.status_code, 200)
        self.assertIn("items", tl.json())

    # ---- 接口级调试 ----
    async def test_dev_endpoints_catalog(self):
        r = await self.client.get("/api/dev/endpoints")
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertTrue(body["ok"])
        self.assertGreaterEqual(body["count"], 30)
        names = [m["name"] for m in body["modules"]]
        for expected in ("setup", "kb", "run", "stream", "export", "dev", "integration"):
            self.assertIn(expected, names)
        paths = {e["path"] for m in body["modules"] for e in m["endpoints"]}
        self.assertIn("/api/start", paths)
        self.assertIn("/api/steer", paths)
        # 请求体示例与 schema 可用（同一路径可能同时有 GET/POST，取 POST 那条）
        steer_eps = [e for m in body["modules"] for e in m["endpoints"]
                     if e["path"] == "/api/steer"]
        post_eps = [e for e in steer_eps if "POST" in (e.get("methods") or [e["method"]])]
        self.assertEqual(len(post_eps), 1)
        self.assertEqual(post_eps[0]["request_model"], "SteerRequest")
        self.assertTrue(post_eps[0]["example_body"])
        self.assertTrue(post_eps[0]["dangerous"])
        # session_id 走请求体而不是查询串，因此 session_scoped 为 False 是正确语义
        schema_props = (post_eps[0]["request_schema"] or {}).get("properties") or {}
        self.assertIn("session_id", schema_props)

    async def test_dev_invoke_status(self):
        r = await self.client.post("/api/dev/invoke", json={
            "method": "GET", "path": "/api/status",
            "query": {"session_id": self.sid}})
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertTrue(body["ok"])
        self.assertEqual(body["status_code"], 200)
        self.assertIn("session_id", body["response"])

    async def test_dev_invoke_rejects_bad_path(self):
        r = await self.client.post("/api/dev/invoke", json={
            "method": "GET", "path": "api/status"})
        self.assertEqual(r.status_code, 400)
        r2 = await self.client.post("/api/dev/invoke", json={
            "method": "GET", "path": "/api/dev/invoke"})
        self.assertEqual(r2.status_code, 400)

    async def test_dev_smoke_all_pass(self):
        r = await self.client.post("/api/dev/smoke")
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertTrue(body["ok"], body)
        self.assertEqual(body["failed"], 0)
        self.assertGreaterEqual(body["total"], 8)

    async def test_dev_modules(self):
        r = await self.client.get("/api/dev/modules")
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.json()["items"])

    # ---- 对外集成 ----
    async def test_integrations_status(self):
        r = await self.client.get("/api/integrations")
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertIn("feishu", body)
        self.assertIn("dsh", body)
        self.assertIn("webhooks", body)
        self.assertIn("stream", body)

    async def test_dsh_manifest(self):
        r = await self.client.get("/api/integrations/dsh/manifest")
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertEqual(body["name"], "muti_llm_agent")
        tools = {t["name"] for t in body["tools"]}
        for t in ("collab_start", "collab_status", "collab_steer",
                  "collab_followup", "collab_summary", "collab_export", "collab_history"):
            self.assertIn(t, tools)
        self.assertEqual(body["transport"]["invoke_path"], "/api/integrations/dsh/invoke")

    async def test_dsh_invoke_status_tool(self):
        r = await self.client.post("/api/integrations/dsh/invoke", json={
            "tool": "collab_status", "arguments": {"session_id": self.sid}})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertTrue(r.json()["ok"])

    async def test_dsh_invoke_unknown_tool(self):
        r = await self.client.post("/api/integrations/dsh/invoke", json={
            "tool": "nope", "arguments": {}})
        self.assertEqual(r.status_code, 400)
        self.assertFalse(r.json()["ok"])

    async def test_dsh_invoke_steer(self):
        await self.client.post("/api/configure", json={
            "session_id": self.sid, "agents": [HOST_CFG], "topic": "dsh 纠偏",
            "mode": "collaboration"})
        r = await self.client.post("/api/integrations/dsh/invoke", json={
            "tool": "collab_steer",
            "arguments": {"session_id": self.sid, "text": "换成英文输出"}})
        self.assertEqual(r.status_code, 200, r.text)
        res = r.json()["result"]
        self.assertEqual(res["directive"]["kind"], "correct")

    async def test_feishu_challenge(self):
        r = await self.client.post("/api/integrations/feishu/webhook", json={
            "type": "url_verification", "challenge": "abc123"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json().get("challenge"), "abc123")

    async def test_feishu_message_without_auto_run(self):
        with patch.object(settings, "FEISHU_AUTO_RUN", False):
            r = await self.client.post("/api/integrations/feishu/webhook", json={
                "header": {"event_type": "im.message.receive_v1"},
                "event": {"message": {"content": json.dumps({"text": "帮我写个方案"})},
                          "sender": {"sender_id": {"open_id": "u1"}}}})
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertEqual(body["code"], 0)
        self.assertIn("帮我写个方案", body["received"]["text"])
        self.assertIn("note", body)

    async def test_feishu_push_without_config(self):
        with patch.object(settings, "FEISHU_WEBHOOK_URL", ""):
            r = await self.client.post("/api/integrations/feishu/push", json={
                "session_id": self.sid})
        self.assertEqual(r.status_code, 400)

    async def test_webhook_dispatch_without_config(self):
        with patch.object(settings, "WEBHOOK_URLS", ""):
            r = await self.client.post(
                f"/api/integrations/webhooks/dispatch?session_id={self.sid}")
        self.assertEqual(r.status_code, 400)

    # ---- SSE ----
    async def test_sse_endpoint_registered_and_framing(self):
        """SSE 的帧格式与路由注册（真正的长连接消费放到 scripts/api_smoke.py，
        因为 httpx 的 ASGITransport 会把无限流式响应缓冲住，在单测里会挂住）。"""
        from muti_llm_agent.api.router_stream import _sse

        frame = _sse({"type": "speech_delta", "delta": "你好"}, "speech_delta")
        self.assertTrue(frame.startswith("event: speech_delta\n"))
        self.assertIn('data: {"type": "speech_delta"', frame)
        self.assertTrue(frame.endswith("\n\n"))
        self.assertEqual(frame.count("\n\n"), 1)  # 单帧边界清晰
        paths = {r.path for r in main_module.app.routes if hasattr(r, "path")}
        self.assertIn("/api/stream", paths)
        # 未配置 AUTH_TOKEN 时应能连上（这里只验证路由存在且不被鉴权误拦）
        r = await self.client.get("/healthz")
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.headers.get("x-trace-id"))


# ============================================================
# 4. 启动脚本 / 应用装配
# ============================================================
class AppAssemblyTest(unittest.TestCase):
    def test_app_has_all_module_routes(self):
        paths = {r.path for r in main_module.app.routes if hasattr(r, "path")}
        for expected in ("/", "/console", "/ws", "/api/stream", "/api/start",
                         "/api/steer", "/api/followup", "/api/summary",
                         "/api/review/export", "/api/dev/endpoints",
                         "/api/integrations/dsh/manifest"):
            self.assertIn(expected, paths)

    def test_openapi_has_tags(self):
        spec = main_module.app.openapi()
        self.assertIn("paths", spec)
        tags = {t["name"] for t in spec.get("tags", [])}
        self.assertIn("运行控制", tags)


if __name__ == "__main__":
    unittest.main()
