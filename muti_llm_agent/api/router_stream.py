"""
api/router_stream.py — 实时推送（WebSocket / SSE）与可观测性检索

本项目同时提供两种流式通道，用途不同：

  - `WS /ws`          双向、浏览器 UI 用（已存在，带鉴权与断线重连）
  - `GET /api/stream` 单向 SSE，**给外部集成用**：飞书机器人 / DSH / curl 都能消费，
                      比 WebSocket 更容易穿过网关与反向代理，也便于用 `tail -f` 调试。

两者订阅的是同一个 `EventBus`，因此事件语义完全一致（speech_delta / task_assigned /
directive / run_summary …），不会出现「UI 看得到、集成看不到」的分裂。

另外提供三件可观测性查询：
  - `GET /api/logs`        进程内环形缓冲的结构化日志（按 trace_id / level 过滤）
  - `GET /api/trace/{id}`  某个 trace 的事件日志 + 日志 + 摘要索引（一键排障）
  - `GET /api/timeline`    复盘时间线（review.build_timeline）
"""
from __future__ import annotations

import asyncio
import json

from fastapi import APIRouter, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import StreamingResponse

from muti_llm_agent.api.deps import (
    bearer_of,
    existing_orch,
    get_orch,
    status_body,
    token_ok,
    unauthorized,
)
from muti_llm_agent.config import settings
from muti_llm_agent.logger import log as _log
from muti_llm_agent.logger import log_stats, query_logs
from muti_llm_agent.runlog import (
    get_run,
    list_runs,
    load_events,
)
from muti_llm_agent.runlog import (
    stats as runlog_stats,
)

MODULE_NAME = "stream"
MODULE_TITLE = "实时推送与可观测"
MODULE_DESC = "WebSocket / SSE 流、结构化日志检索、trace 时间线"

router = APIRouter()


def _sse(event: dict, event_name: str = "") -> str:
    """把事件编码成一帧 SSE（data 为单行 JSON，避免多行破坏帧边界）。"""
    payload = json.dumps(event, ensure_ascii=False, default=str)
    head = f"event: {event_name}\n" if event_name else ""
    return f"{head}data: {payload}\n\n"


# ---------------- WebSocket（浏览器 UI） ----------------
@router.websocket("/ws")
async def ws_endpoint(websocket: WebSocket, session_id: str = "default", token: str = ""):
    await websocket.accept()
    # 与 HTTP 侧一致的令牌校验：浏览器 WS 无法自定义请求头，故支持 ?token=
    if not token_ok(token or bearer_of(websocket.headers.get("authorization", ""))):
        await websocket.close(code=4401)  # 前端据此提示"未授权"
        return
    orch = get_orch(session_id)
    q = orch.event_bus.subscribe()
    try:
        # 连接时立即推送当前状态
        await websocket.send_json({
            "type": "status",
            "session_id": orch.session_id,
            "status": orch.status,
            "phase": orch.phase,
            "trace_id": orch.trace_id,
            "topic": orch.topic,
            "round": orch.round,
            "max_rounds": orch.max_rounds,
            "spent_tokens": orch.spent,          # 累计 token 用量（只读遥测）
            "output_tokens": orch.output_tokens,  # 累计输出 token（实时）
            "fairness": orch.governor.report(),
            "steering": orch.steering.stats(),
            "iteration": orch.iteration_no,
            "agents": [{"name": a.name, "role": a.role, "is_main": a.is_main,
                        "stance": a.stance,
                        "model_id": a.model_config.get("model_id", "")}
                       for a in orch.agents],
        })
        while True:
            event = await q.get()
            await websocket.send_json(event)
    except WebSocketDisconnect:
        # 客户端正常断连不是故障（S110 只约束宽泛捕获，这里显式保留原因）
        pass
    except Exception as exc:
        # 原来的 catch-all 会把真正的编程错误也一起吞掉，这里区分开：
        # 断连走上面分支，其余留一条 debug 便于排查推送链路。
        _log("debug", "ws.push_failed", "WebSocket 推送中断",
             session_id=orch.session_id, error=str(exc))
    finally:
        orch.event_bus.unsubscribe(q)


# ---------------- SSE（外部集成 / 脚本 / 调试） ----------------
@router.get("/api/stream", summary="SSE 事件流（供外部集成消费）")
async def sse_stream(request: Request, session_id: str = "default",
                     token: str = "", replay: int = 0):
    """Server-Sent Events 流。

    参数：
      session_id  订阅哪个会话的事件
      token       访问令牌（未配置 AUTH_TOKEN 时忽略）
      replay=1    连上后先回放该会话的历史运行事件（来自 runlog，最多 SSE_REPLAY_MAX 条）

    帧格式：`event: <type>\\ndata: <单行 JSON>\\n\\n`；空闲时每 SSE_KEEPALIVE_SEC
    发一行注释 `: keep-alive`，防止中间代理把长连接掐掉。
    """
    if not token_ok(token or bearer_of(request.headers.get("authorization", ""))):
        return unauthorized()
    orch = get_orch(session_id)
    queue = orch.event_bus.subscribe()
    keepalive = max(1.0, float(settings.SSE_KEEPALIVE_SEC))

    async def gen():
        try:
            # 1) 首帧：完整状态快照（客户端拿不到首帧就无法渲染）
            yield _sse({"type": "status", **status_body(orch)}, "status")
            yield _sse({"type": "stream_open", "session_id": orch.session_id,
                        "trace_id": orch.trace_id, "keepalive_sec": keepalive},
                       "stream_open")
            # 2) 可选回放：用运行事件日志补齐「连接之前发生的事」
            if replay:
                limit = max(1, int(settings.SSE_REPLAY_MAX))
                for ev in load_events(orch.trace_id, limit=limit):
                    yield _sse(ev, "replay")
            # 3) 实时推送
            while True:
                if await request.is_disconnected():
                    break
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=keepalive)
                except asyncio.TimeoutError:
                    yield ": keep-alive\n\n"
                    continue
                etype = str(event.get("type", "message"))
                yield _sse(event, etype)
        except asyncio.CancelledError:  # 客户端断开：正常退出
            raise
        except Exception as e:  # 推送异常不应把连接留成僵尸
            _log("warning", "stream.error", f"SSE 推送异常：{e}", error=str(e))
        finally:
            orch.event_bus.unsubscribe(queue)

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "Connection": "keep-alive",
            # 关掉 nginx 之类的响应缓冲，否则流式会被攒成一整块
            "X-Accel-Buffering": "no",
        },
    )


# ---------------- 日志与追踪 ----------------
@router.get("/api/logs", summary="查询结构化日志（进程内环形缓冲）")
async def logs(trace_id: str = "", level: str = "", event: str = "", limit: int = 100):
    items = query_logs(trace_id=trace_id, level=level, limit=limit, event=event)
    return {"ok": True, "items": items, "count": len(items), **log_stats()}


@router.get("/api/trace/{trace_id}", summary="按 trace_id 汇总排障信息")
async def trace_detail(trace_id: str, limit: int = 500):
    """一条 trace 的全量视图：运行事件 + 诊断日志 + 索引记录。

    排查「这一场为什么跑歪了」时，按 trace_id 一次拿全，不用在三个页面之间跳。
    """
    events = load_events(trace_id, limit=limit)
    return {
        "ok": bool(events),
        "trace_id": trace_id,
        "events": events,
        "event_count": len(events),
        "logs": query_logs(trace_id=trace_id, limit=limit),
        "index": get_run(trace_id),
        "error": "" if events else "该 trace_id 没有运行事件记录（可能尚未运行或已被清理）",
    }


@router.get("/api/timeline", summary="复盘时间线")
async def timeline(session_id: str = "default", trace_id: str = "", limit: int = 300):
    """优先按 trace_id 取；未指定时取该会话当前运行的 trace。"""
    from muti_llm_agent.review import build_timeline

    tid = (trace_id or "").strip()
    if not tid:
        orch = existing_orch(session_id)
        tid = getattr(orch, "trace_id", "") if orch is not None else ""
    if not tid:
        return {"ok": True, "trace_id": "", "items": [], "count": 0}
    items = build_timeline(tid, limit=limit)
    return {"ok": True, "trace_id": tid, "items": items, "count": len(items)}


@router.get("/api/runs", summary="运行索引（历史上每一场运行）")
async def runs(q: str = "", limit: int = 50):
    return {"ok": True, "items": list_runs(q=q, limit=limit), "runlog": runlog_stats()}


@router.get("/api/runs/{trace_id}", summary="单场运行的事件明细")
async def run_detail(trace_id: str, limit: int = 1000):
    events = load_events(trace_id, limit=limit)
    return {"ok": bool(events), "trace_id": trace_id, "items": events,
            "count": len(events), "index": get_run(trace_id)}
