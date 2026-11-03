"""
api/router_run.py — 运行控制、中途纠偏、补充材料续轮、运行摘要

这是「一场协同」的生命周期主入口：
    configure → start →（steer…，pause/resume）→ end →（followup 续轮 → start）
每一场的终态都会产出一份运行摘要（/api/summary），并写入运行事件日志（runlog）。

拆成独立模块的理由：运行控制是最常被外部系统（飞书 / DSH / 脚本）调用的部分，
把它从 main.py 里分出来后，接口级测试与冒烟脚本可以只针对本模块的端点做验证。
"""
from __future__ import annotations

import asyncio

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from muti_llm_agent.api.deps import (
    existing_orch,
    get_orch,
    idle_status_body,
    normalize_sid,
    resolve_sid,
    status_body,
)
from muti_llm_agent.bgtasks import spawn as _spawn_task
from muti_llm_agent.config import settings
from muti_llm_agent.logger import log
from muti_llm_agent.schemas import FollowupRequest, SessionRequest, StartRequest, SteerRequest
from muti_llm_agent.steering import KIND_LABELS, PRIORITIES, SCOPES

MODULE_NAME = "run"
MODULE_TITLE = "运行控制"
MODULE_DESC = "开始/暂停/继续/结束、状态查询、中途纠偏、续轮、运行摘要"

router = APIRouter()


# ---------------- 生命周期 ----------------
@router.post("/api/start", summary="开始运行")
async def start(payload: StartRequest = None):
    payload = payload or StartRequest()
    orch = get_orch(payload.session_id)
    # 并发闸门：running 与 paused 都算「进行中」。暂停后再次 start 会在同一编排器
    # 上并发跑两个主循环（见 Orchestrator.is_active）。
    if orch.is_active:
        return JSONResponse(
            {"ok": False, "error": "讨论进行中或已暂停，请先结束或继续后再开始"},
            status_code=400,
        )
    if not orch.agents:
        return JSONResponse({"ok": False, "error": "请先配置 Agent"}, status_code=400)
    topic = payload.topic or orch.topic
    max_rounds = payload.max_rounds
    # 双模式：start 时可按 payload.mode 覆盖 configure 时的模式；缺省沿用
    if payload.mode is not None:
        orch.mode = payload.mode if payload.mode in ("debate", "collaboration") else "debate"
    _spawn_task(orch.run_discussion(topic=topic, max_rounds=max_rounds))
    # 让出一次事件循环：run_discussion 会在入口重置运行态并生成新的 trace_id，
    # 不 yield 就直接读会返回上一次运行的（或空的）trace_id，导致 /api/start 给出的
    # 链路标识与真实运行对不上，复盘时按 trace 查不到东西。
    await asyncio.sleep(0)
    return {
        "ok": True,
        "session_id": orch.session_id,
        "status": "running",
        "phase": "preparing",
        "trace_id": orch.trace_id,
        "iteration": orch.iteration_no,
    }


@router.post("/api/pause", summary="暂停运行")
async def pause(session_id: str = "default", payload: SessionRequest = None):
    orch = existing_orch(resolve_sid(payload, session_id))
    if orch is None:
        return JSONResponse({"ok": False, "error": "会话不存在或已过期"}, status_code=404)
    orch.pause()
    return {"ok": True, "session_id": orch.session_id,
            "status": orch.status, "phase": orch.phase}


@router.post("/api/resume", summary="继续运行")
async def resume(session_id: str = "default", payload: SessionRequest = None):
    orch = existing_orch(resolve_sid(payload, session_id))
    if orch is None:
        return JSONResponse({"ok": False, "error": "会话不存在或已过期"}, status_code=404)
    orch.resume()
    return {"ok": True, "session_id": orch.session_id,
            "status": orch.status, "phase": orch.phase}


@router.post("/api/end", summary="结束运行")
async def end(session_id: str = "default", payload: SessionRequest = None):
    orch = existing_orch(resolve_sid(payload, session_id))
    if orch is None:
        return JSONResponse({"ok": False, "error": "会话不存在或已过期"}, status_code=404)
    orch.end()
    return {"ok": True, "session_id": orch.session_id,
            "status": orch.status, "phase": orch.phase}


# ---------------- 状态 ----------------
@router.get("/api/status", summary="运行状态快照")
async def status(session_id: str = "default"):
    # 只读端点不创建会话：轮询不该有「新建 Orchestrator」的副作用
    orch = existing_orch(session_id)
    if orch is None:
        return idle_status_body(normalize_sid(session_id))
    body = status_body(orch)
    # 进程重启后的中断会话：该会话尚未重新配置（无 agents）时，
    # 用持久化的中断元数据补齐话题信息，供前端展示中断横幅
    if orch.status == "idle" and not orch.agents:
        from muti_llm_agent.manager import manager
        im = manager.interrupted_meta_for(orch.session_id)
        if im:
            body["status"] = "interrupted"
            body["interrupted"] = True
            body["topic"] = im.get("topic") or orch.topic
            body["round"] = im.get("round", 0)
            body["max_rounds"] = im.get("max_rounds", 8)
    return body


# ---------------- 中途注入纠偏 ----------------
@router.post("/api/steer", summary="中途注入纠偏指令")
async def steer(payload: SteerRequest):
    """讨论进行中下发一条纠偏指令，在下一个 Agent 回合注入 prompt。

    kind: correct / constrain / fact / redirect / stop
    scope: all / main / agent（配合 target 指定席位）
    """
    if not settings.STEERING_ENABLED:
        return JSONResponse({"ok": False, "error": "中途纠偏功能已关闭（STEERING_ENABLED=0）"},
                            status_code=403)
    orch = get_orch(payload.session_id)
    if not orch.agents:
        return JSONResponse({"ok": False, "error": "会话尚未配置席位，无法纠偏"},
                            status_code=400)
    if payload.kind not in KIND_LABELS:
        return JSONResponse(
            {"ok": False, "error": f"kind 必须是 {sorted(KIND_LABELS)} 之一"}, status_code=400)
    if payload.scope not in SCOPES:
        return JSONResponse({"ok": False, "error": f"scope 必须是 {list(SCOPES)} 之一"},
                            status_code=400)
    if payload.priority not in PRIORITIES:
        return JSONResponse({"ok": False, "error": f"priority 必须是 {list(PRIORITIES)} 之一"},
                            status_code=400)
    try:
        info = orch.add_directive(
            text=payload.text, kind=payload.kind, scope=payload.scope,
            target=payload.target, priority=payload.priority,
            source=payload.source, at_round=payload.at_round)
    except ValueError as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=400)
    return {
        "ok": True,
        "session_id": orch.session_id,
        "directive": info,
        "steering": orch.steering.stats(),
        # 运行中 → 下个回合生效；已结束时下发指令只会滞留在队列里，这里明确提示
        "will_apply": bool(orch.is_active),
        "hint": "" if orch.is_active else "当前无进行中的运行，指令将在下次 start 后生效",
    }


@router.get("/api/steer", summary="查看中途指令队列")
async def steer_list(session_id: str = "default"):
    orch = existing_orch(session_id)
    if orch is None:
        return {"ok": True, "items": [], "steering": {"total": 0, "pending": 0}}
    return {"ok": True, "session_id": orch.session_id,
            "items": orch.steering.all(), "steering": orch.steering.stats()}


@router.delete("/api/steer/{directive_id}", summary="撤销一条尚未生效的指令")
async def steer_cancel(directive_id: str, session_id: str = "default"):
    orch = existing_orch(session_id)
    if orch is None:
        return JSONResponse({"ok": False, "error": "会话不存在或已过期"}, status_code=404)
    ok = orch.steering.cancel(directive_id)
    if not ok:
        return JSONResponse(
            {"ok": False, "error": "指令不存在或已生效，无法撤销"}, status_code=404)
    return {"ok": True, "directive_id": directive_id, "steering": orch.steering.stats()}


# ---------------- 补充材料续轮 ----------------
@router.post("/api/followup", summary="会话结束后补充材料续轮")
async def followup(payload: FollowupRequest):
    """在已结束的运行之上，带着补充材料开启下一轮迭代。

    - materials：字符串 / 字符串数组 / [{"source","text"}]
    - focus：本轮聚焦点（可为空，但 materials 与 focus 不能同时为空）
    - auto_start：True 立即开始新一轮迭代；False 只准备承接包
    """
    if not settings.FOLLOWUP_ENABLED:
        return JSONResponse({"ok": False, "error": "续轮功能已关闭（FOLLOWUP_ENABLED=0）"},
                            status_code=403)
    orch = existing_orch(payload.session_id)
    if orch is None:
        return JSONResponse({"ok": False, "error": "会话不存在或已过期"}, status_code=404)
    try:
        info = orch.start_followup(materials=payload.materials, focus=payload.focus,
                                   max_rounds=payload.max_rounds)
    except ValueError as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=400)
    if payload.auto_start:
        _spawn_task(orch.run_discussion(topic=orch.topic, max_rounds=orch.max_rounds))
        # 同上：run_discussion 入口会再生成一次 trace_id，必须等它跑起来再读
        await asyncio.sleep(0)
        info["trace_id"] = orch.trace_id
        info["started"] = True
        info["status"] = "running"
    else:
        info["started"] = False
        info["status"] = orch.status
    log("info", "followup.prepare", "补充材料续轮已准备",
        session_id=orch.session_id, iteration=info.get("iteration"),
        materials_chars=info.get("materials_chars"))
    return {"ok": True, **info}


@router.get("/api/iterations", summary="迭代链（原始运行 + 每次续轮）")
async def iterations(session_id: str = "default"):
    orch = existing_orch(session_id)
    if orch is None:
        # 会话不在内存（进程重启 / 已被 LRU 淘汰）：退回磁盘上的会话元数据，
        # 否则「昨天续的那一轮」在复盘页会凭空消失。
        from muti_llm_agent.manager import manager
        meta = manager.load_meta(normalize_sid(session_id)) or {}
        items = [it for it in (meta.get("iterations") or []) if isinstance(it, dict)]
        return {"ok": True, "session_id": normalize_sid(session_id),
                "current": int(meta.get("iteration") or 1), "items": items,
                "running": None, "restored_from": "meta" if meta else ""}
    current = {
        "iteration": orch.iteration_no,
        "trace_id": orch.trace_id,
        "status": orch.status,
        "rounds": orch.round,
        "max_rounds": orch.max_rounds,
        "output_tokens": orch.output_tokens,
        "conclusion_core": ((orch.conclusion or {}).get("core") or "")[:600],
        "focus": orch.followup_focus,
        "materials_chars": orch.followup_materials_chars,
    }
    return {"ok": True, "session_id": orch.session_id,
            "current": orch.iteration_no,
            "items": list(orch.iterations), "running": current}


# ---------------- 运行摘要 ----------------
@router.get("/api/summary", summary="结构化运行摘要")
async def summary(session_id: str = "default"):
    """当前（或最近一次）运行的结构化摘要。

    与 /api/status 的区别：status 是**实时**状态（轮次 / 进度 / 队列），
    summary 是**聚合**视图（各模型指标 / 成本 / 指令统计 / 异常），用于复盘与对外集成。
    """
    orch = existing_orch(session_id)
    if orch is None:
        return {"ok": True, "summary": {
            "session_id": normalize_sid(session_id), "trace_id": "",
            "status": "idle", "agents": [], "tokens": {"total": 0, "output": 0},
            "models_used": [], "highlights": ["该会话尚未开始运行"],
        }}
    return {"ok": True, "summary": orch.summary()}
