"""
test_p1_p2.py — P1 / P2 改造的确定性回归测试（无需服务器 / 无需真实 LLM）

覆盖：
  1. 事件总线背压（`muti_llm_agent/events.py`）：有界队列、丢弃最旧事件、不阻塞发布方、丢弃计数；
  2. 出站地址校验（`muti_llm_agent/netguard.py`）：拒绝回环 / 私网 / 链路本地 / 保留地址、
     拒绝非 http(s) 协议、`CUSTOM_BASE_URL_ALLOW_PRIVATE` 放行开关；
  3. 模型配置层：自定义 provider 的私网 base_url 被拒（防 SSRF）；
  4. 评分纯函数（`muti_llm_agent/scoring.py`）：TTFT/速率分档、纯内容判定胜方、性能分不参与胜负；
  5. 结论纯函数（`muti_llm_agent/conclusion.py`）：四段式补齐 + 裁判段权威重写；
  6. 文本工具（`muti_llm_agent/textutil.py`）：费用估算、文本相似度、论据句抽取、摘要截断；
  7. 可选的令牌鉴权（`AUTH_TOKEN`）：未配置放行、配置后 401 / Bearer 放行 / 静态资源放行；
  8. 上传大小预检（413）与工具循环上限（可配置 + 超限告警）。

运行方式（无需服务器、无需真实 LLM）：
    python -m unittest tests.test_p1_p2 -v
"""
import asyncio
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

import httpx

from muti_llm_agent import events as events_module
from muti_llm_agent import main as main_module
from muti_llm_agent import netguard, textutil
from muti_llm_agent.conclusion import fallback_summary, normalize_conclusion
from muti_llm_agent.config import settings
from muti_llm_agent.events import EventBus
from muti_llm_agent.llm import resolve_model_config
from muti_llm_agent.manager import manager
from muti_llm_agent.protocol import Judgment
from muti_llm_agent.scoring import compute_composite, model_performance, score_rate, score_ttft


# ---------------------------------------------------------------------------
# 1. 事件总线背压
# ---------------------------------------------------------------------------
class EventBusBackpressureTest(unittest.IsolatedAsyncioTestCase):
    """队列满时丢弃最旧事件，绝不阻塞发布方。"""

    async def test_queue_is_bounded_and_drops_oldest(self):
        bus = EventBus(maxsize=3)
        q = bus.subscribe()
        for i in range(6):
            await bus.publish({"type": "seq", "n": i})
        received = [q.get_nowait()["n"] for _ in range(q.qsize())]
        # 只保留最新 3 条，旧事件被丢弃
        self.assertEqual(received, [3, 4, 5])
        self.assertEqual(bus.dropped, 3)
        self.assertEqual(bus.stats()["dropped"], 3)
        self.assertEqual(bus.stats()["maxsize"], 3)

    async def test_publish_does_not_block_without_consumer(self):
        bus = EventBus(maxsize=2)
        bus.subscribe()
        # 100 次发布必须在事件循环里立刻返回（无界队列 + await q.put 的旧实现会堆积）
        await asyncio.wait_for(
            asyncio.gather(*[bus.publish({"type": "x", "n": i}) for i in range(100)]),
            timeout=2)

    async def test_multiple_subscribers_are_independent(self):
        bus = EventBus(maxsize=2)
        q1, q2 = bus.subscribe(), bus.subscribe()
        for i in range(3):
            await bus.publish({"n": i})
        self.assertEqual([q1.get_nowait()["n"] for _ in range(q1.qsize())], [1, 2])
        self.assertEqual([q2.get_nowait()["n"] for _ in range(q2.qsize())], [1, 2])

    async def test_unsubscribe_stops_delivery(self):
        bus = EventBus(maxsize=4)
        q = bus.subscribe()
        bus.unsubscribe(q)
        await bus.publish({"n": 1})
        self.assertEqual(q.qsize(), 0)
        self.assertEqual(bus.stats()["subscribers"], 0)

    async def test_default_capacity_comes_from_constant(self):
        self.assertEqual(events_module.DEFAULT_QUEUE_MAXSIZE, EventBus().maxsize)


# ---------------------------------------------------------------------------
# 2. 出站地址校验（防 SSRF）
# ---------------------------------------------------------------------------
class NetGuardTest(unittest.TestCase):
    """默认拒绝一切指向内网 / 本机 / 保留地址的 base_url。"""

    def setUp(self):
        p = patch.object(settings, "CUSTOM_BASE_URL_ALLOW_PRIVATE", False)
        p.start()
        self.addCleanup(p.stop)

    def test_rejects_loopback_and_private_literals(self):
        for url in ("http://127.0.0.1:8000/v1", "http://10.1.2.3/v1",
                    "http://192.168.1.10/v1", "http://172.16.0.1/v1",
                    "http://169.254.169.254/latest/meta-data",
                    "http://[::1]/v1", "http://0.0.0.0/v1"):
            with self.assertRaises(netguard.UnsafeUrlError, msg=url):
                netguard.check_outbound_url(url)

    def test_rejects_localhost_names(self):
        for url in ("http://localhost:11434/v1", "http://LOCALHOST/v1"):
            with self.assertRaises(netguard.UnsafeUrlError, msg=url):
                netguard.check_outbound_url(url)

    def test_rejects_bad_scheme_and_empty(self):
        for url in ("ftp://example.com/v1", "file:///etc/passwd", "", "   "):
            with self.assertRaises(netguard.UnsafeUrlError, msg=url):
                netguard.check_outbound_url(url)

    def test_allows_public_ip_literal(self):
        # 用公网 IP 字面量，避免测试依赖 DNS
        self.assertEqual(netguard.check_outbound_url("https://8.8.8.8/v1"),
                         "https://8.8.8.8/v1")

    def test_escape_hatch_allows_private(self):
        with patch.object(settings, "CUSTOM_BASE_URL_ALLOW_PRIVATE", True):
            self.assertEqual(netguard.check_outbound_url("http://127.0.0.1:11434/v1"),
                             "http://127.0.0.1:11434/v1")


class ModelConfigSsrfTest(unittest.TestCase):
    """自定义 provider 的 base_url 经 netguard 校验后才落库。"""

    def setUp(self):
        p = patch.object(settings, "CUSTOM_BASE_URL_ALLOW_PRIVATE", False)
        p.start()
        self.addCleanup(p.stop)

    def test_custom_provider_rejects_private_base_url(self):
        with self.assertRaises(netguard.UnsafeUrlError):
            resolve_model_config("custom", "http://127.0.0.1:1234/v1", "sk-x", "m")

    def test_custom_provider_accepts_public_base_url(self):
        cfg = resolve_model_config("custom", "https://8.8.8.8/v1", "sk-x", "m")
        self.assertEqual(cfg["base_url"], "https://8.8.8.8/v1")
        self.assertEqual(cfg["model_id"], "m")


# ---------------------------------------------------------------------------
# 3. 评分 / 结论 / 文本工具纯函数
# ---------------------------------------------------------------------------
class ScoringTest(unittest.TestCase):
    def _judgment(self, pro, con):
        return Judgment(agent_scores={"正方": pro, "反方": con},
                        winner="DRAW", critique="c", convergence_flag=False)

    def test_score_bands(self):
        self.assertEqual(score_ttft(0.5), 10)
        self.assertEqual(score_ttft(3), 8)
        self.assertEqual(score_ttft(31), 2)
        self.assertEqual(score_ttft(None), 5.0)
        self.assertEqual(score_rate(100), 10)
        self.assertEqual(score_rate(5), 2)
        self.assertEqual(score_rate(None), 5.0)

    def test_winner_is_content_only(self):
        """性能分（TTFT / 速率）不得影响胜负：反方性能极高但内容略低，胜方仍是正方。"""
        pro = {"logic": 6, "evidence": 6, "rebuttal": 6, "clarity": 6}
        con = {"logic": 5.5, "evidence": 5.5, "rebuttal": 5.5, "clarity": 5.5}
        j = self._judgment(pro, con)
        stances = {"正方": "PRO", "反方": "CON"}
        perf = {"正方": {"avg_ttft_ms": 9999.0, "avg_gen_rate": 0.1,
                        "performance_score": 1.0},
                "反方": {"avg_ttft_ms": 100.0, "avg_gen_rate": 200.0,
                        "performance_score": 10.0}}
        out = compute_composite(j, ["正方", "反方"], perf, stances.get)
        # 综合分（仅展示）被性能分拉高的是反方……
        self.assertGreater(out.composite_scores["反方"], out.composite_scores["正方"])
        # ……但胜负只由内容四维均分决定
        self.assertEqual(out.winner, "PRO")

    def test_tie_is_draw(self):
        same = {"logic": 8, "evidence": 8, "rebuttal": 8, "clarity": 8}
        j = self._judgment(dict(same), dict(same))
        stances = {"正方": "PRO", "反方": "CON"}
        out = compute_composite(j, ["正方", "反方"], {}, stances.get)
        self.assertEqual(out.winner, "DRAW")

    def test_missing_agent_scores_falls_back_to_draw(self):
        j = Judgment(winner="PRO", critique="", convergence_flag=False)
        out = compute_composite(j, ["正方"], {}, lambda n: "PRO")
        self.assertEqual(out.winner, "DRAW")
        self.assertEqual(out.composite_scores["正方"], 5.0)

    def test_model_performance_aggregates(self):
        perf = model_performance({"A": {"calls": 2, "ttft_sum": 2.0, "gen_sum": 4.0,
                                       "chars": 400}})
        self.assertEqual(perf["A"]["avg_ttft_ms"], 1000.0)
        self.assertEqual(perf["A"]["avg_gen_rate"], 100.0)
        self.assertEqual(perf["A"]["performance_score"], 10.0)


class ConclusionTest(unittest.TestCase):
    def test_fills_missing_sections_and_overwrites_ruling(self):
        judgment = Judgment(agent_scores={"正方": {"logic": 9, "evidence": 9,
                                                   "rebuttal": 9, "clarity": 9}},
                            winner="PRO", critique="评语", convergence_flag=True)
        report, concl = normalize_conclusion("## 核心结论\n\n我方获胜", judgment,
                                             [{"type": "speech", "speaker": "正方",
                                               "round": 1, "text": "论点"}])
        self.assertIn("我方获胜", concl["core"])
        self.assertIn("正反方".replace("正反方", "正方"), concl["positions"])
        # 裁判段落由权威数据重写（模型原文不参与）
        self.assertIn("胜方：PRO", concl["judge_ruling"])
        self.assertIn("## 分歧与待办", report)

    def test_fallback_summary_marks_deterministic(self):
        report, concl = fallback_summary(None, [])
        self.assertIn("系统按发言记录确定性生成", report)
        self.assertEqual(concl["judge_ruling"], "（无裁判裁决）")


class TextUtilTest(unittest.TestCase):
    def test_cost_estimate(self):
        # 100 万输入 + 100 万输出，按默认 ¥2 / ¥8
        cost = textutil.estimate_cost_rmb(2_000_000, 1_000_000)
        self.assertAlmostEqual(cost, settings.COST_INPUT_PER_1M + settings.COST_OUTPUT_PER_1M)

    def test_text_similarity_ignores_whitespace(self):
        self.assertEqual(textutil.text_similarity("你好 世界", "你好世界"), 1.0)
        self.assertEqual(textutil.text_similarity("", "x"), 0.0)

    def test_extract_argument_sentences(self):
        text = "首先，成本很高。其次，风险不可控。今天天气不错。"
        out = textutil.extract_argument_sentences(text, 100)
        self.assertIn("首先", out)
        self.assertIn("其次", out)

    def test_shorten(self):
        self.assertEqual(textutil.shorten("a" * 10, 5), "aaaaa…")
        self.assertEqual(textutil.shorten(None), "")
        self.assertIn('"k"', textutil.shorten({"k": 1}))


# ---------------------------------------------------------------------------
# 4. 令牌鉴权 / 上传预检 / 工具循环上限
# ---------------------------------------------------------------------------
class AuthMiddlewareTest(unittest.IsolatedAsyncioTestCase):
    """未配置 AUTH_TOKEN 放行；配置后 /api/* 必须带令牌。"""

    async def asyncSetUp(self):
        self.client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=main_module.app), base_url="http://test")
        self.addCleanup(self.client.aclose)
        self.sid = f"p12_auth_{uuid.uuid4().hex[:8]}"

    async def test_open_when_no_token_configured(self):
        with patch.object(settings, "AUTH_TOKEN", ""):
            r = await self.client.get(f"/api/status?session_id={self.sid}")
        self.assertEqual(r.status_code, 200, r.text)

    async def test_401_without_token(self):
        with patch.object(settings, "AUTH_TOKEN", "s3cret"):
            r = await self.client.get(f"/api/status?session_id={self.sid}")
        self.assertEqual(r.status_code, 401, r.text)
        self.assertFalse(r.json()["ok"])

    async def test_bearer_and_query_token_accepted(self):
        with patch.object(settings, "AUTH_TOKEN", "s3cret"):
            r1 = await self.client.get(
                f"/api/status?session_id={self.sid}",
                headers={"Authorization": "Bearer s3cret"})
            r2 = await self.client.get(f"/api/status?session_id={self.sid}&token=s3cret")
            r3 = await self.client.get(
                f"/api/status?session_id={self.sid}",
                headers={"Authorization": "Bearer wrong"})
        self.assertEqual(r1.status_code, 200, r1.text)
        self.assertEqual(r2.status_code, 200, r2.text)
        self.assertEqual(r3.status_code, 401, r3.text)

    async def test_static_and_index_stay_public(self):
        with patch.object(settings, "AUTH_TOKEN", "s3cret"):
            index = await self.client.get("/")
            css = await self.client.get("/static/style.css")
        self.assertEqual(index.status_code, 200)
        self.assertEqual(css.status_code, 200)

    async def test_defaults_reports_auth_required_flag(self):
        with patch.object(settings, "AUTH_TOKEN", ""):
            r = await self.client.get("/api/defaults")
        self.assertFalse(r.json()["auth_required"])
        self.assertIn("stream_pool", r.json())


class UploadLimitTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=main_module.app), base_url="http://test")
        self.addCleanup(self.client.aclose)

    async def test_rejects_oversized_upload_with_413(self):
        # 路由层拆包后，上传上限常量由 api.router_kb 持有
        with patch("muti_llm_agent.api.router_kb.MAX_UPLOAD_BYTES", 16):
            files = {"file": ("big.txt", b"x" * 64, "text/plain")}
            r = await self.client.post("/api/upload", files=files)
        self.assertEqual(r.status_code, 413, r.text)
        self.assertIn("MB", r.json()["error"])


class ToolLoopLimitTest(unittest.IsolatedAsyncioTestCase):
    """工具循环上限可配，跑满时记 llm.tool_loop_limit 告警而不是静默截断。"""

    async def asyncSetUp(self):
        from muti_llm_agent.orchestrator import Orchestrator

        self._tmp = __import__("tempfile").TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        mem = patch("muti_llm_agent.agent.MEMORY_DIR", Path(self._tmp.name) / "mem")
        mem.start()
        self.addCleanup(mem.stop)
        meta = patch("muti_llm_agent.manager.SESSIONS_DIR", Path(self._tmp.name) / "sessions")
        meta.start()
        self.addCleanup(meta.stop)

        self.orch = Orchestrator(session_id=f"p12_loop_{uuid.uuid4().hex[:8]}")
        self.orch.configure([{"name": "主持", "role": "主持", "personality": "",
                              "provider": "default", "stance": "HOST"}],
                            topic="工具循环", max_rounds=1)

    async def test_stops_at_configured_limit_and_reports(self):
        calls = {"n": 0}

        async def fake_retry(messages, model_config, tools=None, **kwargs):
            calls["n"] += 1
            return {
                "content": "",
                "tool_calls": [{"id": "t1", "name": "knowledge_search",
                                "arguments": {"query": "x"}}],
                "assistant_message": {"role": "assistant", "content": "",
                                      "tool_calls": [{"id": "t1", "type": "function",
                                                      "function": {"name": "knowledge_search",
                                                                   "arguments": "{}"}}]},
                "usage": {"total_tokens": 1},
            }

        q = self.orch.event_bus.subscribe()
        with patch.object(settings, "TOOL_LOOP_MAX", 2), \
                patch.object(self.orch, "_run_llm_retry", fake_retry), \
                patch.object(self.orch, "_consume_stream", fake_retry):
            out = await self.orch._agent_turn(self.orch.main, "hi", mode="discussion")

        self.assertEqual(calls["n"], 2)  # 严格按配置的上限停止
        self.assertEqual(out["content"], "")
        # _log 通过后台任务发布事件，让出一次事件循环再收
        await asyncio.sleep(0)
        await asyncio.sleep(0.05)
        seen = []
        while not q.empty():
            seen.append(q.get_nowait())
        events = [e.get("event") for e in seen if e.get("type") == "log"]
        self.assertIn("llm.tool_loop_limit", events)
        self.assertTrue(any(e.get("type") == "agent_error" for e in seen))


class MemoryForgetRegressionTest(unittest.TestCase):
    """回归：`MemorySystem.forget()` 曾被同名属性 `self.forget`（dict）覆盖。

    该属性赋值会让实例方法失效 —— `memory.forget()` 实际是在"调用一个 dict"，
    抛 TypeError 后又被编排器 `_memory_housekeeping` 的宽泛 except 静默吞掉，
    导致**遗忘机制长期完全失效**（短期记忆只涨不减）。扩容 mypy 覆盖面后由
    `method-assign` 检查发现，这里用行为测试锁住。
    """

    def test_forget_method_is_callable_and_prunes_expired(self):
        from muti_llm_agent.memory import MemorySystem

        with tempfile.TemporaryDirectory() as td:
            mem = MemorySystem("agent_x", Path(td))
            self.assertTrue(callable(mem.forget), "forget 必须是方法，不能被属性覆盖")
            self.assertIsInstance(mem.forget_config, dict)
            mem.add_short("低重要度记忆", importance=0.01)
            for item in mem.short_term:      # 让有效重要性跌破 min_importance
                item["ts"] = 0
            mem.forget()                     # 修复前此处抛 TypeError
            self.assertEqual(mem.short_term, [])

    def test_snapshot_exposes_forget_config_copy(self):
        from muti_llm_agent.memory import MemorySystem

        with tempfile.TemporaryDirectory() as td:
            mem = MemorySystem("agent_y", Path(td))
            snap = mem.snapshot()
            self.assertIn("forget_config", snap)
            snap["forget_config"]["min_importance"] = 999  # 拷贝，不回写实例
            self.assertNotEqual(mem.forget_config["min_importance"], 999)


class StatusObservabilityTest(unittest.IsolatedAsyncioTestCase):
    """状态接口暴露事件总线背压指标。"""

    async def asyncSetUp(self):
        self.client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=main_module.app), base_url="http://test")
        self.addCleanup(self.client.aclose)
        self.sid = f"p12_obs_{uuid.uuid4().hex[:8]}"
        self._tmp = __import__("tempfile").TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        meta = patch("muti_llm_agent.manager.SESSIONS_DIR", Path(self._tmp.name) / "sessions")
        meta.start()
        self.addCleanup(meta.stop)
        self.orch = manager.get_or_create(self.sid)

    async def asyncTearDown(self):
        self.orch.status = "done"
        manager._sessions.pop(self.sid, None)
        manager._last_used.pop(self.sid, None)

    async def test_status_exposes_eventbus_stats(self):
        r = await self.client.get(f"/api/status?session_id={self.sid}")
        self.assertEqual(r.status_code, 200, r.text)
        stats = r.json()["eventbus"]
        self.assertEqual(stats["dropped"], 0)
        self.assertGreaterEqual(stats["maxsize"], 1)


if __name__ == "__main__":
    unittest.main()
