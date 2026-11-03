"""
main.py — FastAPI 应用装配（后端入口）

本文件只做「装配」这一件事，不再承载业务逻辑：

  1. 创建 FastAPI 实例、挂静态目录、加鉴权与观测中间件；
  2. 注册 api/ 下的各功能路由模块（配置 / 知识库 / 运行 / 推送 / 导出 / 调试 / 集成）；
  3. 页面路由：`/` 圆桌 UI、`/console` 接口控制台；
  4. lifespan：启动时标记遗留中断会话、注册「运行完成」回调（落盘 + 对外推送）。

模块化的动机（相对早期把所有端点写在 main.py 的做法）：
  - 一个 550 行的 main.py 里同时有配置、运行、导出、WebSocket、鉴权，
    定位问题时必须在无关代码之间来回跳；
  - 拆成 router 之后，`/api/dev/endpoints` 能按模块导出接口清单，
    接口控制台与冒烟测试可以只针对某个模块做验证（模块化调试）；
  - 新增能力（纠偏 / 续轮 / 集成）只需要新增一个 router 并在 api/__init__.py
    登记，不必再改 main.py。

进程启动入口仍然是项目根目录的 start.py（uvicorn）。
"""
from __future__ import annotations

import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from muti_llm_agent.api import ALL_MODULES
from muti_llm_agent.api.deps import (
    DISCUSSIONS_DIR,
    PUBLIC_PATHS,
    PUBLIC_PREFIXES,
    STATIC_DIR,
    request_token,
    token_ok,
    unauthorized,
)
from muti_llm_agent.api.router_integration import notify_run_finished
from muti_llm_agent.config import PROJECT_ROOT, settings, warn_data_dir_errors
from muti_llm_agent.history import record_discussion
from muti_llm_agent.logger import log, set_log_context
from muti_llm_agent.manager import manager
from muti_llm_agent.protocol import new_trace_id
from muti_llm_agent.runlog import stats as runlog_stats


# ---------------- 讨论完成回调：落盘 + 对外推送 ----------------
def _persist_done_markdown(orch) -> str:
    """讨论完成时把 Markdown 落盘并记录历史索引；返回文件名。"""
    md = orch.export_markdown()
    fname = f"discussion_{time.strftime('%Y%m%d_%H%M%S')}.md"
    (DISCUSSIONS_DIR / fname).write_text(md, encoding="utf-8")
    # 历史讨论题目记忆化保存（供后续检索）
    record_discussion(
        topic=orch.topic,
        trace_id=orch.trace_id,
        filename=fname,
        status=orch.status,
        phase=orch.phase,
        round_no=orch.round,
        max_rounds=orch.max_rounds,
        token_spent=orch.spent,
        agents=[{"name": a.name, "role": a.role, "stance": a.stance,
                 "is_main": a.is_main} for a in orch.agents],
    )
    return fname


def _on_done(orch):
    """讨论完成回调：落盘失败不静默吞掉（记录 + 重试一次），随后推送外部集成。"""
    for attempt in (1, 2):
        try:
            _persist_done_markdown(orch)
            break
        except Exception as e:
            log("error", "lifespan.save_markdown",
                f"讨论落盘失败（第 {attempt} 次）", error=str(e),
                trace_id=orch.trace_id, session_id=orch.session_id)
    try:
        notify_run_finished(orch)
    except Exception as e:
        log("warning", "lifespan.notify_failed",
            f"运行完成推送失败：{e}", error=str(e))


@asynccontextmanager
async def lifespan(app: FastAPI):
    # 启动即检查运行产物目录：不可写时必须显式报出来。
    # 否则现场是「应用看起来一切正常」，直到某次落盘才在别处以错误暴露。
    warn_data_dir_errors()
    # 启动：把上一进程遗留的 running/paused 会话标记为 interrupted
    try:
        interrupted = manager.mark_interrupted()
        if interrupted:
            log("info", "lifespan.mark_interrupted",
                f"标记 {len(interrupted)} 个进程遗留的中断会话",
                session_ids=[m.get("session_id") for m in interrupted])
    except Exception as e:
        log("warning", "lifespan.mark_interrupted",
            f"标记中断会话失败：{e}", error=str(e))
    # 注册完成回调（每个会话讨论完成时自动落盘 Markdown 并对外推送）
    manager.set_done_handler(_on_done)
    log("info", "lifespan.start", "多模型协同工作台已启动",
        project_root=str(PROJECT_ROOT),
        workspace=settings.WORKSPACE_NAME, owner=settings.OWNER_NAME,
        personal_mode=settings.PERSONAL_MODE,
        auth_required=bool(settings.AUTH_TOKEN),
        modules=[name for name, _r, _m in ALL_MODULES],
        runlog=runlog_stats())
    yield
    log("info", "lifespan.stop", "多模型协同工作台已停止")


app = FastAPI(
    title="muti_llm_agent · 多模型协同工作台",
    description=(
        "单用户个人级的多模型协同试点：多个大模型分工配合完成任务，"
        "支持流式 UI、中途注入纠偏、补充材料续轮、复盘导出、接口级调试与对外集成。"),
    version="1.0.0",
    lifespan=lifespan,
    openapi_tags=[
        # tag 名必须与 include_router(tags=[...]) 完全一致，否则 Swagger 会把同名
        # 分组拆成两段（中文标题 + 英文名）。
        {"name": getattr(mod, "MODULE_TITLE", name),
         "description": getattr(mod, "MODULE_DESC", "")}
        for name, _r, mod in ALL_MODULES
    ],
)

app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


# ---------------- 访问控制（可选令牌鉴权） ----------------
# 默认（AUTH_TOKEN 为空）不校验，靠 start.py 只绑 127.0.0.1 保证本地安全；
# 一旦用 HOST=0.0.0.0 对外提供服务，务必在 .env 设置 AUTH_TOKEN，
# 否则任何人都能改写配置、发起（消耗真实额度的）讨论。
@app.middleware("http")
async def auth_middleware(request: Request, call_next):
    """未配置 AUTH_TOKEN 时直通；配置后所有 /api/* 必须带正确令牌。"""
    if not settings.AUTH_TOKEN:
        return await call_next(request)
    path = request.url.path
    if path in PUBLIC_PATHS or path.startswith(PUBLIC_PREFIXES):
        return await call_next(request)
    if token_ok(request_token(request)):
        return await call_next(request)
    return unauthorized()


@app.middleware("http")
async def observability_middleware(request: Request, call_next):
    """给每个请求分配 trace_id，并把耗时与链路标识回写到响应头。

    客户端可以用 `X-Trace-Id` 自带链路标识（便于把外部系统的调用链与本系统对齐），
    没有则就地生成。响应头里的 `X-Trace-Id` / `X-Elapsed-Ms` 让接口控制台与
    curl 都能直接拿到排障入口（配合 `GET /api/trace/{trace_id}`）。
    """
    trace_id = (request.headers.get("x-trace-id") or "").strip() or new_trace_id()
    set_log_context(trace_id=trace_id)
    started = time.perf_counter()
    response = await call_next(request)
    elapsed_ms = round((time.perf_counter() - started) * 1000, 1)
    response.headers["X-Trace-Id"] = trace_id
    response.headers["X-Elapsed-Ms"] = str(elapsed_ms)
    return response


# ---------------- 路由模块注册 ----------------
for _module_name, _router, _module in ALL_MODULES:
    app.include_router(_router, tags=[getattr(_module, "MODULE_TITLE", _module_name)])


# ---------------- 页面 ----------------
@app.get("/", include_in_schema=False)
async def index():
    """圆桌 UI（基于 WebSocket 的实时流式界面）。"""
    return FileResponse(str(STATIC_DIR / "index.html"))


@app.get("/favicon.ico", include_in_schema=False)
async def favicon():
    """避免浏览器控制台里一条无意义的 404。"""
    return Response(status_code=204)


@app.get("/healthz", include_in_schema=False)
async def healthz():
    """轻量健康检查（不触碰编排器，供外部探活使用）。"""
    return JSONResponse({
        "ok": True,
        "service": "muti_llm_agent",
        "workspace": settings.WORKSPACE_NAME,
        "sessions": len(manager.list_sessions()),
        "auth_required": bool(settings.AUTH_TOKEN),
    })
