"""
test_multi_session.py — 多会话隔离（P1-7）与 HTTP 级会话绑定测试（无需真实 LLM）

覆盖完整多会话重构：
  1. DebateManager 注册表：get_or_create 同会话复用 / 异会话隔离 / 空值回退默认；
  2. register() 把既有 Orchestrator 绑定到其 session_id（模块级单例向后兼容）；
  3. 元数据持久化：save_meta / load_meta 往返，不安全 session_id 清洗为 default；
  4. 中断恢复：mark_interrupted 只标记上一进程遗留的 running/paused 会话，
     已结束（done）会话与损坏文件不受影响；
  5. HTTP 层隔离：A 会话完整跑完讨论，B 会话保持 idle，导出 Markdown 互不串扰。

运行方式（无需服务器、无需真实 LLM）：
    python -m unittest tests.test_multi_session -v
"""
import asyncio
import json
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

import httpx

from app import main as main_module
from app.manager import DebateManager
from app.protocol import Judgment

HOST_CFG = {"name": "主持", "role": "主持", "personality": "",
            "provider": "default", "stance": "HOST"}
PRO_CFG = {"name": "正方", "role": "正方", "personality": "",
           "provider": "default", "stance": "PRO"}
CON_CFG = {"name": "反方", "role": "反方", "personality": "",
           "provider": "default", "stance": "CON"}


def _fake_llm_result(content="发言"):
    return {"content": content, "tool_calls": [], "assistant_message": None,
            "usage": {"total_tokens": 1}}


class _StubOrch:
    """仅携带 _snapshot 所需属性的轻量桩对象（避免真实 Orchestrator 的副作用）。"""

    def __init__(self, sid="s1"):
        self.session_id = sid
        self.status = "running"
        self.phase = "debating"
        self.topic = "主题"
        self.trace_id = "trace123"
        self.round = 2
        self.max_rounds = 8
        self.spent = 100
        self.agents = []
        self.judge = None


class DebateManagerTest(unittest.IsolatedAsyncioTestCase):
    """DebateManager 注册表 / 元数据 / 中断恢复的确定性单元测试。"""

    # ---------------- 会话获取与隔离 ----------------

    def test_get_or_create_reuses_same_session(self):
        mgr = DebateManager()
        self.assertIs(mgr.get_or_create("sess_a"), mgr.get_or_create("sess_a"))

    def test_get_or_create_isolates_different_sessions(self):
        mgr = DebateManager()
        self.assertIsNot(mgr.get_or_create("sess_a"), mgr.get_or_create("sess_b"))

    def test_empty_or_none_session_falls_back_to_default(self):
        mgr = DebateManager()
        default = mgr.get_or_create("default")
        self.assertIs(mgr.get_or_create(None), default)
        self.assertIs(mgr.get_or_create(""), default)
        self.assertIs(mgr.get_or_create("  "), default)

    def test_get_returns_none_for_unknown(self):
        mgr = DebateManager()
        self.assertIsNone(mgr.get("not_created"))

    def test_register_binds_existing_orchestrator(self):
        from app.orchestrator import Orchestrator
        mgr = DebateManager()
        orch = Orchestrator(session_id="custom_sid")
        mgr.register(orch)
        self.assertIs(mgr.get_or_create("custom_sid"), orch)
        self.assertIs(mgr.get("custom_sid"), orch)

    async def test_configuring_one_session_leaves_other_idle(self):
        """会话隔离：配置 A 不影响 B 的任意状态。"""
        mgr = DebateManager()
        a = mgr.get_or_create("sess_a")
        b = mgr.get_or_create("sess_b")
        a.configure([HOST_CFG, PRO_CFG], topic="会话A", max_rounds=3)
        self.assertEqual(a.topic, "会话A")
        self.assertEqual(len(a.agents), 2)
        # B 保持完全空白
        self.assertEqual(b.topic, "")
        self.assertEqual(b.agents, [])
        self.assertEqual(b.status, "idle")

    # ---------------- 元数据持久化 ----------------

    def test_meta_path_sanitizes_unsafe_ids(self):
        from app.manager import _SAFE_SID
        self.assertTrue(_SAFE_SID.match("abc_123.def-xyz"))
        self.assertFalse(_SAFE_SID.match("../evil"))
        self.assertFalse(_SAFE_SID.match("a/b"))
        self.assertFalse(_SAFE_SID.match("a\\b"))
        self.assertFalse(_SAFE_SID.match(""))

    def test_save_and_load_meta_roundtrip(self):
        with tempfile.TemporaryDirectory() as td:
            with patch("app.manager.SESSIONS_DIR", Path(td)):
                mgr = DebateManager()
                snap = mgr.save_meta(_StubOrch("s1"))
                self.assertEqual(snap["session_id"], "s1")
                self.assertEqual(snap["topic"], "主题")
                self.assertEqual(snap["status"], "running")
                self.assertEqual(snap["spent_tokens"], 100)

                meta = mgr.load_meta("s1")
                self.assertIsNotNone(meta)
                self.assertEqual(meta["topic"], "主题")
                self.assertEqual(meta["phase"], "debating")
                self.assertEqual(meta["round"], 2)
                self.assertIsNone(mgr.load_meta("nonexistent"))

    def test_unsafe_session_id_meta_written_to_default(self):
        with tempfile.TemporaryDirectory() as td:
            with patch("app.manager.SESSIONS_DIR", Path(td)):
                mgr = DebateManager()
                snap = mgr.save_meta(_StubOrch("../evil"))
                self.assertEqual(snap["session_id"], "../evil")
                # 磁盘文件名被清洗为 default.json，而非路径穿越
                written = list(Path(td).glob("*.json"))
                self.assertEqual([p.name for p in written], ["default.json"])
                self.assertIsNotNone(mgr.load_meta("default"))
                # 读回时同样会被清洗到 default.json，且快照保留原始 SID
                loaded = mgr.load_meta("../evil")
                self.assertIsNotNone(loaded)
                self.assertEqual(loaded["session_id"], "../evil")

    # ---------------- 中断恢复 ----------------

    def test_mark_interrupted_only_marks_running_or_paused(self):
        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            (d / "run.json").write_text(
                json.dumps({"session_id": "run", "status": "running",
                            "phase": "debating"}),
                encoding="utf-8")
            (d / "pause.json").write_text(
                json.dumps({"session_id": "pause", "status": "paused",
                            "phase": "debating"}),
                encoding="utf-8")
            (d / "done.json").write_text(
                json.dumps({"session_id": "done", "status": "done",
                            "phase": "finished"}),
                encoding="utf-8")
            (d / "bad.json").write_text("not json", encoding="utf-8")
            with patch("app.manager.SESSIONS_DIR", d):
                mgr = DebateManager()
                marked = mgr.mark_interrupted()
                names = sorted(m["session_id"] for m in marked)
                self.assertEqual(names, ["pause", "run"])
                for m in marked:
                    self.assertEqual(m["status"], "interrupted")
                    self.assertEqual(m["phase"], "idle")
                    self.assertEqual(m["note"], "服务重启，讨论中断")
                # done 会话不被误标记
                done = json.loads((d / "done.json").read_text(encoding="utf-8"))
                self.assertEqual(done["status"], "done")
                # 损坏文件被跳过
                self.assertTrue((d / "bad.json").is_file())

    def test_interrupted_metas_and_lookup(self):
        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            (d / "x.json").write_text(
                json.dumps({"session_id": "x", "status": "interrupted",
                            "phase": "idle", "topic": "中断会话"}),
                encoding="utf-8")
            with patch("app.manager.SESSIONS_DIR", d):
                mgr = DebateManager()
                self.assertEqual([m["session_id"] for m in mgr.interrupted_metas()],
                                 ["x"])
                meta = mgr.interrupted_meta_for("x")
                self.assertEqual(meta["topic"], "中断会话")
                self.assertIsNone(mgr.interrupted_meta_for("unknown"))

    # ---------------- 完成回调 ----------------

    def test_notify_done_invokes_handler(self):
        mgr = DebateManager()
        called = []

        def handler(orch):
            called.append(orch)
        mgr.set_done_handler(handler)
        orch = _StubOrch("s1")
        mgr.notify_done(orch)
        self.assertEqual(called, [orch])


class HttpSessionIsolationTest(unittest.IsolatedAsyncioTestCase):
    """HTTP 层会话隔离：独立 session_id 绑定独立 Orchestrator，互不串扰。"""

    async def asyncSetUp(self):
        self.client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=main_module.app),
            base_url="http://test",
        )
        self.mgr = main_module.manager
        self.sid_a = f"sess_a_{uuid.uuid4().hex[:6]}"
        self.sid_b = f"sess_b_{uuid.uuid4().hex[:6]}"
        orch_a = self.mgr.get_or_create(self.sid_a)
        orch_b = self.mgr.get_or_create(self.sid_b)

        async def fake_judge():
            return Judgment(winner="PRO", critique="测试裁决", convergence_flag=False)

        async def fake_summary(judgment=None):
            return "测试总结"

        async def fake_consume(messages, model_config, tools=None, on_delta=None, **kwargs):
            if on_delta:
                await on_delta("发言")
            return _fake_llm_result()

        for orch in (orch_a, orch_b):
            orch._judge_round = fake_judge
            orch._final_summary = fake_summary
            orch._consume_stream = fake_consume
            orch._run_llm_retry = lambda *a, **k: _fake_llm_result()

    async def asyncTearDown(self):
        for sid in (self.sid_a, self.sid_b):
            orch = self.mgr.get(sid)
            if orch is not None:
                orch.end()
        await self.client.aclose()

    async def test_sessions_isolated_over_http(self):
        """A 完整跑完 → done；B 保持 idle；导出内容互不串扰。"""
        cfg_a = await self.client.post("/api/configure", json={
            "session_id": self.sid_a,
            "agents": [HOST_CFG, PRO_CFG, CON_CFG],
            "topic": "A会话主题", "max_rounds": 1,
        })
        self.assertEqual(cfg_a.status_code, 200, cfg_a.text)
        self.assertEqual(cfg_a.json()["session_id"], self.sid_a)

        cfg_b = await self.client.post("/api/configure", json={
            "session_id": self.sid_b,
            "agents": [HOST_CFG, PRO_CFG],
            "topic": "B会话主题", "max_rounds": 1,
        })
        self.assertEqual(cfg_b.status_code, 200, cfg_b.text)
        self.assertEqual(cfg_b.json()["session_id"], self.sid_b)

        st = await self.client.post(
            "/api/start", json={"session_id": self.sid_a, "max_rounds": 1})
        self.assertEqual(st.status_code, 200, st.text)
        self.assertEqual(st.json()["session_id"], self.sid_a)

        # 轮询 A 直到 done
        for _ in range(150):
            await asyncio.sleep(0.1)
            body = (await self.client.get(f"/api/status?session_id={self.sid_a}")).json()
            if body["status"] == "done":
                break
        else:
            self.fail("A 会话讨论未在时限内结束")
        self.assertEqual(body["session_id"], self.sid_a)

        # B 不受 A 运行影响：仍为 idle，且绑定自己的会话
        sb = (await self.client.get(f"/api/status?session_id={self.sid_b}")).json()
        self.assertEqual(sb["session_id"], self.sid_b)
        self.assertEqual(sb["status"], "idle")
        self.assertEqual(sb["phase"], "idle")

        # 导出隔离：A 的 markdown 含 A 主题且不含 B 主题；B 相反
        ex_a = (await self.client.get(f"/api/export?session_id={self.sid_a}")).json()["markdown"]
        ex_b = (await self.client.get(f"/api/export?session_id={self.sid_b}")).json()["markdown"]
        self.assertIn("A会话主题", ex_a)
        self.assertNotIn("B会话主题", ex_a)
        self.assertIn("B会话主题", ex_b)
        self.assertNotIn("A会话主题", ex_b)

    async def test_status_reflects_own_session_topic(self):
        """configure 后 status 各自携带自己的主题，互不覆盖。"""
        await self.client.post("/api/configure", json={
            "session_id": self.sid_a,
            "agents": [HOST_CFG, PRO_CFG], "topic": "甲主题", "max_rounds": 2,
        })
        await self.client.post("/api/configure", json={
            "session_id": self.sid_b,
            "agents": [HOST_CFG, PRO_CFG], "topic": "乙主题", "max_rounds": 2,
        })
        sa = (await self.client.get(f"/api/status?session_id={self.sid_a}")).json()
        sb = (await self.client.get(f"/api/status?session_id={self.sid_b}")).json()
        self.assertEqual(sa["topic"], "甲主题")
        self.assertEqual(sb["topic"], "乙主题")


if __name__ == "__main__":
    unittest.main()
