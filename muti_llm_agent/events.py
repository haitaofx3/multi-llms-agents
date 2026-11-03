"""
events.py — 事件总线（有界队列 + 背压）

编排器把讨论过程中的全部事件（发言增量、任务、状态同步、日志、裁决…）经
`EventBus` 广播给订阅者（WebSocket 连接）。这里用**有界队列**替代无界
`asyncio.Queue`：

- 无界队列在客户端停止读取（网络卡住 / 标签页挂起）但连接未断时会无限堆积，
  进程内存持续上涨，而且没有任何可观测信号；
- `publish` 由讨论主循环（每轮多次）直接 `await`，绝不能因为某个订阅者慢
  而阻塞讨论推进 —— 因此投递用 `put_nowait`，队列满时丢弃**最旧**事件并计数。

丢事件只会让该客户端少几帧过期增量（终态由 `speech_end` / `status_sync`
兜底），换来的是主循环永不被 UI 拖住，且丢弃量可观测（`dropped` + 按百计数
落 warning 日志）。
"""
from __future__ import annotations

import asyncio
from typing import Any

from muti_llm_agent.logger import log

# 单个订阅者的事件队列上限（约等于十几轮的发言增量，足够覆盖正常消费速度）
DEFAULT_QUEUE_MAXSIZE = 512


class EventBus:
    """进程内发布 / 订阅总线（单事件循环，有界背压）。"""

    def __init__(self, maxsize: int = DEFAULT_QUEUE_MAXSIZE):
        self.maxsize = max(1, int(maxsize))
        self.subs: list[asyncio.Queue] = []
        self.dropped = 0            # 累计丢弃事件数（背压可观测指标）
        self._drop_reported = 0     # 上次落日志时的丢弃数（按百上报，防刷屏）

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=self.maxsize)
        self.subs.append(q)
        return q

    async def publish(self, event: dict) -> None:
        """广播事件；队列满时丢最旧的一条，绝不阻塞发布方。"""
        for q in list(self.subs):
            try:
                q.put_nowait(event)
                continue
            except asyncio.QueueFull:
                pass
            # 队列满：先腾掉最旧的一条（过期增量），再尝试放入当前事件
            try:
                q.get_nowait()
                self.dropped += 1
                self._report_drop()
            except asyncio.QueueEmpty:
                pass
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                self.dropped += 1
                self._report_drop()

    def _report_drop(self) -> None:
        """丢弃数每满 100 条落一次 warning，避免日志本身成为刷屏源。"""
        if self.dropped - self._drop_reported >= 100:
            self._drop_reported = self.dropped
            log("warning", "eventbus.backpressure",
                "事件队列积压，已丢弃最旧事件（客户端消费过慢）",
                dropped=self.dropped, maxsize=self.maxsize)

    def unsubscribe(self, q: Any) -> None:
        if q in self.subs:
            self.subs.remove(q)

    def stats(self) -> dict:
        """订阅者 / 队列上限 / 累计丢弃数（供状态接口与排查使用）。"""
        return {
            "subscribers": len(self.subs),
            "maxsize": self.maxsize,
            "dropped": self.dropped,
        }
