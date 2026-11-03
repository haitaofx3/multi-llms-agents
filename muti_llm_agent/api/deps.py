"""
api/deps.py — 路由层共享依赖

把原先挤在 muti_llm_agent/main.py 里的「会话标识归一 / 编排器获取 / 鉴权 / 空闲
状态快照」等横切逻辑集中到这里，让每个 router 只关心自己的业务：

  - router_setup      配置与模型花名册
  - router_kb         共享知识库（注入 / 上传解析）
  - router_run        运行控制、纠偏、续轮、摘要
  - router_stream     实时推送（WebSocket / SSE）与日志检索
  - router_export     导出、历史、复盘
  - router_dev        接口级调试（清单 / 通用调用 / 冒烟）
  - router_integration 飞书 / DSH / webhook

这样做的直接收益是「模块化调试」：定位问题时可以直接看某个 router 文件，
而不是在一个 700 行的 main.py 里翻；接口控制台（/api/dev/endpoints）也按
本包的模块划分给接口分组。
"""
from __future__ import annotations

import hmac
import time
from pathlib import Path

from fastapi.responses import JSONResponse

from muti_llm_agent.config import PROJECT_ROOT, settings
from muti_llm_agent.manager import manager

# 静态资源目录（本文件位于 muti_llm_agent/api/ 下，上一级是包目录）
STATIC_DIR = Path(__file__).resolve().parent.parent / "static"
DATA_DIR = PROJECT_ROOT / "data"
DISCUSSIONS_DIR = DATA_DIR / "discussions"
DISCUSSIONS_DIR.mkdir(parents=True, exist_ok=True)


def ts() -> str:
    """文件名用时间戳（YYYYmmdd_HHMMSS）。"""
    return time.strftime("%Y%m%d_%H%M%S")


def normalize_sid(session_id: str | None) -> str:
    """会话标识归一：None / 空串回退 "default"。

    个人级试点仍保留 session_id：对外集成（飞书 / DSH）与接口控制台都需要一个
    稳定可寻址的运行标识；但它不再承担多租户隔离语义。
    """
    return (session_id or "").strip() or "default"


def resolve_sid(payload, query_sid: str | None = None) -> str:
    """解析会话标识：请求体优先，其次查询串。

    控制类端点（pause / resume / end / save_markdown）前端是把 session_id 放在
    查询串上的（`?session_id=...`），只读请求体会静默落到 "default" 会话 ——
    即对默认会话暂停 / 结束，而用户自己的会话毫无反应。这里两侧都接受。
    """
    body_sid = getattr(payload, "session_id", None) if payload is not None else None
    return normalize_sid(body_sid or query_sid)


def get_orch(session_id: str | None):
    """按 session_id 获取（必要时创建）会话编排器。

    仅写端点（configure / start / steer / followup）使用：读端点不该有
    「新建会话」副作用，否则任意 session_id 都能把 Orchestrator 灌进注册表。
    """
    return manager.get_or_create(normalize_sid(session_id))


def existing_orch(session_id: str | None):
    """按 session_id 取已存在的会话；不存在返回 None（不创建）。"""
    return manager.get(normalize_sid(session_id))


def idle_status_body(session_id: str) -> dict:
    """未创建会话的空闲状态快照（只读端点用，形状与正常 status 一致）。"""
    from muti_llm_agent.fairness import FairnessGovernor

    body = {
        "session_id": session_id,
        "status": "idle",
        "phase": "idle",
        "mode": settings.DISCUSSION_MODE,
        "trace_id": "",
        "round": 0,
        "max_rounds": 8,
        "topic": "",
        "agents": [],
        "judge": None,
        "tasks": [],
        "shared_context": {},
        "spent_tokens": 0,
        "output_tokens": 0,
        "fairness": FairnessGovernor().report(),
        "elapsed_time_ms": 0,
        "judgment": None,
        "iteration": 1,
        "steering": {"enabled": True, "total": 0, "added": 0,
                     "injected": 0, "pending": 0, "by_status": {}, "by_kind": {}},
        "error_count": 0,
    }
    # 进程重启前遗留的中断会话：即使本进程还没创建该会话，也要把中断信息回给前端
    im = manager.interrupted_meta_for(session_id)
    if im:
        body["status"] = "interrupted"
        body["interrupted"] = True
        body["topic"] = im.get("topic") or ""
        body["round"] = im.get("round", 0)
        body["max_rounds"] = im.get("max_rounds", 8)
    return body


def status_body(orch) -> dict:
    """已存在会话的状态快照（/api/status 与 SSE 首帧共用）。"""
    # 只读端点不创建会话：轮询不该有「新建 Orchestrator」的副作用
    body = {
        "session_id": orch.session_id,
        "status": orch.status,
        "phase": orch.phase,
        "mode": orch.mode,
        "trace_id": orch.trace_id,
        "round": orch.round,
        "max_rounds": orch.max_rounds,
        "topic": orch.topic,
        "agents": [{"name": a.name, "role": a.role, "is_main": a.is_main,
                    "stance": a.stance,
                    "model_id": a.model_config.get("model_id", "")}
                   for a in orch.agents],
        "judge": {"name": orch.judge.name,
                  "model_id": orch.judge.model_config["model_id"]}
        if orch.judge else None,
        "tasks": orch.tasks,
        "shared_context": orch.shared_context,
        # 只读遥测：token 用量不再做任何约束，仅保留观测（实时显示）
        "spent_tokens": orch.spent,          # 累计 token 用量（输入+输出）
        "output_tokens": orch.output_tokens,  # 累计输出 token（实时）
        # 公平性治理报告（L1-L6 各层状态；替代旧的 budget_plan 视图）
        "fairness": orch.governor.report(),
        "elapsed_time_ms": int((time.time() - orch._started_at) * 1000)
        if orch._started_at else 0,
        "judgment": orch.judgment,
        # 事件总线背压指标：订阅者数量 / 队列上限 / 累计丢弃事件数
        "eventbus": orch.event_bus.stats(),
        # 中途纠偏与迭代链（多模型协同试点的可见性）
        "steering": orch.steering.stats(),
        "iteration": orch.iteration_no,
        "iterations": list(orch.iterations),
        "error_count": len(orch.errors),
        "followup_focus": orch.followup_focus,
    }
    return body


# ---------------- 访问控制（可选令牌鉴权） ----------------
# 默认（AUTH_TOKEN 为空）不校验，靠 start.py 只绑 127.0.0.1 保证本地安全；
# 一旦用 HOST=0.0.0.0 对外提供服务，务必在 .env 设置 AUTH_TOKEN，
# 否则任何人都能改写配置、发起（消耗真实额度的）讨论。
PUBLIC_PATHS = {"/", "/favicon.ico", "/console"}
PUBLIC_PREFIXES = ("/static/",)


def token_ok(candidate: str) -> bool:
    """常量时间比较令牌；未配置令牌时视为放行。"""
    expected = settings.AUTH_TOKEN or ""
    if not expected:
        return True
    return hmac.compare_digest((candidate or "").strip(), expected)


def bearer_of(header: str) -> str:
    """从 Authorization 头里取 Bearer 令牌（无则空串）。"""
    h = (header or "").strip()
    return h[7:].strip() if h.lower().startswith("bearer ") else ""


def request_token(request) -> str:
    """从 Authorization: Bearer 或 ?token= 中取令牌。"""
    return bearer_of(request.headers.get("authorization", "")) or \
        (request.query_params.get("token") or "").strip()


def unauthorized(message: str = "未授权：缺少或错误的访问令牌",
                 status_code: int = 401) -> JSONResponse:
    return JSONResponse({"ok": False, "error": message}, status_code=status_code)


def check_dsh_secret(request) -> JSONResponse | None:
    """DSH 集成端的共享密钥校验（未配置 DSH_SHARED_SECRET 时放行）。"""
    expected = settings.DSH_SHARED_SECRET or ""
    if not expected:
        return None
    got = bearer_of(request.headers.get("authorization", "")) or \
        (request.query_params.get("secret") or "").strip()
    if hmac.compare_digest(got, expected):
        return None
    return unauthorized("未授权：DSH 共享密钥缺失或错误")
