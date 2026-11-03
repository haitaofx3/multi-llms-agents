"""
tests/conftest.py — 测试会话级隔离

测试会在内存里跑真实的编排循环，因此会顺带写出「运行事件日志」「模型健康度」
这类运行期产物。若不隔离，`data/runs/` 会被 200+ 个测试用例堆出上千个文件，
真实的运行记录淹没在里面 ——「打开复盘列表全是测试数据」。

这里在 pytest 会话开始时把运行日志目录重定向到临时目录，并关掉 logger 的文件出口
（控制台日志保留，方便看用例输出）。

注意：`tests/test_multi_llm_collab.py` 里对 RUNS_DIR / REVIEWS_DIR 的 per-test patch
依然有效 —— 它作用在本 fixture 之后，属于更细粒度的隔离。
"""
from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from muti_llm_agent import logger, runlog
from muti_llm_agent.config import settings

# test_e2e.py 是「手动跑的端到端脚本」，不是 pytest 用例：没有任何 test_ 函数，
# 却会在导入时连真实服务器、`import websockets`，并改写 os.environ / sys.stdout。
# 因为文件名匹配 test_*.py，pytest 会去导入它——一旦导入失败（典型情况：CI 没装
# websockets），pytest 以 "Interrupted: 1 error during collection" 中断**整个**
# 测试会话，而不是只跳过这一个文件。所以这里显式排除，让它彻底不参与采集。
collect_ignore = ["test_e2e.py"]


@pytest.fixture(scope="session", autouse=True)
def _isolate_runtime_artifacts():
    tmp = tempfile.TemporaryDirectory(prefix="muti_llm_agent_tests_")
    root = Path(tmp.name)
    runs_dir = root / "runs"
    runs_dir.mkdir(parents=True, exist_ok=True)

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(runlog, "RUNS_DIR", runs_dir)
        mp.setattr(runlog, "INDEX_PATH", runs_dir / "index.json")
        mp.setattr(settings, "LOG_TO_FILE", False)
        mp.setattr(settings, "FEISHU_AUTO_RUN", False)
        # logger 会在首次写日志时缓存「是否落盘」，所以要连同缓存一起关掉
        mp.setitem(logger._file_state, "enabled", False)
        try:
            yield root
        finally:
            tmp.cleanup()


# =============================================================================
# 每个用例一个全新的 "default" 编排器（用例级隔离）
# =============================================================================
# 为什么需要这一段：
#
# `DebateManager` 是**进程级注册表**，生产上它就该让同一个 session_id 始终映射到
# 同一个 Orchestrator（前端轮询、断线重连都靠它）。但对测试来说这是个大坑：
# 三个测试模块都在**模块导入时**执行 `manager.get_or_create("default")`，
# 于是整个 pytest 会话里它们拿到的是**同一个可变对象**——一个用例 configure 过的
# 席位、改过的 status、monkeypatch 过的方法，会被后面另一个模块的用例看见。
#
# 更糟的是"还原"只能靠人工维护白名单（`_restore_llm_fakes` 里逐个 delattr），
# 新增一个 patch 点忘了登记就静默泄漏；而一旦用例顺序变化，故障现象也跟着变
# （表现为"改一处、别处莫名失败"）。
#
# 解决办法不是"更仔细地还原"，而是**根本不共享**：每个用例重新装入一个干净实例，
# 放进注册表的同一槽位，让 HTTP 端点解析到的始终是「本用例这一个」。
# 补丁随实例一起消失，不需要任何 delattr 白名单。


def install_default_orchestrator(session_id: str = "default"):
    """为当前用例装入一个全新的编排器，并占据注册表的 "default" 槽位。

    HTTP 端点用 `session_id="default"` 去 manager 取会话，因此把新实例放进
    该槽位后，测试里直接调用编排器和走 HTTP 拿到的是同一个对象。

    直接操作 `manager._sessions` 是有意的：注册表没有提供「替换」接口，
    而为测试在 production 代码里加一个 setter 不值得。这里集中一处说明原因。
    """
    from muti_llm_agent.manager import manager
    from muti_llm_agent.orchestrator import Orchestrator

    orch = Orchestrator(session_id=session_id)
    manager._sessions[session_id] = orch  # 用例隔离，见上方说明
    return orch


def drop_session(session_id: str = "default") -> None:
    """把会话从注册表摘掉（用例结束时调用，避免下一个用例看到残留）。"""
    from muti_llm_agent.manager import manager

    manager._sessions.pop(session_id, None)


class FreshOrchestratorMixin:
    """让每个用例跑在独立编排器上的混入。

    用法：`class XxxTest(FreshOrchestratorMixin, unittest.IsolatedAsyncioTestCase)`，
    用例内通过 **`self.orch`** 访问编排器（不要再引用模块级全局）。

    子类若自己定义了 `setUp`/`tearDown`（含 async 版本），**必须**调用 `super()`，
    否则本混入不会生效。
    """

    def setUp(self):
        self.orch = install_default_orchestrator()
        super().setUp()

    def tearDown(self):
        try:
            self.orch.end()  # 结束可能仍在跑的运行，避免任务泄漏到下一个用例
        except Exception:  # noqa: S110 - 用例已在异常路径上，end() 失败无可上报
            pass
        drop_session("default")
        super().tearDown()
