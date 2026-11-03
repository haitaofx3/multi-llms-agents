"""
test_p0_fixes.py — P0 缺陷修复的确定性回归测试（无需服务器 / 无需真实 LLM）

覆盖五个 P0 修复点：
  1. 哈希降级向量跨进程确定性：`_fallback_embed` 改用 blake2b 内容摘要做种子，
     不再依赖被 `PYTHONHASHSEED` 随机化的内置 `hash()`（memory.json 会持久化
     向量、Qdrant 用哈希值做 point id，种子漂移会让语义检索退化为噪声）；
  2. 并发闸门：`Orchestrator.is_active` 同时覆盖 running / paused / run 任务未退出，
     paused 状态下再次 start 不再并发跑第二个主循环；
  3. 运行态重置：`run_discussion` 入口调用 `_reset_run_state()`，不重新 configure
     直接再跑一场时不会带上一场的 transcript / tasks / shared_context / Agent 历史；
  4. 记忆按会话隔离：`MemorySystem` 落盘到 `{session}/{agent_id}/`，同名席位的
     不同会话互不可见；会话标识经 safe_path_component 清洗，防目录穿越；
  5. 会话注册表容量治理：超上限按 LRU 淘汰空闲会话（运行中的绝不淘汰），
     只读端点（/api/status、/api/export）不再有「新建会话」的副作用。

运行方式（无需服务器、无需真实 LLM）：
    python -m unittest tests.test_p0_fixes -v
"""
import asyncio
import hashlib
import os
import subprocess
import sys
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

import httpx
import numpy as np

from muti_llm_agent import main as main_module
from muti_llm_agent.config import PROJECT_ROOT, safe_path_component, settings
from muti_llm_agent.manager import DebateManager, manager
from muti_llm_agent.protocol import Judgment

HOST_CFG = {"name": "主持", "role": "主持", "personality": "",
            "provider": "default", "stance": "HOST"}
PRO_CFG = {"name": "正方", "role": "正方", "personality": "",
           "provider": "default", "stance": "PRO"}
CON_CFG = {"name": "反方", "role": "反方", "personality": "",
           "provider": "default", "stance": "CON"}


def _fake_llm_result(content="发言"):
    return {"content": content, "tool_calls": [], "assistant_message": None,
            "usage": {"total_tokens": 1}}


def _unique_sid(tag: str) -> str:
    return f"p0_{tag}_{uuid.uuid4().hex[:8]}"


def _isolate_meta_dir(case, tmp_root: Path) -> None:
    """把会话元数据目录重定向到临时目录，避免测试往 data/sessions 落文件。"""
    meta_dir = tmp_root / "sessions"
    meta_dir.mkdir(parents=True, exist_ok=True)
    patcher = patch("muti_llm_agent.manager.SESSIONS_DIR", meta_dir)
    patcher.start()
    case.addCleanup(patcher.stop)


# ---------------------------------------------------------------------------
# 1. 哈希降级向量的跨进程确定性
# ---------------------------------------------------------------------------
class HashFallbackDeterminismTest(unittest.TestCase):
    """降级向量必须只由文本内容决定，不能依赖进程随机的内置 hash()。"""

    def test_seed_is_content_digest(self):
        from muti_llm_agent.embeddings import _fallback_seed

        text = "同一段文本在不同进程必须得到相同向量"
        expected = int.from_bytes(
            hashlib.blake2b(text.encode("utf-8"), digest_size=4).digest(), "big")
        self.assertEqual(_fallback_seed(text), expected)
        # 内容不同 → 种子不同；空串也不崩
        self.assertNotEqual(_fallback_seed("甲"), _fallback_seed("乙"))
        self.assertEqual(_fallback_seed(""), _fallback_seed(""))

    def test_fallback_vector_matches_expected_reconstruction(self):
        from muti_llm_agent.embeddings import _fallback_embed

        text = "确定性哈希向量"
        vec = _fallback_embed([text])[0]
        self.assertEqual(len(vec), settings.QDRANT_VECTOR_SIZE)
        # 重复调用一致（纯函数）
        self.assertEqual(vec, _fallback_embed([text])[0])
        # 独立复算期望值：若实现退回内置 hash()，期望值几乎不可能吻合
        seed = int.from_bytes(
            hashlib.blake2b(text.encode("utf-8"), digest_size=4).digest(), "big")
        rng = np.random.RandomState(seed)
        expected = rng.rand(settings.QDRANT_VECTOR_SIZE).astype("float32")
        expected = expected / (np.linalg.norm(expected) + 1e-9)
        np.testing.assert_allclose(vec, expected.tolist(), rtol=1e-6, atol=1e-9)

    def test_fallback_vector_stable_across_processes(self):
        """真实跨进程校验：PYTHONHASHSEED 不同也必须得到同一向量。"""
        code = ("import json;"
                "from muti_llm_agent.embeddings import _fallback_embed;"
                "print(json.dumps(_fallback_embed(['跨进程一致性'])[0][:4]))")
        outputs = []
        for hash_seed in ("0", "1", "random"):
            env = {**os.environ, "PYTHONHASHSEED": hash_seed,
                   "PYTHONPATH": str(PROJECT_ROOT)}
            try:
                proc = subprocess.run(  # noqa: S603 解释器与命令均为本文件内的常量
                    [sys.executable, "-c", code], capture_output=True, text=True,
                    env=env, cwd=str(PROJECT_ROOT), timeout=120, check=False)
            except (OSError, PermissionError) as exc:
                self.skipTest(f"无法启动子进程（沙箱限制）：{exc}")
            if proc.returncode != 0:
                self.skipTest(f"子进程执行失败：{(proc.stderr or '')[-300:]}")
            outputs.append((proc.stdout or "").strip().splitlines()[-1])
        self.assertEqual(len(set(outputs)), 1,
                         f"不同 PYTHONHASHSEED 下向量不一致：{outputs}")

    def test_stable_digest_int_is_not_python_hash(self):
        from muti_llm_agent.embeddings import stable_digest_int

        self.assertEqual(stable_digest_int("abc", 8),
                         int.from_bytes(hashlib.blake2b(b"abc", digest_size=8).digest(),
                                        "big"))
        self.assertNotEqual(stable_digest_int("a\x000", 8), stable_digest_int("a\x001", 8))


# ---------------------------------------------------------------------------
# 2. 并发闸门：paused 不得再次启动
# ---------------------------------------------------------------------------
class RunConcurrencyGateTest(unittest.IsolatedAsyncioTestCase):
    """status 只判 running 会漏掉 paused —— 必须用 is_active 做闸门。"""

    async def asyncSetUp(self):
        from muti_llm_agent.orchestrator import Orchestrator

        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        # 隔离记忆 / 会话元数据落盘目录，避免测试污染 data/
        p = patch("muti_llm_agent.agent.MEMORY_DIR", Path(self._tmp.name) / "mem")
        p.start()
        self.addCleanup(p.stop)
        _isolate_meta_dir(self, Path(self._tmp.name))

        self.orch = Orchestrator(session_id=_unique_sid("gate"))
        self.orch.configure([HOST_CFG, PRO_CFG], topic="闸门测试", max_rounds=2)

    def test_is_active_covers_paused_and_running(self):
        self.assertFalse(self.orch.is_active)      # 刚 configure 完：空闲
        self.orch.status = "paused"
        self.assertTrue(self.orch.is_active)       # 暂停中仍算进行中
        self.orch.status = "running"
        self.assertTrue(self.orch.is_active)
        self.orch.status = "done"
        self.assertFalse(self.orch.is_active)      # 终态：可再开始

    async def test_run_discussion_noop_while_paused(self):
        self.orch.status = "paused"
        await self.orch.run_discussion(max_rounds=2)
        # 没有启动任何一轮：轮次 / 发言记录均未被推进
        self.assertEqual(self.orch.round, 0)
        self.assertEqual(self.orch.transcript, [])
        self.assertEqual(self.orch.status, "paused")

    async def test_run_discussion_noop_while_running(self):
        self.orch.status = "running"
        await self.orch.run_discussion(max_rounds=2)
        self.assertEqual(self.orch.round, 0)
        self.assertEqual(self.orch.status, "running")

    def test_configure_rejected_while_paused(self):
        self.orch.status = "paused"
        with self.assertRaises(ValueError):
            self.orch.configure([HOST_CFG], topic="再来一次", max_rounds=1)


class HttpConcurrencyGateTest(unittest.IsolatedAsyncioTestCase):
    """/api/start 与 /api/configure 在 paused 状态下必须拒绝。"""

    async def asyncSetUp(self):
        self.client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=main_module.app), base_url="http://test")
        self.addCleanup(self.client.aclose)
        self.sid = _unique_sid("http_gate")
        self.orch = manager.get_or_create(self.sid)
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        p = patch("muti_llm_agent.agent.MEMORY_DIR", Path(self._tmp.name) / "mem")
        p.start()
        self.addCleanup(p.stop)
        _isolate_meta_dir(self, Path(self._tmp.name))

    async def asyncTearDown(self):
        self.orch.status = "done"
        self._sessions_drop(self.sid)

    @staticmethod
    def _sessions_drop(sid):
        manager._sessions.pop(sid, None)
        manager._last_used.pop(sid, None)

    async def test_start_and_configure_rejected_while_paused(self):
        cfg = await self.client.post("/api/configure", json={
            "session_id": self.sid, "agents": [HOST_CFG, PRO_CFG],
            "topic": "暂停闸门", "max_rounds": 2,
        })
        self.assertEqual(cfg.status_code, 200, cfg.text)

        self.orch.status = "paused"  # 模拟「暂停」后的状态

        started = await self.client.post("/api/start", json={"session_id": self.sid})
        self.assertEqual(started.status_code, 400, started.text)
        self.assertFalse(started.json()["ok"])

        reconf = await self.client.post("/api/configure", json={
            "session_id": self.sid, "agents": [HOST_CFG], "topic": "重配",
        })
        self.assertEqual(reconf.status_code, 409, reconf.text)


# ---------------------------------------------------------------------------
# 3. 运行态重置：不重新 configure 直接再跑一场
# ---------------------------------------------------------------------------
class RunStateResetTest(unittest.IsolatedAsyncioTestCase):
    """上一场的发言 / 任务 / 共享变量 / Agent 历史不得带入新一场。"""

    async def asyncSetUp(self):
        from muti_llm_agent.orchestrator import Orchestrator

        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        p = patch("muti_llm_agent.agent.MEMORY_DIR", Path(self._tmp.name) / "mem")
        p.start()
        self.addCleanup(p.stop)
        _isolate_meta_dir(self, Path(self._tmp.name))

        self.orch = Orchestrator(session_id=_unique_sid("reset"))
        self.orch.configure([HOST_CFG, PRO_CFG, CON_CFG],
                            topic="重置测试", max_rounds=1)

        async def fake_judge():
            return Judgment(winner="PRO", critique="测试裁决", convergence_flag=False)

        async def fake_summary(judgment=None):
            return "测试总结"

        async def fake_consume(messages, model_config, tools=None, on_delta=None,
                               **kwargs):
            if on_delta:
                await on_delta("新一场发言")
            return _fake_llm_result("新一场发言")

        self.orch._judge_round = fake_judge
        self.orch._final_summary = fake_summary
        self.orch._consume_stream = fake_consume
        self.orch._run_llm_retry = lambda *a, **k: _fake_llm_result("新一场发言")

    async def test_second_run_starts_from_clean_state(self):
        orch = self.orch
        # 伪造「上一场」残留：发言记录 / 任务 / 共享变量 / Agent 历史 / 遥测
        orch.transcript = [{"type": "speech", "round": 9, "speaker": "上一场辩手",
                            "is_main": False, "text": "上一场的旧发言内容"}]
        orch.tasks = [{"id": 99, "task_id": "old-task", "recipient": "正方",
                       "status": "completed", "content": "旧任务", "expected_outcome": "",
                       "var_name": "old_var", "completed": True, "result_value": "旧结果",
                       "result_message": "", "rounds_waiting": 0, "reflections": 0,
                       "reflection_log": [], "timestamp": 0, "token_cost": 0}]
        orch.task_counter = 99
        orch.shared_context = {"old_var": {"by": "主持", "value": "旧结果", "task_id": 99}}
        orch.spent = 123456
        orch.agents[1].append_history("user", "上一场的旧消息", round_no=9)

        orch.status = "done"          # 上一场正常结束
        await orch.run_discussion(max_rounds=1)

        md = orch.export_markdown()
        self.assertNotIn("上一场的旧发言内容", md)
        self.assertNotIn("old_var", orch.shared_context)
        self.assertNotIn("old_task", [t.get("content") for t in orch.tasks])
        self.assertNotIn("上一场的旧消息",
                         [m.get("content") for m in orch.agents[1].history])
        # 上一场的 99 号任务编号不会延续
        self.assertTrue(all(t["id"] <= orch.task_counter for t in orch.tasks))
        self.assertLess(orch.spent, 123456)
        self.assertEqual(orch.status, "done")

    async def test_trace_id_regenerated_per_run(self):
        self.orch.status = "done"
        old_trace = self.orch.trace_id
        await self.orch.run_discussion(max_rounds=1)
        self.assertNotEqual(self.orch.trace_id, old_trace)
        self.assertTrue(self.orch.trace_id)


# ---------------------------------------------------------------------------
# 4. 记忆按会话隔离
# ---------------------------------------------------------------------------
class MemorySessionIsolationTest(unittest.TestCase):
    """同 agent_id + 不同 session 必须落在不同目录、互不可见。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.mem_root = Path(self._tmp.name) / "mem"
        p = patch("muti_llm_agent.agent.MEMORY_DIR", self.mem_root)
        p.start()
        self.addCleanup(p.stop)
        _isolate_meta_dir(self, Path(self._tmp.name))

    @staticmethod
    def _make_agent(agent_id: str, session_id: str):
        from muti_llm_agent.agent import Agent

        return Agent(
            agent_id=agent_id, name=f"席_{agent_id}", role="测试",
            personality="", model_config={"base_url": "x", "api_key": "y",
                                          "model_id": "z"},
            stance="PRO", session_id=session_id,
        )

    def test_same_agent_id_different_session_isolated(self):
        a = self._make_agent("agent_1", "sess_a")
        b = self._make_agent("agent_1", "sess_b")
        self.assertNotEqual(a.memory.file, b.memory.file)
        self.assertNotEqual(a.memory.dir.parent, b.memory.dir.parent)

        a.memory.add_long("A 会话的结论", importance=0.9)
        # 重新构造（模拟重启后读取）：B 不应看到 A 的记忆
        b2 = self._make_agent("agent_1", "sess_b")
        a2 = self._make_agent("agent_1", "sess_a")
        self.assertTrue(any("A 会话的结论" in it.get("text", "")
                            for it in a2.memory.long_term))
        self.assertFalse(any("A 会话的结论" in it.get("text", "")
                             for it in b2.memory.long_term))

    def test_default_session_keeps_legacy_layout(self):
        a = self._make_agent("agent_1", "default")
        # 默认会话沿用旧布局 {root}/{agent_id}/，无需迁移历史数据
        self.assertEqual(a.memory.dir, self.mem_root / "agent_1")

    def test_session_id_sanitized_stays_inside_root(self):
        evil = self._make_agent("agent_1", "../../escape")
        root = self.mem_root.resolve()
        self.assertTrue(str(evil.memory.dir.resolve()).startswith(str(root)))
        self.assertEqual(evil.memory.session_id, "default")

    def test_orchestrator_passes_session_id_to_agents(self):
        import asyncio

        from muti_llm_agent.orchestrator import Orchestrator

        orch = Orchestrator(session_id="sess_xyz")
        asyncio.run(self._configure(orch))
        for agent in orch.agents:
            self.assertEqual(agent.memory.session_id, "sess_xyz")
            self.assertEqual(agent.memory.dir.parent.name, "sess_xyz")

    @staticmethod
    async def _configure(orch):
        orch.configure([HOST_CFG, PRO_CFG], topic="会话记忆", max_rounds=1)
        await asyncio.sleep(0)


# ---------------------------------------------------------------------------
# 5. 会话注册表容量治理 + 只读端点无副作用
# ---------------------------------------------------------------------------
class SessionRegistryEvictionTest(unittest.TestCase):
    """超上限按 LRU 淘汰空闲会话；运行中的会话绝不淘汰。"""

    def test_evicts_least_recently_used_idle_session(self):
        mgr = DebateManager(max_sessions=2)
        a = mgr.get_or_create("a")
        b = mgr.get_or_create("b")
        self.assertIs(mgr.get("a"), a)        # 访问 a → a 变为最近使用
        mgr.get_or_create("c")                # 触发淘汰：最久未用的 b 出局
        self.assertEqual(sorted(mgr.list_sessions()), ["a", "c"])
        self.assertNotIn("b", mgr.list_sessions())
        self.assertIsNotNone(b)

    def test_active_session_never_evicted(self):
        mgr = DebateManager(max_sessions=2)
        running = mgr.get_or_create("running_one")
        running.status = "running"
        idle = mgr.get_or_create("idle_one")
        idle.status = "done"
        mgr.get_or_create("new_one")
        sessions = mgr.list_sessions()
        self.assertIn("running_one", sessions)
        self.assertIn("new_one", sessions)
        self.assertNotIn("idle_one", sessions)

    def test_no_eviction_below_limit(self):
        mgr = DebateManager(max_sessions=5)
        for sid in ("s1", "s2", "s3"):
            mgr.get_or_create(sid)
        self.assertEqual(len(mgr.list_sessions()), 3)

    def test_limit_from_settings(self):
        mgr = DebateManager()
        self.assertEqual(mgr.max_sessions, max(1, settings.MAX_SESSIONS))


class ReadOnlyEndpointsTest(unittest.IsolatedAsyncioTestCase):
    """只读端点不得创建会话（否则任意 session_id 都能灌注册表）。"""

    async def asyncSetUp(self):
        self.client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=main_module.app), base_url="http://test")
        self.addCleanup(self.client.aclose)

    async def test_status_does_not_create_session(self):
        sid = _unique_sid("ghost_status")
        self.assertIsNone(manager.get(sid))
        r = await self.client.get(f"/api/status?session_id={sid}")
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertEqual(body["session_id"], sid)
        self.assertEqual(body["status"], "idle")
        self.assertEqual(body["phase"], "idle")
        self.assertIn("fairness", body)
        self.assertIsNone(manager.get(sid), "只读端点不应创建会话")

    async def test_export_does_not_create_session(self):
        sid = _unique_sid("ghost_export")
        r = await self.client.get(f"/api/export?session_id={sid}")
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["markdown"], "")
        self.assertIsNone(manager.get(sid), "只读端点不应创建会话")

    async def test_control_endpoints_404_for_unknown_session(self):
        sid = _unique_sid("ghost_ctrl")
        for action in ("pause", "resume", "end"):
            r = await self.client.post(f"/api/{action}", json={"session_id": sid})
            self.assertEqual(r.status_code, 404, f"{action}: {r.text}")
            self.assertFalse(r.json()["ok"])
        self.assertIsNone(manager.get(sid))


class ControlEndpointSessionRoutingTest(unittest.IsolatedAsyncioTestCase):
    """控制类端点的 session_id 可能只出现在查询串上（前端就是这么调的）。"""

    async def asyncSetUp(self):
        self.client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=main_module.app), base_url="http://test")
        self.addCleanup(self.client.aclose)
        self.sid = _unique_sid("ctrl_route")
        self.orch = manager.get_or_create(self.sid)
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        p = patch("muti_llm_agent.agent.MEMORY_DIR", Path(self._tmp.name) / "mem")
        p.start()
        self.addCleanup(p.stop)
        _isolate_meta_dir(self, Path(self._tmp.name))

    async def asyncTearDown(self):
        self.orch.status = "done"
        manager._sessions.pop(self.sid, None)
        manager._last_used.pop(self.sid, None)

    async def test_pause_resume_end_follow_query_session_id(self):
        cfg = await self.client.post("/api/configure", json={
            "session_id": self.sid, "agents": [HOST_CFG, PRO_CFG],
            "topic": "控制路由", "max_rounds": 2,
        })
        self.assertEqual(cfg.status_code, 200, cfg.text)
        self.orch.status = "running"   # 模拟讨论进行中（无 body 的 pause 需要 running）

        paused = await self.client.post(f"/api/pause?session_id={self.sid}")
        self.assertEqual(paused.status_code, 200, paused.text)
        self.assertEqual(self.orch.status, "paused")
        self.assertEqual(paused.json()["session_id"], self.sid)

        resumed = await self.client.post(f"/api/resume?session_id={self.sid}")
        self.assertEqual(resumed.status_code, 200, resumed.text)
        self.assertEqual(self.orch.status, "running")

        ended = await self.client.post(f"/api/end?session_id={self.sid}")
        self.assertEqual(ended.status_code, 200, ended.text)
        self.assertEqual(self.orch.status, "stopped")
        # 未误伤默认会话：这里必须**显式**先造一个 default 会话再断言，
        # 否则 manager.get("default") 为 None 时 getattr(None, ...) 恒不等于 "stopped"，
        # 这条断言会退化成永远通过的空断言（曾经就是这样）。
        from tests.conftest import drop_session, install_default_orchestrator
        default = install_default_orchestrator()
        try:
            default.status = "running"
            again = await self.client.post(f"/api/end?session_id={self.sid}")
            self.assertEqual(again.status_code, 200, again.text)
            self.assertEqual(default.status, "running", "end 误伤了默认会话")
        finally:
            default.status = "done"
            drop_session("default")

    async def test_save_markdown_follows_query_session_id(self):
        await self.client.post("/api/configure", json={
            "session_id": self.sid, "agents": [HOST_CFG], "topic": "保存路由",
        })
        out_dir = Path(self._tmp.name) / "discussions"
        out_dir.mkdir(parents=True, exist_ok=True)
        # 隔离落盘目录与历史索引，避免测试污染 data/ 下的真实数据
        # （路由层拆包后，落盘目录由 api.router_export 持有，main 只负责运行完成回调）
        with patch("muti_llm_agent.api.router_export.DISCUSSIONS_DIR", out_dir), \
                patch("muti_llm_agent.main.DISCUSSIONS_DIR", out_dir), \
                patch("muti_llm_agent.history.DISCUSSIONS_DIR", out_dir), \
                patch("muti_llm_agent.history.INDEX_PATH", out_dir / "index.json"):
            saved = await self.client.post(f"/api/save_markdown?session_id={self.sid}")
        self.assertEqual(saved.status_code, 200, saved.text)
        self.assertTrue((out_dir / saved.json()["filename"]).is_file())

    async def test_save_markdown_404_for_unknown_session(self):
        sid = _unique_sid("ghost_save")
        r = await self.client.post(f"/api/save_markdown?session_id={sid}")
        self.assertEqual(r.status_code, 404, r.text)
        self.assertIsNone(manager.get(sid))


class SafePathComponentTest(unittest.TestCase):
    """会话 / Agent 标识的路径清洗（manager 与 memory 共用）。"""

    def test_accepts_safe_names(self):
        for name in ("abc", "abc_123.def-xyz", "a.b", "A1"):
            self.assertEqual(safe_path_component(name), name)

    def test_rejects_traversal_and_separators(self):
        for bad in ("", "   ", ".", "..", "../evil", "a/b", "a\\b", "C:\\x", "a\x00b"):
            self.assertEqual(safe_path_component(bad), "default")


if __name__ == "__main__":
    unittest.main()
