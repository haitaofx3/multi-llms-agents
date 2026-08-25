"""
main.py — FastAPI 应用入口（后端）

提供：
  - 静态 UI 服务（圆桌界面）
  - REST 接口：配置 Agent / 注入知识 / 开始·暂停·继续·结束讨论 / 导出 Markdown
  - WebSocket(/ws)：把讨论事件实时推送到前端

统一由项目根目录的 start.py 启动（uvicorn）。
"""
import asyncio
import json
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, File, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from app.config import settings, PROJECT_ROOT
from app.file_parser import ParseError, parse_file, split_paragraphs, SUPPORTED_EXTS
from app.history import record_discussion, list_discussions, get_markdown
from app.llm import PROVIDER_PRESETS
from app.vector_store import shared_vector_store
from app.orchestrator import orchestrator

STATIC_DIR = Path(__file__).resolve().parent / "static"
DATA_DIR = PROJECT_ROOT / "data"
DISCUSSIONS_DIR = DATA_DIR / "discussions"
DISCUSSIONS_DIR.mkdir(parents=True, exist_ok=True)


def _ts():
    return time.strftime("%Y%m%d_%H%M%S")


# ---------------- 启动：监听"完成"事件，自动保存 Markdown ----------------
@asynccontextmanager
async def lifespan(app: FastAPI):
    q = orchestrator.event_bus.subscribe()

    async def _listener():
        while True:
            ev = await q.get()
            if ev.get("type") == "status" and ev.get("status") == "done":
                try:
                    md = orchestrator.export_markdown()
                    fname = f"discussion_{_ts()}.md"
                    (DISCUSSIONS_DIR / fname).write_text(md, encoding="utf-8")
                    # 历史讨论题目记忆化保存（供后续检索）
                    record_discussion(
                        topic=orchestrator.topic,
                        trace_id=orchestrator.trace_id,
                        filename=fname,
                        status=orchestrator.status,
                        phase=orchestrator.phase,
                        round_no=orchestrator.round,
                        max_rounds=orchestrator.max_rounds,
                        token_spent=orchestrator.spent,
                        token_budget=orchestrator.budget,
                        agents=[{"name": a.name, "role": a.role,
                                 "stance": a.stance, "is_main": a.is_main}
                                for a in orchestrator.agents],
                    )
                except Exception:
                    pass

    task = asyncio.create_task(_listener())
    yield
    # 关闭时：取消监听任务并退订，避免悬挂引用
    task.cancel()
    orchestrator.event_bus.unsubscribe(q)


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
async def configure(payload: dict):
    if orchestrator.status == "running":
        return JSONResponse(
            {"ok": False, "error": "讨论进行中，请先结束或等待后再重新配置"},
            status_code=409,
        )
    agents = payload.get("agents", [])
    if not (1 <= len(agents) <= 6):
        return JSONResponse({"ok": False, "error": "Agent 数量需在 1-6 之间"}, status_code=400)
    topic = payload.get("topic", "")
    max_rounds = int(payload.get("max_rounds", 8))
    budget = payload.get("budget")
    try:
        orchestrator.configure(agents, topic=topic, max_rounds=max_rounds)
        if budget:
            orchestrator.budget = max(0, int(budget))
    except ValueError as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=400)
    return {
        "ok": True,
        "agents": [{"name": a.name, "role": a.role, "is_main": a.is_main,
                    "stance": a.stance,
                    "model_id": a.model_config["model_id"]} for a in orchestrator.agents],
        "status": orchestrator.status,
        "phase": orchestrator.phase,
        "trace_id": orchestrator.trace_id,
        "budget": orchestrator.budget,
        "max_rounds": orchestrator.max_rounds,
    }


# ---------------- 注入共享知识 ----------------
@app.post("/api/ingest")
async def ingest(payload: dict):
    text = payload.get("text", "").strip()
    source = payload.get("source", "用户注入")
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
async def upload_file(file: UploadFile = File(...)):
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
async def start(payload: dict):
    if orchestrator.status == "running":
        return JSONResponse({"ok": False, "error": "讨论进行中，请先结束或等待"}, status_code=400)
    if not orchestrator.agents:
        return JSONResponse({"ok": False, "error": "请先配置 Agent"}, status_code=400)
    topic = payload.get("topic") or orchestrator.topic
    max_rounds = payload.get("max_rounds")
    budget = payload.get("budget")
    asyncio.create_task(orchestrator.run_discussion(
        topic=topic, max_rounds=max_rounds, budget=budget))
    return {
        "ok": True,
        "status": "running",
        "phase": "preparing",
        "trace_id": orchestrator.trace_id,
    }


@app.post("/api/pause")
async def pause():
    orchestrator.pause()
    return {"ok": True, "status": orchestrator.status, "phase": orchestrator.phase}


@app.post("/api/resume")
async def resume():
    orchestrator.resume()
    return {"ok": True, "status": orchestrator.status, "phase": orchestrator.phase}


@app.post("/api/end")
async def end():
    orchestrator.end()
    return {"ok": True, "status": orchestrator.status, "phase": orchestrator.phase}


# ---------------- 状态 ----------------
@app.get("/api/status")
async def status():
    remaining = max(0, orchestrator.budget - orchestrator.spent)
    return {
        "status": orchestrator.status,
        "phase": orchestrator.phase,
        "trace_id": orchestrator.trace_id,
        "round": orchestrator.round,
        "max_rounds": orchestrator.max_rounds,
        "topic": orchestrator.topic,
        "agents": [{"name": a.name, "role": a.role, "is_main": a.is_main,
                    "stance": a.stance} for a in orchestrator.agents],
        "tasks": orchestrator.tasks,
        "shared_context": orchestrator.shared_context,
        "budget": orchestrator.budget,
        "spent": orchestrator.spent,
        "remaining_budget": remaining,
        "judgment": orchestrator.judgment,
    }


# ---------------- 导出 Markdown ----------------
@app.get("/api/export")
async def export():
    md = orchestrator.export_markdown()
    return JSONResponse({"markdown": md})


@app.post("/api/save_markdown")
async def save_markdown(payload: dict = None):
    md = orchestrator.export_markdown()
    fname = payload.get("filename") if payload else None
    if not fname:
        fname = f"discussion_{_ts()}.md"
    if not fname.endswith(".md"):
        fname += ".md"
    path = DISCUSSIONS_DIR / fname
    path.write_text(md, encoding="utf-8")
    # 历史讨论题目记忆化保存（供后续检索）
    record_discussion(
        topic=orchestrator.topic,
        trace_id=orchestrator.trace_id,
        filename=fname,
        status=orchestrator.status,
        phase=orchestrator.phase,
        round_no=orchestrator.round,
        max_rounds=orchestrator.max_rounds,
        token_spent=orchestrator.spent,
        token_budget=orchestrator.budget,
        agents=[{"name": a.name, "role": a.role, "stance": a.stance,
                 "is_main": a.is_main} for a in orchestrator.agents],
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
async def ws_endpoint(websocket: WebSocket):
    await websocket.accept()
    q = orchestrator.event_bus.subscribe()
    try:
        # 连接时立即推送当前状态
        await websocket.send_json({
            "type": "status",
            "status": orchestrator.status,
            "phase": orchestrator.phase,
            "trace_id": orchestrator.trace_id,
            "topic": orchestrator.topic,
            "max_rounds": orchestrator.max_rounds,
            "budget": orchestrator.budget,
            "spent": orchestrator.spent,
            "agents": [{"name": a.name, "role": a.role, "is_main": a.is_main,
                        "stance": a.stance} for a in orchestrator.agents],
        })
        while True:
            event = await q.get()
            await websocket.send_json(event)
    except (WebSocketDisconnect, Exception):  # noqa
        pass
    finally:
        orchestrator.event_bus.unsubscribe(q)
