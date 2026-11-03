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
