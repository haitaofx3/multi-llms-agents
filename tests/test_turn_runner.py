"""
test_turn_runner.py — 直接驱动 turn.run_agent_turn 的单元测试

这些用例刻意**不经过 Orchestrator**：只构造一个满足最小接口的假 Agent，
用几个桩函数替代事件推送 / 日志 / LLM 调用。这正是把「LLM 往返 + 工具循环」
从编排器里抽出来换来的收益——验证这一层不再需要起整个编排器、不需要事件循环
之外的任何基础设施。

覆盖的不变式（与 turn.py 模块 docstring 一一对应）：
  1. speech_start 只在首次产出时推一次；
  2. 普通工具执行完必须把结果以 role=tool 回灌上下文，然后继续下一轮；
  3. delegate / report / claim 是终态动作：处理完本条消息里的整批调用后结束本回合，
     不再发起下一轮 LLM 调用；
  4. 流式失败要回退非流式，并留下 warning 日志；
  5. 工具循环跑满要发 agent_error 并留日志，而不是静默收尾；
  6. LLM 抛错要记运行期异常 + 模型健康度 + 前端错误事件，且不抛出。

运行方式：
    python -m pytest tests/test_turn_runner.py -q
    python -m unittest tests.test_turn_runner -v
"""
import unittest

from muti_llm_agent.turn import TurnOutcome, run_agent_turn


class _StubMCP:
    """假的工具注册表：只记录被调用了什么，返回可预测的字符串。"""

    def __init__(self):
        self.calls = []

    def call(self, name, arguments):
        self.calls.append((name, arguments))
        return f"tool-result:{name}"


class _StubAgent:
    """满足 run_agent_turn 所需最小接口的假 Agent。"""

    def __init__(self, name="发言人", is_main=True):
        self.name = name
        self.is_main = is_main
        self.model_config = {"model_id": "stub-model", "provider": "stub"}
        self.mcp = _StubMCP()


def _result(content="", tool_calls=None, usage=None, **extra):
    """构造一份与 llm.chat 返回值同形的结果。"""
    out = {
        "content": content,
        "tool_calls": tool_calls or [],
        "assistant_message": {"role": "assistant", "content": content} if tool_calls else None,
        "usage": usage if usage is not None else {"total_tokens": 7, "completion_tokens": 3},
    }
    out.update(extra)
    return out


class _Harness:
    """收集桩调用，便于逐项断言。"""

    def __init__(self):
        self.events = []
        self.logs = []
        self.errors = []
        self.failures = []
        self.usage_records = []

    async def publish(self, event):
        self.events.append(event)

    def log(self, message, level, event):
        self.logs.append((level, event, message))

    def record_error(self, where, exc):
        self.errors.append((where, exc))

    def record_model_failure(self, agent, exc):
        self.failures.append((agent.name, str(exc)))

    def on_llm_result(self, agent, result, metrics):
        self.usage_records.append((agent.name, result, metrics))
        return int((result.get("usage") or {}).get("total_tokens") or 0)

    def event_types(self):
        return [e.get("type") for e in self.events]

    def logs_with(self, event):
        return [x for x in self.logs if x[1] == event]


class AgentTurnRunnerTest(unittest.IsolatedAsyncioTestCase):
    async def _run(self, harness, *, agent=None, stream=False, max_loops=3,
                   stream_call=None, sync_call=None, messages=None):
        agent = agent or _StubAgent()

        async def _unexpected_stream(*a, **kw):  # pragma: no cover - 用例不该走这里
            raise AssertionError("未预期走流式分支")

        return await run_agent_turn(
            agent=agent,
            messages=messages if messages is not None else [{"role": "user", "content": "hi"}],
            tools=[],
            round_no=2,
            stream=stream,
            temperature=0.7,
            top_p=None,
            max_loops=max_loops,
            publish=harness.publish,
            log=harness.log,
            record_error=harness.record_error,
            record_model_failure=harness.record_model_failure,
            on_llm_result=harness.on_llm_result,
            stream_call=stream_call or _unexpected_stream,
            sync_call=sync_call,
        )

    async def test_normal_tool_then_final_content(self):
        """普通工具执行完回灌上下文，再下一轮拿到正文即结束。"""
        harness = _Harness()
        agent = _StubAgent()
        messages = [{"role": "user", "content": "hi"}]
        script = [
            _result(tool_calls=[{"id": "t1", "name": "knowledge_search",
                                 "arguments": {"query": "x"}}]),
            _result(content="最终结论"),
        ]

        async def sync_call(msgs, cfg, tools, temperature=0.7, top_p=None):
            return script.pop(0)

        outcome = await self._run(harness, agent=agent, sync_call=sync_call, messages=messages)

        self.assertIsInstance(outcome, TurnOutcome)
        self.assertEqual(outcome.content, "最终结论")
        self.assertEqual(outcome.delegate, [])
        self.assertIsNone(outcome.report)
        # 工具真的被执行了，且结果以 role=tool 回灌进上下文
        self.assertEqual(agent.mcp.calls, [("knowledge_search", {"query": "x"})])
        tool_msgs = [m for m in messages if m.get("role") == "tool"]
        self.assertEqual(len(tool_msgs), 1)
        self.assertEqual(tool_msgs[0]["tool_call_id"], "t1")
        self.assertIn("tool-result:knowledge_search", tool_msgs[0]["content"])
        # 两次调用各自记账，token 累加
        self.assertEqual(len(harness.usage_records), 2)
        self.assertEqual(outcome.token_cost, 14)

    async def test_speech_start_emitted_once_per_turn(self):
        """多轮工具调用下，speech_start 仍只推一次（气泡不重复）。"""
        harness = _Harness()
        agent = _StubAgent()
        script = [
            _result(tool_calls=[{"id": "t1", "name": "knowledge_search", "arguments": {}}]),
            _result(tool_calls=[{"id": "t2", "name": "knowledge_search", "arguments": {}}]),
            _result(content="done"),
        ]

        async def sync_call(msgs, cfg, tools, temperature=0.7, top_p=None):
            return script.pop(0)

        await self._run(harness, agent=agent, sync_call=sync_call)
        self.assertEqual(harness.event_types().count("speech_start"), 1)
        # 每次工具执行都有 start / end 配对
        self.assertEqual(harness.event_types().count("tool_call_start"), 2)
        self.assertEqual(harness.event_types().count("tool_call_end"), 2)

    async def test_delegate_is_terminal_and_stops_further_rounds(self):
        """delegate_task 让本回合结束，不再发起下一轮 LLM 调用。

        语义边界（容易误解）：同一条 assistant 消息里的**所有**工具调用都会被处理，
        跑完这一批才 break —— 所以紧跟在终态动作后面的普通工具仍会执行，
        真正的「终态」体现在「不再开新回合」，而不是「后面的调用被跳过」。
        """
        harness = _Harness()
        agent = _StubAgent(is_main=True)
        llm_calls = []

        async def sync_call(msgs, cfg, tools, temperature=0.7, top_p=None):
            llm_calls.append(1)
            return _result(tool_calls=[
                {"id": "t1", "name": "delegate_task",
                 "arguments": {"recipient": "研究员", "content": "查资料"}},
                {"id": "t2", "name": "knowledge_search", "arguments": {}},
            ])

        outcome = await self._run(harness, agent=agent, sync_call=sync_call)

        self.assertEqual(len(outcome.delegate), 1)
        self.assertEqual(outcome.delegate[0]["recipient"], "研究员")
        self.assertEqual(len(llm_calls), 1, "终态动作之后不应再发起下一轮 LLM 调用")
        self.assertEqual(agent.mcp.calls, [("knowledge_search", {})])
        self.assertEqual(harness.event_types().count("speech_start"), 1)

    async def test_report_and_claim_are_collected(self):
        """非主位席位的 report_result / claim_task 分别落到 report 与 claim。"""
        harness = _Harness()
        agent = _StubAgent(is_main=False)

        async def sync_call(msgs, cfg, tools, temperature=0.7, top_p=None):
            return _result(tool_calls=[
                {"id": "t1", "name": "claim_task", "arguments": {"var_name": "v"}},
                {"id": "t2", "name": "report_result", "arguments": {"var_name": "v",
                                                                    "var_value": "42"}},
            ])

        outcome = await self._run(harness, agent=agent, sync_call=sync_call)

        self.assertEqual(len(outcome.claim), 1)
        self.assertEqual(outcome.report["var_value"], "42")
        self.assertEqual(agent.mcp.calls, [])

    async def test_stream_failure_falls_back_to_sync(self):
        """流式抛错要回退非流式，并留下 llm.stream_fallback 警告。"""
        harness = _Harness()
        agent = _StubAgent()

        async def stream_call(*a, **kw):
            raise RuntimeError("stream boom")

        async def sync_call(msgs, cfg, tools, temperature=0.7, top_p=None):
            return _result(content="回退成功")

        outcome = await self._run(harness, agent=agent, stream=True,
                                  stream_call=stream_call, sync_call=sync_call)

        self.assertEqual(outcome.content, "回退成功")
        warns = harness.logs_with("llm.stream_fallback")
        self.assertEqual(len(warns), 1)
        self.assertEqual(warns[0][0], "warning")
        self.assertIn("stream boom", warns[0][2])
        self.assertEqual(harness.errors, [])

    async def test_loop_limit_emits_agent_error(self):
        """工具循环跑满：发 agent_error + 警告日志，并按已有内容收尾。"""
        harness = _Harness()
        agent = _StubAgent()

        async def sync_call(msgs, cfg, tools, temperature=0.7, top_p=None):
            return _result(content="尚未定性", tool_calls=[
                {"id": "t", "name": "knowledge_search", "arguments": {}}])

        outcome = await self._run(harness, agent=agent, sync_call=sync_call, max_loops=2)

        self.assertEqual(outcome.content, "尚未定性")
        self.assertEqual(len(agent.mcp.calls), 2, "应当恰好跑满 2 轮")
        errs = [e for e in harness.events if e.get("type") == "agent_error"]
        self.assertEqual(len(errs), 1)
        self.assertIn("上限", errs[0]["error"])
        self.assertEqual(len(harness.logs_with("llm.tool_loop_limit")), 1)

    async def test_llm_error_is_recorded_not_raised(self):
        """LLM 抛错：记运行期异常 + 模型健康度 + 前端错误事件，且不向上抛。"""
        harness = _Harness()
        agent = _StubAgent(name="反方")

        async def sync_call(msgs, cfg, tools, temperature=0.7, top_p=None):
            raise RuntimeError("模型 502")

        outcome = await self._run(harness, agent=agent, sync_call=sync_call)

        self.assertEqual(outcome.content, "")
        self.assertEqual(len(harness.errors), 1)
        self.assertEqual(harness.errors[0][0], "llm:反方")
        self.assertEqual(len(harness.failures), 1)
        self.assertEqual(harness.failures[0][0], "反方")
        errs = [e for e in harness.events if e.get("type") == "agent_error"]
        self.assertEqual(len(errs), 1)
        self.assertEqual(errs[0]["model_id"], "stub-model")
        self.assertIn("502", errs[0]["error"])

    async def test_unknown_tool_call_is_executed_not_terminal(self):
        """未知工具名走普通工具分支（交给 MCP 自己决定），不会被当成终态动作。"""
        harness = _Harness()
        agent = _StubAgent(is_main=True)
        script = [
            _result(tool_calls=[{"id": "t1", "name": "delegate_task_typo", "arguments": {}}]),
            _result(content="继续完成"),
        ]

        async def sync_call(msgs, cfg, tools, temperature=0.7, top_p=None):
            return script.pop(0)

        outcome = await self._run(harness, agent=agent, sync_call=sync_call)

        self.assertEqual(outcome.content, "继续完成")
        self.assertEqual(outcome.delegate, [])
        self.assertEqual(agent.mcp.calls, [("delegate_task_typo", {})])


if __name__ == "__main__":
    unittest.main()
