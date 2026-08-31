"""
main.py — FastAPI 应用入口（后端）

提供：
  - 静态 UI 服务（圆桌界面）
  - REST 接口：配置 Agent / 注入知识 / 开始·暂停·继续·结束讨论 / 导出 Markdown
  - WebSocket(/ws)：把讨论事件实时推送到前端

多会话（P1-7）：
  - 所有讨论相关端点通过 session_id 绑定到独立 Orchestrator 实例
    （由 app.manager.DebateManager 管理，缺省会话为 "default"）；
  - 会话元数据持久化到 data/sessions/{session_id}.json，进程重启后
    遗留的 running/paused 会话被标记为 interrupted，供前端展示中断横幅；
  - 讨论完成回调（自动落盘 Markdown）通过 manager.set_done_handler 注册。

统一由项目根目录的 start.py 启动（uvicorn）。
"""
import asyncio
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, File, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from app.config import PROJECT_ROOT
from app.file_parser import SUPPORTED_EXTS, ParseError, parse_file, split_paragraphs
from app.history import get_markdown, list_discussions, record_discussion
from app.llm import PROVIDER_PRESETS
from app.logger import log
from app.manager import manager
from app.schemas import (
    ConfigureRequest,
    IngestRequest,
    SaveMarkdownRequest,
    SessionRequest,
    StartRequest,
)
from app.vector_store import shared_vector_store

STATIC_DIR = Path(__file__).resolve().parent / "static"
DATA_DIR = PROJECT_ROOT / "data"
DISCUSSIONS_DIR = DATA_DIR / "discussions"
DISCUSSIONS_DIR.mkdir(parents=True, exist_ok=True)


def _ts():
    return time.strftime("%Y%m%d_%H%M%S")


# 持有所有后台 asyncio 任务的强引用，防止任务被 GC 提前回收（RUF006）
_background_tasks: set[asyncio.Task] = set()


def _spawn_task(coro):
    """创建并登记一个后台任务，任务结束后自动从集合移除。"""
    task = asyncio.create_task(coro)
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)
    return task


def _get_orch(session_id: str | None):
    """按 session_id 获取会话编排器（缺省 / 空串回退 "default"）。"""
    return manager.get_or_create(session_id or "default")


# ---------------- 启动：监听"完成"事件，自动保存 Markdown ----------------
def _persist_done_markdown(orch) -> str:
    """讨论完成时把 Markdown 落盘并记录历史索引；返回文件名。"""
    md = orch.export_markdown()
    fname = f"discussion_{_ts()}.md"
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
        agents=[{"name": a.name, "role": a.role,
                 "stance": a.stance, "is_main": a.is_main}
                for a in orch.agents],
    )
    return fname


def _on_done(orch):
    """讨论完成回调：落盘失败不静默吞掉，记录日志并重试一次。"""
    for attempt in (1, 2):
        try:
            _persist_done_markdown(orch)
            return
        except Exception as e:
            log("error", "lifespan.save_markdown",
                f"讨论落盘失败（第 {attempt} 次）", error=str(e))


@asynccontextmanager
async def lifespan(app: FastAPI):
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
    # 注册完成回调（每个会话讨论完成时自动落盘 Markdown）
    manager.set_done_handler(_on_done)
    yield


app = FastAPI(title="多 Agent 圆桌协作系统", lifespan=lifespan)

app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


# ---------------- 页面 ----------------
@app.get("/")
async def index():
    return FileResponse(str(STATIC_DIR / "index.html"))


def _redact_presets():
    """对外暴露 Provider 预设时剥离 api_key，避免密钥泄露。"""
    return {
        name: {k: (v if k != "api_key" else "") for k, v in preset.items()}
        for name, preset in PROVIDER_PRESETS.items()
    }


# ---------------- 默认配置 ----------------
@app.get("/api/defaults")
async def defaults():
    return {
        "providers": _redact_presets(),
        "default_provider": "default",
        "vector_backend": shared_vector_store.backend,
        "max_agents": 6,
        "upload_exts": SUPPORTED_EXTS,
    }


# ---------------- 配置 Agent ----------------
@app.post("/api/configure")
async def configure(payload: ConfigureRequest):
    orch = _get_orch(payload.session_id)
    if orch.status == "running":
        return JSONResponse(
            {"ok": False, "error": "讨论进行中，请先结束或等待后再重新配置"},
            status_code=409,
        )
    agents = [a.model_dump() for a in payload.agents]
    if not (1 <= len(agents) <= 6):
        return JSONResponse({"ok": False, "error": "Agent 数量需在 1-6 之间"}, status_code=400)
    topic = payload.topic
    max_rounds = payload.max_rounds
    judge_config = payload.judge or {}
    try:
        orch.configure(agents, topic=topic, max_rounds=max_rounds,
                       judge_config=judge_config)
    except ValueError as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=400)
    return {
        "ok": True,
        "session_id": orch.session_id,
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
    }


# ---------------- 注入共享知识 ----------------
@app.post("/api/ingest")
async def ingest(payload: IngestRequest):
    text = payload.text.strip()
    source = payload.source or "用户注入"
    if not text:
        return JSONResponse({"ok": False, "error": "文本为空"}, status_code=400)
    # 按段落切分
    chunks = [t for t in text.split("\n") if t.strip()]
    if not chunks:
        chunks = [text]
    shared_vector_store.add(chunks, [{"source": source} for _ in chunks])
    return {"ok": True, "added": len(chunks), "backend": shared_vector_store.backend}


# ---------------- 上传文件解析并注入共享知识 ----------------
@app.post("/api/upload")
async def upload_file(file: UploadFile = File(...)):  # noqa: B008  FastAPI 标准注入写法
    """上传文档 / 表格 / PDF / 图片，解析为文本后注入共享向量库。"""
    try:
        data = await file.read()
    except Exception:
        return JSONResponse({"ok": False, "error": "读取上传文件失败"}, status_code=400)

    if not file.filename:
        return JSONResponse({"ok": False, "error": "缺少文件名"}, status_code=400)

    try:
        text, meta = parse_file(file.filename, data)
    except ParseError as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=400)

    if not text.strip():
        return JSONResponse({"ok": False, "error": "未能从文件中提取到文本内容（可能是扫描件/图片型 PDF）"}, status_code=400)

    source = f"{meta['source']}（文件上传）"
    chunks = split_paragraphs(text, source)
    shared_vector_store.add(chunks, [{"source": source} for _ in chunks])
    return {
        "ok": True,
        "added": len(chunks),
        "chars": meta["chars"],
        "truncated": meta["truncated"],
        "backend": shared_vector_store.backend,
        "source": source,
    }


# ---------------- 开始讨论 ----------------
@app.post("/api/start")
async def start(payload: StartRequest = None):
    payload = payload or StartRequest()
    orch = _get_orch(payload.session_id)
    if orch.status == "running":
        return JSONResponse({"ok": False, "error": "讨论进行中，请先结束或等待"}, status_code=400)
    if not orch.agents:
        return JSONResponse({"ok": False, "error": "请先配置 Agent"}, status_code=400)
    topic = payload.topic or orch.topic
    max_rounds = payload.max_rounds
    _spawn_task(orch.run_discussion(topic=topic, max_rounds=max_rounds))
    return {
        "ok": True,
        "session_id": orch.session_id,
        "status": "running",
        "phase": "preparing",
        "trace_id": orch.trace_id,
    }


@app.post("/api/pause")
async def pause(payload: SessionRequest = None):
    orch = _get_orch(payload.session_id if payload else None)
    orch.pause()
    return {"ok": True, "session_id": orch.session_id,
            "status": orch.status, "phase": orch.phase}


@app.post("/api/resume")
async def resume(payload: SessionRequest = None):
    orch = _get_orch(payload.session_id if payload else None)
    orch.resume()
    return {"ok": True, "session_id": orch.session_id,
            "status": orch.status, "phase": orch.phase}


@app.post("/api/end")
async def end(payload: SessionRequest = None):
    orch = _get_orch(payload.session_id if payload else None)
    orch.end()
    return {"ok": True, "session_id": orch.session_id,
            "status": orch.status, "phase": orch.phase}


# ---------------- 状态 ----------------
@app.get("/api/status")
async def status(session_id: str = "default"):
    orch = _get_orch(session_id)
    body = {
        "session_id": orch.session_id,
        "status": orch.status,
        "phase": orch.phase,
        "trace_id": orch.trace_id,
        "round": orch.round,
        "max_rounds": orch.max_rounds,
        "topic": orch.topic,
        "agents": [{"name": a.name, "role": a.role, "is_main": a.is_main,
                    "stance": a.stance} for a in orch.agents],
        "judge": {"name": orch.judge.name,
                  "model_id": orch.judge.model_config["model_id"]}
        if orch.judge else None,
        "tasks": orch.tasks,
        "shared_context": orch.shared_context,
        # 只读遥测：token 用量不再做任何约束，仅保留观测（实时显示）
        "spent_tokens": orch.spent,          # 累计 token 用量（输入+输出）
        "output_tokens": orch.output_tokens, # 累计输出 token（实时）
        # 公平性治理报告（L1-L6 各层状态；替代旧的 budget_plan 视图）
        "fairness": orch.governor.report(),
        "elapsed_time_ms": int((time.time() - orch._started_at) * 1000)
        if orch._started_at else 0,
        "judgment": orch.judgment,
    }
    # 进程重启后的中断会话：该会话尚未重新配置（无 agents）时，
    # 用持久化的中断元数据补齐话题信息，供前端展示中断横幅
    if orch.status == "idle" and not orch.agents:
        im = manager.interrupted_meta_for(orch.session_id)
        if im:
            body["status"] = "interrupted"
            body["interrupted"] = True
            body["topic"] = im.get("topic") or orch.topic
            body["round"] = im.get("round", 0)
            body["max_rounds"] = im.get("max_rounds", 8)
    return body


# ---------------- 导出 Markdown ----------------
def _safe_discussion_filename(fname: str) -> str:
    """清洗讨论导出文件名，防路径穿越。

    只保留 basename（丢弃目录部分 / 绝对路径 / `..`），并强制 `.md` 后缀；
    清洗后为空则回退为自动生成的默认文件名。
    """
    if fname:
        fname = Path(fname).name
    if not fname:
        fname = f"discussion_{_ts()}.md"
    if not fname.endswith(".md"):
        fname += ".md"
    return fname


@app.get("/api/export")
async def export(session_id: str = "default"):
    orch = _get_orch(session_id)
    md = orch.export_markdown()
    return JSONResponse({"markdown": md})


@app.post("/api/save_markdown")
async def save_markdown(payload: SaveMarkdownRequest = None):
    payload = payload or SaveMarkdownRequest()
    orch = _get_orch(payload.session_id)
    md = orch.export_markdown()
    fname = _safe_discussion_filename(payload.filename)
    path = DISCUSSIONS_DIR / fname
    path.write_text(md, encoding="utf-8")
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
    return {"ok": True, "path": str(path), "filename": fname}


# ---------------- 历史讨论查询 / 检索 ----------------
@app.get("/api/history")
async def history(q: str = ""):
    """历史讨论列表；q 为可选关键词（按题目 / trace_id 过滤）。"""
    return {"ok": True, "items": list_discussions(q)}


@app.get("/api/history/{filename}")
async def history_view(filename: str):
    """查看某场历史讨论的完整 Markdown 内容。"""
    md = get_markdown(filename)
    if md is None:
        return JSONResponse({"ok": False, "error": "记录不存在"}, status_code=404)
    return {"ok": True, "markdown": md, "filename": filename}


# ---------------- WebSocket 实时事件 ----------------
@app.websocket("/ws")
async def ws_endpoint(websocket: WebSocket, session_id: str = "default"):
    await websocket.accept()
    orch = _get_orch(session_id)
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
            "max_rounds": orch.max_rounds,
            "spent_tokens": orch.spent,          # 累计 token 用量（只读遥测）
            "output_tokens": orch.output_tokens, # 累计输出 token（实时）
            "fairness": orch.governor.report(),
            "agents": [{"name": a.name, "role": a.role, "is_main": a.is_main,
                        "stance": a.stance} for a in orch.agents],
        })
        while True:
            event = await q.get()
            await websocket.send_json(event)
    except (WebSocketDisconnect, Exception):
        pass
    finally:
        orch.event_bus.unsubscribe(q)
