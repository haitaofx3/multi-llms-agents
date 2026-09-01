"""
bgtasks.py — 后台 asyncio 任务共享管理

持有所有后台任务的强引用，防止任务被 GC 提前回收（RUF006），任务结束后
自动从集合移除。main.py 与 orchestrator.py 统一复用本模块的 spawn()，
避免两份重复实现导致的管理入口漂移。
"""
import asyncio

_background_tasks: set[asyncio.Task] = set()


def spawn(coro) -> asyncio.Task:
    """创建并登记一个后台任务，任务结束后自动从集合移除。"""
    task = asyncio.create_task(coro)
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)
    return task
