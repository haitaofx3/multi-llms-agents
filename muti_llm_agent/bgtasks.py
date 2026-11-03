"""
bgtasks.py — 后台任务与线程池共享管理

1. `spawn(coro)`：持有所有后台 asyncio 任务的强引用，防止任务被 GC 提前回收
   （RUF006），任务结束后自动从集合移除。main.py 与 orchestrator.py 统一复用。
2. `stream_pool()` / `submit_stream(fn)`：LLM 流式调用专用的**有界线程池**。

为什么需要线程池：OpenAI SDK 的流式接口是同步阻塞的，只能在 worker 线程里跑。
早期实现是「每次调用 `threading.Thread(...).start()`」，线程数完全不受控 ——
流式超时后线程并不会被立刻杀死（它会一直阻塞到 SDK 自己返回），长时间运行或
高并发时会累积出僵尸线程。改为有界线程池后：并发上限固定
（`LLM_STREAM_WORKERS`，默认 8），超额请求排队而不是无限建线程。
"""
import asyncio
import threading
from concurrent.futures import Future, ThreadPoolExecutor

from muti_llm_agent.config import settings

_background_tasks: set[asyncio.Task] = set()

_stream_pool: ThreadPoolExecutor | None = None
_stream_pool_max = 0
_pool_lock = threading.Lock()


def spawn(coro) -> asyncio.Task:
    """创建并登记一个后台任务，任务结束后自动从集合移除。"""
    task = asyncio.create_task(coro)
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)
    return task


def spawn_safe(coro) -> asyncio.Task | None:
    """`spawn` 的容错版本：当前没有运行中的事件循环时丢弃该协程。

    有些「广播一条事件」的调用点可能来自同步上下文（例如飞书 webhook 回调
    在请求线程里构造响应、或单元测试直接调用编排器方法）。这种情况下
    `asyncio.create_task` 会抛 RuntimeError，还会留下「coroutine was never
    awaited」警告。事件推送属于尽力而为的旁路，失败不该影响主流程。
    """
    try:
        return spawn(coro)
    except RuntimeError:
        try:
            coro.close()
        except Exception:  # noqa: S110 - 协程已确定不会被调度，close 失败无可上报的信息
            pass
        return None


def stream_pool() -> ThreadPoolExecutor:
    """LLM 流式调用专用线程池（懒加载单例，容量 `settings.LLM_STREAM_WORKERS`）。"""
    global _stream_pool, _stream_pool_max
    with _pool_lock:
        if _stream_pool is None:
            _stream_pool_max = max(1, int(settings.LLM_STREAM_WORKERS))
            _stream_pool = ThreadPoolExecutor(
                max_workers=_stream_pool_max,
                thread_name_prefix="llm-stream",
            )
        return _stream_pool


def submit_stream(fn, *args, **kwargs) -> Future:
    """把同步的流式迭代函数提交到流式线程池，返回 Future。"""
    return stream_pool().submit(fn, *args, **kwargs)


def stream_pool_stats() -> dict:
    """线程池规模（供状态接口 / 排查使用）。"""
    stream_pool()
    return {"max_workers": _stream_pool_max}
