"""
api/router_integration.py — 对外集成（飞书 / DSH / 通用 webhook）

三条集成路径，面向三种不同的「外部驱动」方式：

  1. **飞书入站**：`POST /api/integrations/feishu/webhook`
     飞书事件订阅回调（url_verification 握手 + 消息接收）。个人级默认只回文本，
     打开 `FEISHU_AUTO_RUN=1` 后可以把消息直接变成一场协同讨论。
  2. **飞书出站**：`POST /api/integrations/feishu/push`
     把当前运行的摘要（或自定义文本）推送到群机器人 webhook。
  3. **DSH 工具调用**：`GET /api/integrations/dsh/manifest` 拿工具清单，
     `POST /api/integrations/dsh/invoke` 以 `{tool, arguments}` 形式驱动本系统
     （开始协同 / 查询状态 / 中途纠偏 / 续轮 / 取摘要 / 导出 / 查历史）。

以及通用出站 webhook（`WEBHOOK_URLS`），供其它系统订阅「运行结束」事件。

设计要点：
  - 集成端点绝不抛 500：外部系统的异常格式不能拖垮本服务，一律转成 `{ok:false}`；
  - 出站 HTTP 全部走 `asyncio.to_thread`，避免同步 urllib 阻塞事件循环；
  - DSH 端点支持共享密钥（`DSH_SHARED_SECRET`），与用户令牌（`AUTH_TOKEN`）分离，
    因为外部 Agent 的凭据生命周期通常比个人令牌长。
"""
from __future__ import annotations

import asyncio

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from muti_llm_agent.api.deps import (
    check_dsh_secret,
    existing_orch,
    get_orch,
    idle_status_body,
    normalize_sid,
    status_body,
)
from muti_llm_agent.bgtasks import spawn as _spawn_task
from muti_llm_agent.config import settings, webhook_urls
from muti_llm_agent.history import list_discussions
from muti_llm_agent.integrations import (
    TOOL_SPECS,
    DshToolError,
    dispatch_run_summary,
    dsh_manifest,
    parse_feishu_event,
    post_json_webhook,
    push_feishu_webhook,
    summary_sentence,
    validate_arguments,
)
from muti_llm_agent.logger import log
from muti_llm_agent.models_registry import build_agent_configs
from muti_llm_agent.review import export_content
from muti_llm_agent.schemas import DshInvokeRequest, FeishuPushRequest, WebhookTestRequest

MODULE_NAME = "integration"
MODULE_TITLE = "对外集成"
MODULE_DESC = "飞书入站/出站、DSH 工具清单与调用、通用 webhook"

router = APIRouter()


def _err(message: str, status_code: int = 400, **extra):
    return JSONResponse({"ok": False, "error": message, **extra}, status_code=status_code)


# ---------------- 集成状态 ----------------
@router.get("/api/integrations", summary="集成状态总览")
async def integrations_status():
    return {
        "ok": True,
        "feishu": {
            "outbound_configured": bool(settings.FEISHU_WEBHOOK_URL),
            "webhook": bool(settings.FEISHU_WEBHOOK_URL),
            "verification_token_set": bool(settings.FEISHU_VERIFICATION_TOKEN),
            "push_on_done": bool(settings.FEISHU_PUSH_ON_DONE),
            "auto_run": bool(settings.FEISHU_AUTO_RUN),
            "auto_run_max_rounds": settings.FEISHU_AUTO_RUN_MAX_ROUNDS,
            "inbound_path": "/api/integrations/feishu/webhook",
            "outbound_path": "/api/integrations/feishu/push",
        },
        "dsh": {
            "enabled": bool(settings.DSH_ENABLED),
            "shared_secret_set": bool(settings.DSH_SHARED_SECRET),
            "base_url": settings.DSH_BASE_URL,
            "manifest_path": "/api/integrations/dsh/manifest",
            "invoke_path": "/api/integrations/dsh/invoke",
            "tools": [t.get("name") for t in TOOL_SPECS],
        },
        "webhooks": {
            "urls": webhook_urls(),
            "on_done": bool(settings.WEBHOOK_ON_DONE),
            "timeout": settings.WEBHOOK_TIMEOUT,
        },
        "stream": {
            "sse_path": "/api/stream",
            "ws_path": "/ws",
            "keepalive_sec": settings.SSE_KEEPALIVE_SEC,
        },
    }


# ---------------- DSH ----------------
@router.get("/api/integrations/dsh/manifest", summary="DSH 工具清单（MCP 风格）")
async def dsh_manifest_endpoint(request: Request, base_url: str = ""):
    blocked = check_dsh_secret(request)
    if blocked is not None:
        return blocked
    if not settings.DSH_ENABLED:
        return _err("DSH 集成已关闭（DSH_ENABLED=0）", 403)
    return dsh_manifest(base_url=base_url or settings.DSH_BASE_URL)


@router.post("/api/integrations/dsh/invoke", summary="DSH 工具调用")
async def dsh_invoke(payload: DshInvokeRequest, request: Request):
    """以 `{tool, arguments}` 驱动本系统。

    参数会先经 `validate_arguments` 校验与规整（补默认值 / 类型转换），
    未知工具或坏参数返回 400 而不是 500。
    """
    blocked = check_dsh_secret(request)
    if blocked is not None:
        return blocked
    if not settings.DSH_ENABLED:
        return _err("DSH 集成已关闭（DSH_ENABLED=0）", 403)
    try:
        args = validate_arguments(payload.tool, payload.arguments or {})
    except DshToolError as e:
        return _err(f"{e}（tool={payload.tool}）", 400, code=getattr(e, "code", ""))
    except Exception as e:  # 校验层的其他异常同样不能变成 500
        return _err(f"参数校验失败：{e}", 400)

    session_id = normalize_sid(args.get("session_id") if isinstance(args, dict) else None)
    try:
        result = await _dispatch_tool(payload.tool, session_id, args)
    except ValueError as e:
        return _err(str(e), 400)
    except Exception as e:
        log("error", "dsh.invoke_failed", f"DSH 工具调用失败：{e}",
            tool=payload.tool, error=str(e))
        return _err(f"工具执行失败：{e}", 500)
    log("info", "dsh.invoke", f"DSH 调用工具 {payload.tool}",
        tool=payload.tool, status=str(result.get("status", ""))[:40])
    return {"ok": True, "tool": payload.tool, "session_id": session_id, "result": result}


async def _dispatch_tool(tool: str, session_id: str, args: dict) -> dict:
    """DSH 工具名 → 内部动作。"""
    if tool == "collab_start":
        recipe = args.get("recipe") or settings.DEFAULT_RECIPE
        orch = get_orch(session_id)
        if orch.is_active:
            raise ValueError("该会话已有进行中的运行，请先 end 或等待结束")
        # 每次 collab_start 都用配方重建席位，保证「一次调用 = 一次干净配置」
        cfg = build_agent_configs(recipe)
        orch.configure(cfg["agents"], topic=args.get("topic", ""),
                       max_rounds=int(args.get("max_rounds") or cfg["max_rounds"]),
                       judge_config=cfg.get("judge") or {},
                       mode=args.get("mode") or cfg["mode"])
        _spawn_task(orch.run_discussion(topic=orch.topic, max_rounds=orch.max_rounds))
        # 让出一次事件循环，等 run_discussion 重置运行态并生成真实 trace_id
        await asyncio.sleep(0)
        return {"status": "running", "trace_id": orch.trace_id,
                "session_id": orch.session_id, "recipe": recipe,
                "mode": orch.mode, "max_rounds": orch.max_rounds,
                "agents": [a.name for a in orch.agents],
                "stream": f"/api/stream?session_id={orch.session_id}"}

    if tool == "collab_status":
        orch = existing_orch(session_id)
        return status_body(orch) if orch is not None else idle_status_body(session_id)

    if tool == "collab_steer":
        orch = get_orch(session_id)
        if not orch.agents:
            raise ValueError("会话尚未配置席位，无法纠偏")
        info = orch.add_directive(
            text=args.get("text", ""), kind=args.get("kind", "correct"),
            scope=args.get("scope", "all"), target=args.get("target", ""),
            priority=args.get("priority", "normal"), source="dsh")
        return {"directive": info, "will_apply": bool(orch.is_active),
                "steering": orch.steering.stats()}

    if tool == "collab_followup":
        orch = existing_orch(session_id)
        if orch is None:
            raise ValueError("会话不存在或已过期")
        info = orch.start_followup(
            materials=args.get("materials", ""), focus=args.get("focus", ""),
            max_rounds=args.get("max_rounds"))
        if args.get("auto_start", True):
            _spawn_task(orch.run_discussion(topic=orch.topic, max_rounds=orch.max_rounds))
            info["started"] = True
        return info

    if tool == "collab_summary":
        orch = existing_orch(session_id)
        if orch is None:
            return {"status": "idle", "session_id": session_id,
                    "note": "该会话尚未开始运行"}
        summary = orch.summary()
        return {"summary": summary, "sentence": summary_sentence(summary)}

    if tool == "collab_export":
        orch = existing_orch(session_id)
        if orch is None:
            raise ValueError("会话不存在或已过期")
        content, media_type, filename = export_content(
            orch, args.get("format", "md"))
        return {"format": args.get("format", "md"), "filename": filename,
                "media_type": media_type, "content": content,
                "chars": len(content)}

    if tool == "collab_history":
        items = list_discussions(args.get("query", ""))[: int(args.get("limit", 20))]
        return {"count": len(items), "items": items}

    raise DshToolError("unknown_tool")


# ---------------- 飞书 ----------------
@router.post("/api/integrations/feishu/webhook", summary="飞书事件回调（入站）")
async def feishu_webhook(request: Request):
    """飞书事件订阅回调。

    - url_verification 握手：原样返回 `{"challenge": ...}`；
    - 普通消息：默认仅回执（`FEISHU_AUTO_RUN=0`），打开后自动发起一场协同讨论。
    任何畸形请求体都返回 `{"code": 0}` 或明确的错误，不抛 500。
    """
    try:
        body = await request.json()
    except Exception:
        return {"code": 0, "msg": "ok", "note": "无法解析请求体，已忽略"}
    if not isinstance(body, dict):
        return {"code": 0, "msg": "ok", "note": "请求体不是 JSON 对象，已忽略"}

    # 可选：校验 verification token（飞书在 header.token 或顶层 token 里给）
    expected = settings.FEISHU_VERIFICATION_TOKEN or ""
    if expected:
        got = str(body.get("token") or (body.get("header") or {}).get("token") or "")
        if got and got != expected:
            return _err("飞书 verification token 不匹配", 401)

    event = parse_feishu_event(body)
    if event.get("type") == "challenge":
        # 飞书要求的握手响应形状
        return {"challenge": event.get("challenge", "")}

    text = (event.get("text") or "").strip()
    reply: dict = {
        "code": 0,
        "msg": "ok",
        "session_id": normalize_sid("default"),
        "received": {"text": text[:500], "user": event.get("user", "")},
    }
    if not text:
        reply["note"] = "未识别到消息文本"
        return reply

    if not settings.FEISHU_AUTO_RUN:
        reply["note"] = ("已收到。当前未开启自动发起（FEISHU_AUTO_RUN=0），"
                         "可用 /api/integrations/dsh/invoke 或接口控制台手动发起。")
        reply["hint"] = "把消息文本作为 topic 传入 collab_start 即可自动跑一场协同"
        return reply

    # 自动发起：用默认配方建席位 → 以消息文本为主题开始运行
    session_id = normalize_sid("feishu")
    orch = get_orch(session_id)
    if orch.is_active:
        reply["note"] = "该会话已有进行中的运行，已忽略本次消息"
        return reply
    try:
        cfg = build_agent_configs(settings.DEFAULT_RECIPE)
        orch.configure(cfg["agents"], topic=text,
                       max_rounds=settings.FEISHU_AUTO_RUN_MAX_ROUNDS,
                       judge_config=cfg.get("judge") or {},
                       mode=cfg["mode"])
        _spawn_task(orch.run_discussion(topic=orch.topic, max_rounds=orch.max_rounds))
    except Exception as e:
        log("error", "feishu.auto_run_failed", f"飞书自动发起失败：{e}", error=str(e))
        return _err(f"自动发起失败：{e}", 500)
    reply.update({"started": True, "session_id": session_id,
                  "trace_id": orch.trace_id,
                  "stream": f"/api/stream?session_id={session_id}"})
    return reply


@router.post("/api/integrations/feishu/push", summary="推送运行摘要到飞书群机器人")
async def feishu_push(payload: FeishuPushRequest):
    """把某场运行的摘要（或自定义文本）推到飞书群机器人。

    未显式给 webhook_url 时使用 `.env` 的 FEISHU_WEBHOOK_URL。
    """
    url = (payload.webhook_url or settings.FEISHU_WEBHOOK_URL or "").strip()
    if not url:
        return _err("未配置飞书 webhook：请在 .env 设置 FEISHU_WEBHOOK_URL "
                    "或在请求体里传 webhook_url", 400)
    orch = existing_orch(payload.session_id)
    summary = orch.summary() if orch is not None else {
        "topic": payload.text or "（无运行中的会话）",
        "status": "idle", "session_id": normalize_sid(payload.session_id),
        "text": payload.text,
    }
    if payload.text:
        summary = dict(summary, topic=payload.text)
    result = await asyncio.to_thread(
        push_feishu_webhook, url, summary, payload.as_card,
        settings.WEBHOOK_TIMEOUT)
    return {"ok": bool(result.get("ok")), "result": result,
            "sentence": summary_sentence(summary)}


# ---------------- 通用 webhook ----------------
@router.post("/api/integrations/webhooks/dispatch", summary="把运行摘要推送到已配置的 webhook")
async def webhooks_dispatch(session_id: str = "default"):
    urls = webhook_urls()
    if not urls:
        return _err("未配置 WEBHOOK_URLS（.env 中逗号分隔）", 400)
    orch = existing_orch(session_id)
    if orch is None:
        return _err("会话不存在或已过期", 404)
    summary = orch.summary()
    results = await asyncio.to_thread(
        dispatch_run_summary, urls, summary, settings.WEBHOOK_TIMEOUT)
    return {"ok": all(r.get("ok") for r in results), "results": results}


@router.post("/api/integrations/webhooks/test", summary="测试出站 webhook")
async def webhooks_test(payload: WebhookTestRequest):
    url = (payload.url or (webhook_urls()[0] if webhook_urls() else "")).strip()
    if not url:
        return _err("未提供 url，且 WEBHOOK_URLS 为空", 400)
    body = payload.payload or {"event": "webhook.test",
                               "text": "来自 muti_llm_agent 的连通性测试"}
    result = await asyncio.to_thread(
        post_json_webhook, url, body, settings.WEBHOOK_TIMEOUT)
    return {"ok": bool(result.get("ok")), "result": result}


# ---------------- 运行完成时的出站推送（供 main.py 的完成回调调用） ----------------
def notify_run_finished(orch) -> None:
    """运行完成回调：按配置把摘要推送到飞书 / 通用 webhook。

    同步函数（在完成回调里被调用），内部用后台线程做实际 HTTP，
    避免慢速外部系统拖住讨论主循环的收尾。
    """
    import threading

    summary = orch.summary()
    tasks: list[tuple] = []
    if settings.FEISHU_PUSH_ON_DONE and settings.FEISHU_WEBHOOK_URL:
        tasks.append((push_feishu_webhook, settings.FEISHU_WEBHOOK_URL, summary, True,
                      settings.WEBHOOK_TIMEOUT))
    urls = webhook_urls()
    if settings.WEBHOOK_ON_DONE and urls:
        tasks.append((dispatch_run_summary, urls, summary, settings.WEBHOOK_TIMEOUT))
    if not tasks:
        return

    def _run():
        for fn, *fn_args in tasks:
            try:
                fn(*fn_args)
            except Exception as e:  # 出站通知失败只记日志
                log("warning", "integration.push_failed",
                    f"运行完成推送失败：{e}", error=str(e))

    threading.Thread(target=_run, name="integration-push", daemon=True).start()
