"""
api/router_setup.py — 配置与模型花名册

职责：把「这一场用什么模型、什么模式、多少轮」定下来。
对应端点：/api/defaults、/api/configure、/api/models、/api/recipes、/api/workspace
"""
from __future__ import annotations

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from muti_llm_agent.api.deps import (
    DISCUSSIONS_DIR,
    existing_orch,
    get_orch,
    normalize_sid,
    ts,
)
from muti_llm_agent.bgtasks import stream_pool_stats
from muti_llm_agent.config import PROJECT_ROOT, settings
from muti_llm_agent.history import list_discussions
from muti_llm_agent.llm import PROVIDER_PRESETS
from muti_llm_agent.manager import manager
from muti_llm_agent.models_registry import (
    available_providers,
    build_agent_configs,
    get_recipe,
    roster,
)
from muti_llm_agent.runlog import stats as runlog_stats
from muti_llm_agent.schemas import ConfigureRequest
from muti_llm_agent.vector_store import shared_vector_store

MODULE_NAME = "setup"
MODULE_TITLE = "配置与模型"
MODULE_DESC = "席位/模型花名册、协同配方、配置下发、工作区信息"

router = APIRouter()


def _redact_presets():
    """对外暴露 Provider 预设时剥离 api_key，避免密钥泄露。"""
    return {
        name: {k: (v if k != "api_key" else "") for k, v in preset.items()}
        for name, preset in PROVIDER_PRESETS.items()
    }


@router.get("/api/defaults", summary="默认配置与运行环境")
async def defaults():
    return {
        "providers": _redact_presets(),
        "default_provider": "default",
        "vector_backend": shared_vector_store.backend,
        "max_agents": 6,
        "upload_exts": _upload_exts(),
        # 运维可见性：访问控制是否开启 / 流式线程池规模（不含任何密钥）
        "auth_required": bool(settings.AUTH_TOKEN),
        "stream_pool": stream_pool_stats(),
        # 多模型协同试点：协同配方与模型花名册
        "recipes": roster()["recipes"],
        "default_recipe": settings.DEFAULT_RECIPE,
        "workspace": settings.WORKSPACE_NAME,
        "owner": settings.OWNER_NAME,
        "personal_mode": settings.PERSONAL_MODE,
        "features": {
            "steering": bool(settings.STEERING_ENABLED),
            "followup": bool(settings.FOLLOWUP_ENABLED),
            "review": True,
            "sse": True,
            "dev_console": bool(settings.DEV_API_ENABLED),
            "feishu": bool(settings.FEISHU_WEBHOOK_URL),
            "dsh": bool(settings.DSH_ENABLED),
        },
    }


def _upload_exts():
    from muti_llm_agent.file_parser import SUPPORTED_EXTS
    return SUPPORTED_EXTS


@router.get("/api/models", summary="模型花名册（各供应商配置状态 + 运行期健康度）")
async def models(reset: int = 0):
    """返回当前可用的模型供应商与协同配方（不含 api_key）。

    `reset=1` 会清空运行期失败记录（改完 .env 的模型 ID 后用它刷新健康度）。
    """
    if reset:
        from muti_llm_agent.models_registry import clear_failures
        clear_failures()
    return {"ok": True, **roster()}


@router.get("/api/recipes", summary="协同配方列表")
async def recipes():
    return {"ok": True, "items": roster()["recipes"],
            "default": settings.DEFAULT_RECIPE}


@router.get("/api/recipes/{name}", summary="按配方生成席位配置")
async def recipe_detail(name: str):
    """按配方名生成 `/api/configure` 可直接使用的席位配置（含改派说明）。"""
    if get_recipe(name) is None:
        return JSONResponse({"ok": False, "error": f"未知配方：{name}"}, status_code=404)
    try:
        cfg = build_agent_configs(name)
    except ValueError as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=400)
    # 把不可用供应商的改派建议与当前可用列表一并返回，前端可直接提示用户
    cfg["available_providers"] = available_providers()
    return {"ok": True, **cfg}


@router.post("/api/configure", summary="配置席位（Agent 列表 / 主题 / 轮次 / 裁判）")
async def configure(payload: ConfigureRequest):
    orch = get_orch(payload.session_id)
    if orch.is_active:
        return JSONResponse(
            {"ok": False, "error": "讨论进行中或已暂停，请先结束或等待后再重新配置"},
            status_code=409,
        )
    agents = [a.model_dump() for a in payload.agents]
    if not (1 <= len(agents) <= 6):
        return JSONResponse({"ok": False, "error": "Agent 数量需在 1-6 之间"}, status_code=400)
    try:
        orch.configure(agents, topic=payload.topic, max_rounds=payload.max_rounds,
                       judge_config=payload.judge or {}, mode=payload.mode)
    except ValueError as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=400)
    return {
        "ok": True,
        "session_id": orch.session_id,
        "mode": orch.mode,
        "agents": [{"name": a.name, "role": a.role, "is_main": a.is_main,
                    "stance": a.stance,
                    "model_id": a.model_config["model_id"]} for a in orch.agents],
        "judge": {"name": orch.judge.name} if orch.judge else None,
        "status": orch.status,
        "phase": orch.phase,
        "trace_id": orch.trace_id,
        "max_rounds": orch.max_rounds,
        # 公平性治理报告（L1-L6 各层状态；替代旧预算可行性视图）
        "fairness": orch.governor.report(),
        # 迭代链在重新配置时被重置，这里明确告知前端
        "iteration": orch.iteration_no,
    }


@router.get("/api/workspace", summary="工作区概览（个人级部署信息）")
async def workspace():
    """一次性返回工作区状态：存储路径、会话数、历史与运行数量。

    个人级试点的场景是「一个人一台机器」，因此这里直接把「数据存在哪、
    已经跑过多少场」摆出来，避免用户去翻文件系统。
    """
    runs = list_discussions()
    return {
        "ok": True,
        "workspace": settings.WORKSPACE_NAME,
        "owner": settings.OWNER_NAME,
        "personal_mode": settings.PERSONAL_MODE,
        "auth_required": bool(settings.AUTH_TOKEN),
        "max_sessions": manager.max_sessions,
        "active_sessions": len([
            s for s in manager.list_sessions().values()
            if getattr(s, "is_active", False)
        ]),
        "sessions": len(manager.list_sessions()),
        "paths": {
            "project_root": str(PROJECT_ROOT),
            "data": str(PROJECT_ROOT / "data"),
            "discussions": str(DISCUSSIONS_DIR),
            "runs": str(PROJECT_ROOT / "data" / "runs"),
            "reviews": str(PROJECT_ROOT / "data" / "reviews"),
            "logs": str(PROJECT_ROOT / "data" / "logs"),
        },
        "discussions": {"total": len(runs), "latest": runs[:3]},
        "runlog": runlog_stats(),
        "default_recipe": settings.DEFAULT_RECIPE,
        "now": ts(),
    }


@router.get("/api/session/{session_id}", summary="查看某会话的当前快照（不存在时返回 idle）")
async def session_snapshot(session_id: str):
    """会话快照：存在则返回完整状态，不存在返回 idle 形状（不创建会话）。"""
    from muti_llm_agent.api.deps import idle_status_body, status_body

    orch = existing_orch(session_id)
    return {"ok": True, **(status_body(orch) if orch is not None
                          else idle_status_body(normalize_sid(session_id)))}
